"""代码修改管道核心。

职责：快照 / 边界 / 验证门 / 自动回滚——智能体按诉求修改千枢平台代码的受控管道。
本模块只做"文件内容替换 + 校验 + 快照 + 回滚"，不执行任何代码/命令（验证门只跑
固定白名单检查：py_compile / vite build / structure_fingerprint）。

接口契约（意图集成层按此调用，勿改签名）：
  code_search(query, limit=10)          只读检索 {hits:[{path,line_no,snippet}]}
  validate_plan(plan)                   {ok, violations:[str]}
  snapshot_and_apply(ticket_id, plan, user)
                                        校验→快照→原子应用 {ok, snapshot_id, applied:[{path,lines}]}
  verify_gate(changed_paths)            {passed, results:[{check,ok,detail}]}
  auto_rollback(snapshot_id, user, force=False)
                                        验证失败自动回滚 {ok, restored:[{path,action}]}
  rollback(snapshot_id, user, force=False)
                                        用户一键回滚（同上+审计）
  snapshot_out(snapshot_id)             展示（截断，不吐全文）
  force 为契约外可选参数（缺省 False，不影响契约调用）：回滚前比对目标文件当前 hash
  与快照 after hash，不一致=应用后有人改过 → 拒绝强还原（ok=False + conflicts），
  显式 force=True 才覆盖第三方改动。

plan 格式：{"title"?: str, "files": [{"path": "相对路径", "new": "全文新内容"}]}
  - new 为空串/缺失 = 删除文件 → 一律拒绝（只改不删）
  - 路径必须 POSIX 相对路径、无 ..、命中 CODE_ALLOW 且不命中 CODE_DENY
  - 上限：MAX_FILES 个文件 / 全部变更 diff 行数（增+删）合计 MAX_LINES

安全边界（三层）：
  ① 可改域 CODE_ALLOW 前缀白名单；② 禁改域 CODE_DENY（安全门钥匙不交给被守的门，
  管道本体 code_mod.py 也在禁改清单）；③ 动作边界：文件数/行数上限、删除拒、
  .env*/requirements* 拒、验证门必过、过不了自动回滚不留半成品。

原子性：快照先行落库（快照=回滚日志）→ 逐文件临时文件+os.replace 原子写；
任一文件写失败 → 全部恢复 before → 快照标 rolled_back → 返回 ok=False。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from app.core.db import SessionLocal
from app.models.agent_code_snapshot import AgentCodeSnapshot
from app.services import audit as audit_svc

# ---- 契约常量（意图集成层可见）----
CODE_ALLOW = (
    "frontend/src/", "backend/app/api/", "backend/app/services/",
    "docs/active/", "docs/tmp/",
)
CODE_DENY = (
    "backend/app/core/security.py",
    "backend/app/core/permissions.py",
    "backend/app/core/client_ip.py",
    "backend/app/core/deps.py",
    # 自愈/变更管道自身与守护进程：LLM 不得修改自己的权限、审批、执行和审计边界。
    "backend/app/services/self_heal/",
    "backend/app/services/copilot/",
    "backend/app/services/notify.py",
    "backend/app/services/audit.py",
    "backend/app/api/v1/issues.py",
    "backend/app/api/v1/agent_copilot.py",
    "backend/app/api/v1/agent_lifecycle.py",
    "backend/app/copilotd/",
    "requirements.txt",
    ".env",
)
MAX_FILES, MAX_LINES = 5, 200

# ---- 内部常量 ----
PROJECT_ROOT = Path(__file__).resolve().parents[4]   # /opt/qianshu
VERIFY_TIMEOUT = 120          # 子进程检查超时（vite build 契约值）
_SEARCH_FILESIZE = 512 * 1024
_DIFF_HEAD_LINES = 20
_SNIPPET_MAX = 200
_DIFF_LINE_MAX = 300
_SEARCH_EXCLUDE_DIRS = {"__pycache__", "node_modules", "dist", ".git",
                        ".venv", "venv", "backups", ".pytest_cache"}
_PY_COMPILE_RUNNER = (
    "import os,py_compile,sys,tempfile\n"
    "fd,cfile = tempfile.mkstemp(suffix='.pyc')\n"
    "os.close(fd)\n"
    "try:\n"
    "    py_compile.compile(sys.argv[1], cfile=cfile, doraise=True)\n"
    "finally:\n"
    "    os.unlink(cfile)\n"
)


# ============================================================
# 路径安全与边界
# ============================================================
def _norm_path(p) -> str:
    """计划内路径规范化：POSIX 相对路径、无空段/点段/越界段。非法抛 ValueError。"""
    s = str(p or "").replace("\\", "/").strip()
    if not s:
        raise ValueError("路径为空")
    if s.startswith("/") or s.startswith("~") or (len(s) > 1 and s[1] == ":"):
        raise ValueError(f"只接受相对路径: {p}")
    parts = [x for x in s.split("/") if x not in ("", ".")]
    if not parts:
        raise ValueError(f"路径非法: {p}")
    if any(x == ".." for x in parts):
        raise ValueError(f"路径越界（含 ..）: {p}")
    return "/".join(parts)


def _safe_path(rel: str) -> Path:
    """拼到 PROJECT_ROOT 下并二次校验不越界（防符号链接逃逸）。"""
    fp = (PROJECT_ROOT / rel).resolve()
    root = PROJECT_ROOT.resolve()
    if fp != root and root not in fp.parents:
        raise ValueError(f"路径越界: {rel}")
    return fp


def _boundary_violation(norm: str) -> str:
    """边界判定：deny 优先于 allow。返回违规描述，空串=合规。"""
    for d in CODE_DENY:
        if norm == d or (d.endswith("/") and norm.startswith(d)):
            return f"{norm} 属禁改域（安全边界，直接拒绝）"
    base = norm.rsplit("/", 1)[-1]
    if base.startswith(".env"):
        return f"{norm} 禁改（.env* 环境配置）"
    if base.startswith("requirements"):
        return f"{norm} 禁改（依赖清单——需要新依赖=出提案人工装）"
    if not any(norm.startswith(a) for a in CODE_ALLOW):
        return f"{norm} 不在可改域（白名单：{', '.join(CODE_ALLOW)}）"
    return ""


# ============================================================
# 文本读写（UTF-8 文本管道，字节级可还原）
# ============================================================
def _read_text(rel: str):
    """-> (text|None, err|None)。None+None=文件不存在（新建场景）。"""
    try:
        fp = _safe_path(rel)
    except ValueError as e:
        return None, str(e)
    try:
        data = fp.read_bytes()
    except FileNotFoundError:
        return None, None
    except IsADirectoryError:
        return None, "路径是目录不是文件"
    except OSError as e:
        return None, f"读取失败: {e}"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "非 UTF-8 文本，不支持自动修改（二进制/编码异常）"


def _write_atomic(rel: str, text: str) -> None:
    """临时文件 + os.replace 原子落盘（不覆盖磁盘半成品）。"""
    fp = _safe_path(rel)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(fp.parent), prefix=f".{fp.name}.", suffix=".codemod-tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(fp))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restore_entries(entries: list) -> list:
    """逐文件字节恢复 before。before 不存在的（本次新建）→ 删除该文件还原现场。
    -> [{path, action}]  action: restored / removed / error: ..."""
    restored = []
    for e in entries:
        path = e.get("path", "")
        try:
            if e.get("before_existed", True):
                _write_atomic(path, e.get("before", ""))
                restored.append({"path": path, "action": "restored"})
            else:
                fp = _safe_path(path)
                if fp.is_file():
                    fp.unlink()
                restored.append({"path": path, "action": "removed"})
        except Exception as exc:
            restored.append({"path": path, "action": f"error: {exc}"})
    return restored


# ============================================================
# diff 统计与展示截断
# ============================================================
def _diff_lines(before: str, after: str):
    """统一 diff 行数统计（增/删），忽略文件头行。"""
    d = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    add = rem = 0
    for line in d:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            rem += 1
    return add, rem


def _diff_summary(before: str, after: str, path: str) -> str:
    "+/- 统计 + 首 20 行 diff（截断展示，全文只入快照不出参）。"
    add, rem = _diff_lines(before, after)
    diff = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
    body = [ln[:_DIFF_LINE_MAX] for ln in diff[:_DIFF_HEAD_LINES]]
    lines = [f"+{add}/-{rem}"] + body
    if len(diff) > _DIFF_HEAD_LINES:
        lines.append(f"...（diff 共 {len(diff)} 行，仅展示前 {_DIFF_HEAD_LINES} 行）")
    return "\n".join(lines)


# ============================================================
# 只读检索
# ============================================================
_CJK_FUNCWORDS = re.compile(
    r"(把|的|里|了|是在|和|与|及|改成|改为|换成|请|帮我|我|将|给|从|对|被|也|都|很|么|吗|呢|吧|但|而且|或|如|向|让|一下|这个|那个|相关|的|地|得)")
_QUOTED_RE = re.compile(r"[「“『\"]([^」”』\"]{1,40})[」”』\"]")


def _search_terms(query: str) -> list:
    """中文感知分词，避免无空格整句被当作一个词而零命中：
    引号内容整段保留；中文按功能词切段（≥2 字）；英文/数字按空白分词（≥2 字符）。"""
    s = str(query or "")
    terms: list = []
    for m in _QUOTED_RE.finditer(s):
        t = m.group(1).strip().lower()
        if len(t) >= 2:
            terms.append(t)
        s = s.replace(m.group(0), " ")
    for seg in _CJK_FUNCWORDS.split(s):
        for piece in re.split(r"\s+", seg.strip().lower()):   # 空白二次切分（防多词段粘连成一个词）
            if len(piece) >= 2 and re.search(r"[一-鿿]", piece):
                terms.append(piece)
    for w in re.findall(r"[A-Za-z0-9_\-./]{2,}", s):
        terms.append(w.lower())
    out, seen = [], set()
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:12]


def code_search(query: str, limit: int = 10) -> dict:
    """只读检索：中文感知分词后按命中词数排序（OR 召回+排序保精度），
    大小写不敏感子串。排除 __pycache__/node_modules/dist/.git 等，二进制与超大文件跳过。"""
    terms = _search_terms(query)
    if not terms:
        return {"hits": []}
    try:
        limit = max(1, min(int(limit or 10), 100))
    except (TypeError, ValueError):
        limit = 10
    scored: list = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in _SEARCH_EXCLUDE_DIRS)
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            try:
                if fp.stat().st_size > _SEARCH_FILESIZE:
                    continue
                data = fp.read_bytes()
            except OSError:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            rel = str(fp.relative_to(PROJECT_ROOT)).replace("\\", "/")
            for i, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                score = sum(1 for t in terms if t in low)
                if score:
                    # 同分时短行优先（词条/配置行 > 长注释行）；i18n 词表行加权
                    boost = 2 if ("i18n" in rel and "locale" in rel) else 0
                    scored.append((-(score + boost), len(line.strip()), rel, i,
                                   line.strip()[:_SNIPPET_MAX]))
    scored.sort()
    hits = [{"path": rel, "line_no": i, "snippet": snip}
            for _s, _l, rel, i, snip in scored[:limit]]
    return {"hits": hits}


# ============================================================
# 计划校验
# ============================================================
def _plan_files(plan: dict) -> list:
    """取计划文件清单（files 为主，changes/items 为兼容别名）。"""
    p = plan if isinstance(plan, dict) else {}
    return p.get("files") or p.get("changes") or p.get("items") or []


def validate_plan(plan: dict) -> dict:
    """逐文件查 allow/deny、行数上限、删除类（空 new=删除）拒、.env/依赖拒。
    另含基础安全：越界路径拒、重复路径拒、非 UTF-8 拒。"""
    violations: list = []
    files = _plan_files(plan)
    if not isinstance(files, list) or not files:
        return {"ok": False,
                "violations": ["变更清单为空（files 必填，每项 {path, new} 全文新内容）"]}
    if len(files) > MAX_FILES:
        violations.append(f"文件数 {len(files)} 超上限 {MAX_FILES}——拆分提案分批确认")
    seen: set = set()
    total_lines = 0
    for i, item in enumerate(files):
        tag = f"第 {i + 1} 项"
        if not isinstance(item, dict):
            violations.append(f"{tag}: 必须是 {{path, new}} 对象")
            continue
        try:
            path = _norm_path(item.get("path", ""))
        except ValueError as e:
            violations.append(f"{tag}: {e}")
            continue
        if path in seen:
            violations.append(f"{path}: 重复出现（同一文件只允许一条变更）")
            continue
        seen.add(path)
        v = _boundary_violation(path)
        if v:
            violations.append(v)
            continue
        new = item.get("new", "")
        if new is None:
            new = ""
        if not isinstance(new, str):
            violations.append(f"{path}: new 必须是字符串（全文新内容）")
            continue
        if new == "":
            violations.append(f"{path}: new 为空 = 删除文件——删除类一律拒绝（只改不删）")
            continue
        before, err = _read_text(path)
        if err:
            violations.append(f"{path}: {err}")
            continue
        add, rem = _diff_lines(before or "", new)
        total_lines += add + rem
    if total_lines > MAX_LINES:
        violations.append(f"变更行数合计 {total_lines} 超上限 {MAX_LINES}——拆分提案分批确认")
    return {"ok": not violations, "violations": violations}


# ============================================================
# 快照 + 原子应用
# ============================================================
def _audit(action: str, user, *, resource_id, detail: dict, result: str = "ok") -> None:
    db = SessionLocal()
    try:
        audit_svc.record(
            db, action=action,
            user_id=getattr(user, "id", None),
            username=getattr(user, "username", ""),
            resource_type="code_mod", resource_id=str(resource_id),
            detail=detail, result=result,
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _set_status(snapshot_id: int, status: str) -> None:
    db = SessionLocal()
    try:
        snap = db.query(AgentCodeSnapshot).filter(AgentCodeSnapshot.id == snapshot_id).first()
        if snap:
            snap.status = status
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _save_verify(snapshot_id: int, verify: dict, phase: str) -> None:
    db = SessionLocal()
    try:
        snap = db.query(AgentCodeSnapshot).filter(AgentCodeSnapshot.id == snapshot_id).first()
        if snap:
            payload = dict(verify)
            payload["phase"] = phase
            payload["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            snap.verify_json = json.dumps(payload, ensure_ascii=False, default=str)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def snapshot_and_apply(ticket_id: int, plan: dict, user) -> dict:
    """校验→快照（逐文件 before 全文+hash）→原子应用（全成或全败）。
    成功 {ok, snapshot_id, applied:[{path, lines}]}；
    校验不过 {ok:False, violations}（不动任何文件）；
    写失败自动全量回滚后 {ok:False, snapshot_id, error, restored}。"""
    check = validate_plan(plan)
    if not check["ok"]:
        return {"ok": False, "violations": check["violations"],
                "error": "计划未通过校验，未做任何改动"}

    files = _plan_files(plan)
    entries: list = []
    for item in files:
        path = _norm_path(item["path"])
        before, err = _read_text(path)
        if err:
            return {"ok": False, "violations": [f"{path}: {err}"],
                    "error": "读取现状失败，未做任何改动"}
        existed = before is not None
        before = before or ""
        after = item["new"]
        add, rem = _diff_lines(before, after)
        entries.append({
            "path": path,
            "before": before,
            "after": after,
            "diff_summary": _diff_summary(before, after, path),
            "before_hash": hashlib.sha256(before.encode("utf-8")).hexdigest(),
            "after_hash": hashlib.sha256(after.encode("utf-8")).hexdigest(),
            "before_existed": existed,
            "lines": add + rem,
        })

    # 快照先行落库：快照即回滚日志，先有账再动字节
    db = SessionLocal()
    try:
        snap = AgentCodeSnapshot(
            ticket_id=int(ticket_id or 0),
            files_json=json.dumps(
                [{k: e[k] for k in ("path", "before", "after", "diff_summary",
                                    "before_hash", "after_hash", "before_existed")}
                 for e in entries],
                ensure_ascii=False),
            verify_json="", status="applied", created_at=datetime.now(),
        )
        db.add(snap)
        db.commit()
        db.refresh(snap)
        snapshot_id = snap.id
    except Exception as exc:
        db.rollback()
        return {"ok": False, "error": f"快照落库失败，未做任何改动: {exc}"}
    finally:
        db.close()

    written: list = []
    try:
        for e in entries:
            _write_atomic(e["path"], e["after"])
            written.append(e["path"])
    except Exception as exc:
        # 原子性：全部恢复 before，半成品绝不留现场
        restored = _restore_entries(entries)
        _set_status(snapshot_id, "rolled_back")
        base_detail = {"snapshot_id": snapshot_id, "ticket_id": ticket_id,
                       "files": [e["path"] for e in entries]}
        _audit("code.apply", user, resource_id=snapshot_id,
               detail=dict(base_detail, error=str(exc)), result="fail")
        _audit("code.rollback", user, resource_id=snapshot_id,
               detail=dict(base_detail, trigger="auto", reason="apply_failed"),
               result="ok" if all(r["action"] != "error" for r in restored) else "fail")
        return {"ok": False, "snapshot_id": snapshot_id,
                "error": f"应用失败，已全部回滚到 before: {exc}",
                "written_before_fail": written, "restored": restored}

    _audit("code.apply", user, resource_id=snapshot_id,
           detail={"snapshot_id": snapshot_id, "ticket_id": ticket_id,
                   "files": [e["path"] for e in entries],
                   "lines": sum(e["lines"] for e in entries)})
    return {"ok": True, "snapshot_id": snapshot_id,
            "applied": [{"path": e["path"], "lines": e["lines"]} for e in entries]}


# ============================================================
# 验证门（子进程跑固定检查，超时/异常=该项 fail）
# ============================================================
def _run(cmd: list, cwd, timeout: int) -> tuple:
    """-> (ok, detail)。只跑固定白名单命令，不接受外部命令注入。"""
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"超时（>{timeout}s）"
    except Exception as e:
        return False, f"执行异常: {e}"
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip().splitlines()[-8:]
        return False, f"退出码 {proc.returncode}: " + " | ".join(tail)[:400]
    return True, ""


def verify_gate(changed_paths: list, snapshot_id: int = None) -> dict:
    """验证门：{passed, results:[{check, ok, detail}]}。
    ① 变更 .py 全部 py_compile；② 含 frontend/ 改动 → vite build（120s 超时）；
    ③ 含 backend/app 改动 → tests/quality/structure_fingerprint。
    可选 snapshot_id：把结果写回快照 verify_json（契约外可选参数，不影响契约调用）。"""
    paths: list = []
    for p in changed_paths or []:
        try:
            paths.append(_norm_path(p))
        except ValueError:
            paths.append(str(p))

    results: list = []

    # 1) py_compile：变更的 Python 文件逐个编译（已消失的文件跳过，如回滚后新建文件）
    py_files = [p for p in paths if p.endswith(".py")]
    if not py_files:
        results.append({"check": "py_compile", "ok": True, "detail": "无 Python 文件变更"})
    else:
        bad: list = []
        compiled = 0
        for p in py_files:
            if not (PROJECT_ROOT / p).is_file():
                continue
            compiled += 1
            ok, detail = _run([sys.executable, "-c", _PY_COMPILE_RUNNER, p],
                              cwd=PROJECT_ROOT, timeout=VERIFY_TIMEOUT)
            if not ok:
                bad.append(f"{p}: {detail}")
        if bad:
            results.append({"check": "py_compile", "ok": False,
                            "detail": "; ".join(bad)[:500]})
        else:
            results.append({"check": "py_compile", "ok": True,
                            "detail": f"{compiled}/{len(py_files)} 个 Python 文件编译通过"})

    # 2) vite build（前端改动才跑）
    if any(p.startswith("frontend/") for p in paths):
        vite = PROJECT_ROOT / "frontend" / "node_modules" / ".bin" / "vite"
        if not vite.is_file():
            results.append({"check": "vite_build", "ok": False,
                            "detail": f"vite 可执行不存在: {vite}"})
        else:
            ok, detail = _run([str(vite), "build"], cwd=PROJECT_ROOT / "frontend",
                              timeout=VERIFY_TIMEOUT)
            results.append({"check": "vite_build", "ok": ok,
                            "detail": detail or "vite build 通过"})

    # 3) 结构指纹（后端改动才跑）
    if any(p.startswith("backend/app") for p in paths):
        script = PROJECT_ROOT / "tests" / "quality" / "structure_fingerprint.py"
        if not script.is_file():
            results.append({"check": "structure_fingerprint", "ok": False,
                            "detail": f"脚本不存在: {script}"})
        else:
            ok, detail = _run([sys.executable, str(script)], cwd=PROJECT_ROOT,
                              timeout=VERIFY_TIMEOUT)
            results.append({"check": "structure_fingerprint", "ok": ok,
                            "detail": detail or "结构指纹通过"})

    passed = all(r["ok"] for r in results)
    out = {"passed": passed, "results": results}
    if snapshot_id:
        _save_verify(snapshot_id, out, phase="verify")
    return out


# ============================================================
# 回滚
# ============================================================
def _after_hash(e: dict) -> str:
    """快照条目 after 指纹：优先落库的 after_hash，旧快照从 after 全文现算（同口径）。"""
    h = e.get("after_hash")
    if h:
        return str(h)
    return hashlib.sha256(str(e.get("after") or "").encode("utf-8")).hexdigest()


def _current_conflicts(entries: list) -> list:
    """回滚前冲突检测，防止跨快照覆盖：目标文件当前 hash ≠ 快照 after hash
    = 应用后有人改过（或文件消失）→ 列出冲突 [{path, reason}]，交调用方拒绝或 force。
    不改任何文件，只读比对。"""
    out: list = []
    for e in entries:
        path = e.get("path", "")
        cur, err = _read_text(path)
        if err:
            out.append({"path": path, "reason": err})
            continue
        if cur is None:
            out.append({"path": path, "reason": "文件已不存在（应用后被删除或改名）"})
            continue
        if hashlib.sha256(cur.encode("utf-8")).hexdigest() != _after_hash(e):
            out.append({"path": path,
                        "reason": "当前内容与快照应用结果（after）不一致——应用后有人改过"})
    return out


def _do_rollback(snapshot_id: int, user, trigger: str, force: bool = False) -> dict:
    db = SessionLocal()
    try:
        snap = (db.query(AgentCodeSnapshot)
                .filter(AgentCodeSnapshot.id == snapshot_id).first())
        if not snap:
            return {"ok": False, "error": f"快照不存在: {snapshot_id}"}
        if snap.status == "rolled_back":
            return {"ok": True, "already": True, "restored": [],
                    "snapshot_id": snapshot_id, "note": "快照此前已回滚"}
        entries = json.loads(snap.files_json or "[]")
        ticket_id = snap.ticket_id
    except Exception as exc:
        return {"ok": False, "error": f"读取快照失败: {exc}"}
    finally:
        db.close()

    # 跨快照冲突检测：不一致=有人中途改过 → 拒绝强还原（force=True 才覆盖第三方改动）
    conflicts = _current_conflicts(entries)
    if conflicts and not force:
        _audit("code.rollback", user, resource_id=str(snapshot_id), result="fail",
               detail={"snapshot_id": snapshot_id, "ticket_id": ticket_id,
                       "trigger": trigger, "forced": False, "conflicts": conflicts})
        return {"ok": False, "snapshot_id": snapshot_id, "conflicts": conflicts,
                "error": "拒绝回滚：目标文件在快照应用后被改动过，强还原会覆盖第三方修改"
                         "（确认无误可带 force 参数强制回滚）"}

    restored = _restore_entries(entries)
    verify = verify_gate([e.get("path", "") for e in entries], snapshot_id=snapshot_id)
    _set_status(snapshot_id, "rolled_back")
    detail = {"snapshot_id": snapshot_id, "ticket_id": ticket_id,
              "files": [e.get("path", "") for e in entries],
              "trigger": trigger, "verify_passed": verify["passed"]}
    if conflicts:
        detail.update({"forced": True, "conflicts": conflicts})
    clean = verify["passed"] and all(r["action"] != "error" for r in restored)
    _audit("code.rollback", user, resource_id=str(snapshot_id), detail=detail,
           result="ok" if clean else "warn")
    return {"ok": True, "snapshot_id": snapshot_id, "restored": restored,
            "verify": verify, **({"forced": True, "conflicts": conflicts} if conflicts else {})}


def auto_rollback(snapshot_id: int, user, force: bool = False) -> dict:
    """验证失败自动回滚：逐文件字节恢复 before→再跑 verify_gate 确认干净。
    回滚前先做跨快照冲突检测（force 语义同 rollback）。"""
    return _do_rollback(snapshot_id, user, trigger="auto", force=force)


def rollback(snapshot_id: int, user, force: bool = False) -> dict:
    """用户一键回滚（同 auto_rollback + 审计留痕）。force=True 才覆盖第三方改动。"""
    return _do_rollback(snapshot_id, user, trigger="user", force=force)


# ============================================================
# 展示（截断，不吐全文）
# ============================================================
def snapshot_out(snapshot_id) -> dict:
    """展示口径：{id, ticket_id, files:[{path, diff_summary, before_hash}],
    created_at, verified, status}——before/after 全文只入快照不出参。"""
    db = SessionLocal()
    try:
        snap = (db.query(AgentCodeSnapshot)
                .filter(AgentCodeSnapshot.id == snapshot_id).first())
        if not snap:
            return {"id": snapshot_id, "error": "快照不存在"}
        try:
            entries = json.loads(snap.files_json or "[]")
        except ValueError:
            entries = []
        try:
            verify = json.loads(snap.verify_json) if snap.verify_json else None
        except ValueError:
            verify = None
        return {
            "id": snap.id,
            "ticket_id": snap.ticket_id,
            "files": [{"path": e.get("path", ""),
                       "diff_summary": e.get("diff_summary", ""),
                       "before_hash": e.get("before_hash", "")}
                      for e in entries],
            "created_at": snap.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if snap.created_at else "",
            "verified": verify,
            "status": snap.status,
        }
    finally:
        db.close()
