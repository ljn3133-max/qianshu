#!/usr/bin/env python3
"""L3b 代码修改管道 mock 测试（智能体能力边界 §2）。

运行：cd /opt/qianshu/backend && python3 tests/test_code_mod_mock.py   （退出码 0=全过）
隔离性：MASTER_KEY/DATABASE_URL 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
文件系统面用临时 PROJECT_ROOT（A~F 段）+ docs/tmp/ 自建测试文件（G 段 e2e，测完即删，
不碰任何现有文件）；vite/结构指纹分支用桩脚本（快、确定），另有真实坏文件构造
（i18n locale 全量 glob 必进 vite 构建图）验证"真 vite 构建失败→验证门 fail"。

验证项：
  A 边界：allow 通过 / deny 拒（change_tools/code_mod/core 安全件/copilotd）/
    超文件数拒 / 超行数拒 / 删除拒（空 new=删除）/ requirements 拒 / .env 拒 /
    越界路径拒 / 重复路径拒 / 非可改域拒
  B 快照 before/after 落库 + 成功应用 + snapshot_out 截断口径（不吐全文）
  C 原子应用：第 2 个文件写失败 → 全部回滚到 before（hash 断言）+ 快照 rolled_back
  D verify_gate 分支：无 py / py 好 / py 坏 / vite 好 / vite 坏 / 超时 /
    结构指纹好 / 坏 / 真实坏文件→真 vite 构建失败
  E 回滚字节级还原：改内容还原 hash 一致；新建文件回滚=删除；重复回滚幂等
  F 审计：code.apply/code.rollback 落库、detail 含文件清单不落全文、ACTION_LABELS 登记
  G 真实 e2e：docs/tmp/ 自建文件走全管道（validate→snapshot→apply 改一行→verify_gate 过
    →rollback→内容字节还原→清理）
"""
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-codemod-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("codemod")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import Base, SessionLocal, engine  # noqa: E402

# 全量模型注册后再 create_all（漏注册建表会漏表）
import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")
Base.metadata.create_all(bind=engine)

from app.models.agent_code_snapshot import AgentCodeSnapshot  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.services import audit as audit_svc  # noqa: E402
from app.services.copilot import code_mod  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


USER = SimpleNamespace(id=1, username="tester")

# ============================================================
# 临时工程根（A~F 段用；G 段恢复真实根）
# ============================================================
REAL_ROOT = code_mod.PROJECT_ROOT
ROOT = Path(_TMPDIR) / "proj"
for d in ("docs/tmp", "docs/active", "frontend/src/views", "backend/app/services", "backend/app/api",
          "tests/quality", "frontend/node_modules/.bin"):
    (ROOT / d).mkdir(parents=True, exist_ok=True)


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


STUB_VITE_OK = ROOT / "frontend/node_modules/.bin/vite"
_stub(STUB_VITE_OK, "#!/bin/sh\necho vite-ok\nexit 0\n")
_stub(ROOT / "tests/quality/structure_fingerprint.py",
      "import sys\nprint('结构指纹: 0 项问题')\nsys.exit(0)\n")

code_mod.PROJECT_ROOT = ROOT

# ============================================================
# A. 边界
# ============================================================
print("== A 边界 ==")
r = code_mod.validate_plan({"files": [{"path": "docs/tmp/x.md", "new": "hello\n"}]})
check("A allow 域通过（docs/tmp/）", r["ok"], str(r))

r = code_mod.validate_plan({"files": [{"path": "docs/active/current.md", "new": "current\n"}]})
check("A allow 域通过（docs/active/）", r["ok"], str(r))

for denied_doc in ("docs/archive/history.md", "docs/x.md", "docs/quality/report.md"):
    r = code_mod.validate_plan({"files": [{"path": denied_doc, "new": "x\n"}]})
    check(f"A 文档白名单外拒绝（{denied_doc}）",
          not r["ok"] and "不在可改域" in "".join(r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": "frontend/src/views/Foo.vue", "new": "<template />\n"}]})
check("A allow 域通过（frontend/src/）", r["ok"], str(r))

for bad_path, kw in [
    ("backend/app/services/copilot/change_tools.py", "change_tools"),
    ("backend/app/services/copilot/code_mod.py", "管道本体自保护"),
    ("backend/app/core/security.py", "core 安全件"),
    ("backend/app/copilotd/runner.py", "copilotd"),
]:
    r = code_mod.validate_plan({"files": [{"path": bad_path, "new": "x\n"}]})
    check(f"A deny 拒（{kw}）", not r["ok"] and "禁改域" in "".join(r["violations"]),
          str(r))

r = code_mod.validate_plan({"files": [{"path": "requirements.txt", "new": "x\n"}]})
check("A requirements.txt 拒", not r["ok"] and ("依赖清单" in "".join(r["violations"])
      or "禁改域" in "".join(r["violations"])), str(r))

r = code_mod.validate_plan({"files": [{"path": "backend/requirements-dev.txt", "new": "x\n"}]})
check("A requirements* 拒（任意目录）", not r["ok"] and "依赖清单" in "".join(r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": ".env", "new": "x\n"}]})
check("A .env 拒", not r["ok"] and ".env" in "".join(r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": "config/.env.production", "new": "x\n"}]})
check("A .env* 拒（任意目录）", not r["ok"] and ".env" in "".join(r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": f"docs/tmp/f{i}.md", "new": "x\n"} for i in range(6)]})
check("A 超文件数拒（6>5）", not r["ok"] and any("超上限 5" in v for v in r["violations"]), str(r))

(ROOT / "docs/tmp/big.md").write_text("one line\n", encoding="utf-8")
r = code_mod.validate_plan({"files": [{"path": "docs/tmp/big.md",
                                       "new": "\n".join(f"L{i}" for i in range(250)) + "\n"}]})
check("A 超行数拒（250>200）", not r["ok"] and any("超上限 200" in v for v in r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": "docs/tmp/x.md", "new": ""}]})
check("A 删除拒（空 new=删除）", not r["ok"] and any("删除" in v for v in r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": "docs/tmp/x.md"}]})
check("A 删除拒（new 缺失）", not r["ok"] and any("删除" in v for v in r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": "../etc/passwd", "new": "x\n"}]})
check("A 越界路径拒（..）", not r["ok"], str(r))

r = code_mod.validate_plan({"files": [{"path": "/etc/passwd", "new": "x\n"}]})
check("A 绝对路径拒", not r["ok"], str(r))

r = code_mod.validate_plan({"files": [{"path": "docs/tmp/x.md", "new": "a\n"},
                                      {"path": "docs/tmp/x.md", "new": "b\n"}]})
check("A 重复路径拒", not r["ok"] and any("重复" in v for v in r["violations"]), str(r))

r = code_mod.validate_plan({"files": [{"path": "backend/app/models/user.py", "new": "x\n"}]})
check("A 非可改域拒（backend/app/models）", not r["ok"] and any("不在可改域" in v for v in r["violations"]), str(r))

r = code_mod.validate_plan({"files": []})
check("A 空清单拒", not r["ok"], str(r))

# ============================================================
# B. 快照 before/after 落库 + 成功应用 + 展示截断
# ============================================================
print("== B 快照落库与应用 ==")
before_b = "line1\nCANARY_FULLTEXT_MARKER_7f3a\nline3\nline4\nline5\n"
after_b = "line1\nline2-changed\nline3\nline4\nline5\n"
(ROOT / "docs/tmp/b.md").write_text(before_b, encoding="utf-8")
r = code_mod.snapshot_and_apply(
    42, {"files": [{"path": "docs/tmp/b.md", "new": after_b}]}, USER)
check("B 应用成功返回 snapshot_id+applied", r.get("ok") is True and r.get("applied")
      and r["applied"][0]["path"] == "docs/tmp/b.md" and r["applied"][0]["lines"] == 2,
      str(r))
sid_b = r.get("snapshot_id")
check("B 磁盘内容=after", (ROOT / "docs/tmp/b.md").read_bytes() == after_b.encode("utf-8"))

_db = SessionLocal()
snap = _db.query(AgentCodeSnapshot).filter(AgentCodeSnapshot.id == sid_b).first()
entries = json.loads(snap.files_json) if snap else []
_db.close()
check("B 快照 before/after 全文落库", snap is not None and snap.ticket_id == 42
      and snap.status == "applied" and entries
      and entries[0]["before"] == before_b and entries[0]["after"] == after_b,
      str(entries)[:200])
check("B 快照 before_hash 正确", entries and entries[0]["before_hash"] == sha(before_b.encode("utf-8")))
check("B diff_summary 含 +/- 统计", entries and entries[0]["diff_summary"].splitlines()[0] == "+1/-1",
      entries[0]["diff_summary"].splitlines()[0] if entries else "")

out = code_mod.snapshot_out(sid_b)
check("B snapshot_out 口径（无全文/有 hash）", out.get("id") == sid_b and out.get("status") == "applied"
      and out["files"] and out["files"][0]["before_hash"] == sha(before_b.encode("utf-8"))
      and "before" not in out["files"][0] and "after" not in out["files"][0]
      and out.get("verified") is None, str(out)[:200])

# diff_summary 截断：30 行改动只出前 20 行 diff
many = "\n".join(f"old{i}" for i in range(30)) + "\n"
(ROOT / "docs/tmp/many.md").write_text(many, encoding="utf-8")
r = code_mod.snapshot_and_apply(
    43, {"files": [{"path": "docs/tmp/many.md", "new": "\n".join(f"new{i}" for i in range(30)) + "\n"}]}, USER)
_db = SessionLocal()
snap2 = _db.query(AgentCodeSnapshot).filter(AgentCodeSnapshot.id == r["snapshot_id"]).first()
ds = json.loads(snap2.files_json)[0]["diff_summary"]
_db.close()
n_lines = len(ds.splitlines())
check("B diff_summary 截断（30 行改动≤22 行展示）", "+30/-30" in ds.splitlines()[0]
      and "仅展示前 20 行" in ds and n_lines <= 23, f"{n_lines} 行: {ds[:80]}")

# ============================================================
# C. 原子应用：中途写失败 → 全部回滚
# ============================================================
print("== C 原子应用 ==")
c1_before, c2_before = "C1-original\n", "C2-original\n"
(ROOT / "docs/tmp/c1.md").write_text(c1_before, encoding="utf-8")
(ROOT / "docs/tmp/c2.md").write_text(c2_before, encoding="utf-8")
_real_write = code_mod._write_atomic
_fail_once = {"docs/tmp/c2.md"}


def _faulty_write(rel, text):
    if rel in _fail_once:
        _fail_once.discard(rel)
        raise OSError("模拟磁盘写失败")
    return _real_write(rel, text)


code_mod._write_atomic = _faulty_write
r = code_mod.snapshot_and_apply(
    44, {"files": [{"path": "docs/tmp/c1.md", "new": "C1-changed\n"},
                   {"path": "docs/tmp/c2.md", "new": "C2-changed\n"}]}, USER)
code_mod._write_atomic = _real_write
check("C 写失败返回 ok=False", r.get("ok") is False and r.get("snapshot_id"), str(r)[:200])
check("C 已写文件回滚到 before（字节级）",
      (ROOT / "docs/tmp/c1.md").read_bytes() == c1_before.encode("utf-8")
      and (ROOT / "docs/tmp/c2.md").read_bytes() == c2_before.encode("utf-8"))
check("C 回滚清单完整", r.get("restored") and len(r["restored"]) == 2
      and all(x["action"] == "restored" for x in r["restored"]), str(r.get("restored")))
_db = SessionLocal()
snap_c = _db.query(AgentCodeSnapshot).filter(AgentCodeSnapshot.id == r["snapshot_id"]).first()
_db.close()
check("C 快照标 rolled_back", snap_c is not None and snap_c.status == "rolled_back")

# ============================================================
# D. verify_gate 分支
# ============================================================
print("== D 验证门 ==")
r = code_mod.verify_gate(["docs/tmp/b.md"])
check("D 无 py 文件→仅 py_compile 检查且过", r["passed"] and len(r["results"]) == 1
      and r["results"][0]["check"] == "py_compile" and r["results"][0]["ok"], str(r))

(ROOT / "docs/tmp/good.py").write_text("x = 1\n", encoding="utf-8")
r = code_mod.verify_gate(["docs/tmp/good.py"])
check("D py 好→过", r["passed"] and r["results"][0]["ok"], str(r))

(ROOT / "docs/tmp/bad.py").write_text("def broken(:\n", encoding="utf-8")
r = code_mod.verify_gate(["docs/tmp/bad.py"])
check("D py 坏→fail", not r["passed"] and not r["results"][0]["ok"]
      and "bad.py" in r["results"][0]["detail"], str(r))

# vite 好/坏/超时（桩脚本，确定性）
_stub(STUB_VITE_OK, "#!/bin/sh\necho vite-ok\nexit 0\n")
r = code_mod.verify_gate(["frontend/src/views/Foo.vue"])
check("D vite 好→过", r["passed"] and any(x["check"] == "vite_build" and x["ok"] for x in r["results"]), str(r))

_stub(STUB_VITE_OK, "#!/bin/sh\necho 'Unexpected token' >&2\nexit 1\n")
r = code_mod.verify_gate(["frontend/src/views/Foo.vue"])
check("D vite 坏→fail", not r["passed"] and any(
    x["check"] == "vite_build" and not x["ok"] and "退出码 1" in x["detail"] for x in r["results"]), str(r))

_stub(STUB_VITE_OK, "#!/bin/sh\nsleep 30\nexit 0\n")
_real_timeout = code_mod.VERIFY_TIMEOUT
code_mod.VERIFY_TIMEOUT = 2
r = code_mod.verify_gate(["frontend/src/views/Foo.vue"])
code_mod.VERIFY_TIMEOUT = _real_timeout
check("D vite 超时→fail", not r["passed"] and any(
    x["check"] == "vite_build" and not x["ok"] and "超时" in x["detail"] for x in r["results"]), str(r))

# 结构指纹好/坏（桩脚本）
_stub(STUB_VITE_OK, "#!/bin/sh\nexit 0\n")
r = code_mod.verify_gate(["backend/app/services/foo.py"])
check("D 结构指纹好→过（含 py_compile）", r["passed"]
      and any(x["check"] == "structure_fingerprint" and x["ok"] for x in r["results"])
      and any(x["check"] == "py_compile" and x["ok"] for x in r["results"]), str(r))

_stub(ROOT / "tests/quality/structure_fingerprint.py",
      "import sys\nprint('结构指纹: 3 项问题')\nsys.exit(1)\n")
r = code_mod.verify_gate(["backend/app/services/foo.py"])
check("D 结构指纹坏→fail", not r["passed"] and any(
    x["check"] == "structure_fingerprint" and not x["ok"] for x in r["results"]), str(r))

# 真实坏文件构造→真 vite 构建失败（i18n locale glob 必进构建图），测完即删
bad_fe = REAL_ROOT / "frontend/src/i18n/locales/zh-CN/__codemod_bad__.js"
code_mod.PROJECT_ROOT = REAL_ROOT
try:
    bad_fe.write_text("this is { not valid javascript\n", encoding="utf-8")
    r = code_mod.verify_gate(["frontend/src/i18n/locales/zh-CN/__codemod_bad__.js"])
    check("D 真实坏文件→真 vite build 失败→fail", not r["passed"] and any(
        x["check"] == "vite_build" and not x["ok"] for x in r["results"]), str(r)[:300])
finally:
    if bad_fe.exists():
        bad_fe.unlink()
code_mod.PROJECT_ROOT = ROOT
check("D 真实坏文件已清理", not bad_fe.exists())

# ============================================================
# E. 回滚字节级还原 + 新建文件回滚=删除 + 幂等
# ============================================================
print("== E 回滚 ==")
e_before = "E-original-content\n第二行\n"
(ROOT / "docs/tmp/e.md").write_text(e_before, encoding="utf-8")
h_before = sha((ROOT / "docs/tmp/e.md").read_bytes())
r = code_mod.snapshot_and_apply(45, {"files": [{"path": "docs/tmp/e.md", "new": "E-changed\n"}]}, USER)
sid_e = r["snapshot_id"]
r = code_mod.auto_rollback(sid_e, USER)
check("E auto_rollback 成功+verify 确认", r.get("ok") is True and r.get("verify", {}).get("passed") is True,
      str(r)[:300])
check("E 字节级还原（hash 断言）", sha((ROOT / "docs/tmp/e.md").read_bytes()) == h_before)

r = code_mod.rollback(sid_e, USER)
check("E 重复回滚幂等", r.get("ok") is True and r.get("already") is True and r["restored"] == [], str(r))

# 新建文件：应用→存在；回滚→删除还原现场
r = code_mod.snapshot_and_apply(46, {"files": [{"path": "docs/tmp/created.md", "new": "new file\n"}]}, USER)
check("E 新建文件应用成功", r.get("ok") is True and (ROOT / "docs/tmp/created.md").exists(), str(r)[:200])
sid_n = r["snapshot_id"]
r = code_mod.auto_rollback(sid_n, USER)
check("E 新建文件回滚=删除", r.get("ok") is True and not (ROOT / "docs/tmp/created.md").exists()
      and r["restored"] and r["restored"][0]["action"] == "removed", str(r)[:300])

# ============================================================
# F. 审计
# ============================================================
print("== F 审计 ==")
_db = SessionLocal()
rows = _db.query(AuditLog).filter(AuditLog.action.in_(["code.apply", "code.rollback"])).all()
_db.close()
applies = [x for x in rows if x.action == "code.apply"]
rollbacks = [x for x in rows if x.action == "code.rollback"]
check("F code.apply/code.rollback 审计落库", applies and rollbacks,
      f"apply={len(applies)} rollback={len(rollbacks)}")
apply_ok = next((x for x in applies if x.result == "ok"), None)
detail_text = apply_ok.detail if apply_ok else ""
check("F apply 审计含文件清单", apply_ok is not None and "docs/tmp/b.md" in detail_text, detail_text[:200])
check("F apply 审计不落全文（canary 不出现）",
      "CANARY_FULLTEXT_MARKER_7f3a" not in "".join(x.detail or "" for x in rows))
check("F rollback 审计带 trigger", any('"trigger"' in (x.detail or "") for x in rollbacks))
check("F ACTION_LABELS 登记 code.apply/code.rollback",
      "code.apply" in audit_svc.ACTION_LABELS and "code.rollback" in audit_svc.ACTION_LABELS)
check("F 审计中文标签可读", audit_svc.ACTION_LABELS["code.apply"].startswith("应用代码变更"))

# ============================================================
# G. 真实 e2e：docs/tmp/ 自建文件全管道（真实 PROJECT_ROOT）
# ============================================================
print("== G 真实 e2e（docs/tmp/ 自建文件）==")
code_mod.PROJECT_ROOT = REAL_ROOT
ts = str(int(time.time()))
e2e_md = REAL_ROOT / f"docs/tmp/__codemod_e2e_{ts}.md"
e2e_py = REAL_ROOT / f"docs/tmp/__codemod_e2e_{ts}.py"
md_before = f"# e2e fixture {ts}\nline-two\nline-three\n"
py_before = f"E2E_TS = {ts}  # e2e fixture\ndef fixture():\n    return 1\n"
try:
    e2e_md.write_text(md_before, encoding="utf-8")
    e2e_py.write_text(py_before, encoding="utf-8")
    md_path = f"docs/tmp/__codemod_e2e_{ts}.md"
    py_path = f"docs/tmp/__codemod_e2e_{ts}.py"

    plan = {"files": [{"path": md_path,
                       "new": md_before.replace("line-two", "line-two-modified")},
                      {"path": py_path,
                       "new": py_before.replace("return 1", "return 2")}]}
    r = code_mod.validate_plan(plan)
    check("G1 validate 过（真实根，docs/tmp/ 可改域）", r["ok"], str(r))

    r = code_mod.snapshot_and_apply(99, plan, USER)
    check("G2 snapshot+apply 成功", r.get("ok") is True and len(r.get("applied", [])) == 2, str(r)[:200])
    sid_g = r.get("snapshot_id")
    check("G3 磁盘=after（改一行生效）",
          "line-two-modified" in e2e_md.read_text(encoding="utf-8")
          and "return 2" in e2e_py.read_text(encoding="utf-8"))

    r = code_mod.verify_gate([md_path, py_path], snapshot_id=sid_g)
    check("G4 verify_gate 过（含真实 py_compile）", r["passed"]
          and any(x["check"] == "py_compile" and x["ok"] and "1/1" in x["detail"] for x in r["results"]),
          str(r))

    out = code_mod.snapshot_out(sid_g)
    check("G5 snapshot_out 含 verified 结果", out.get("verified") and out["verified"].get("passed") is True
          and out.get("status") == "applied", str(out)[:200])

    r = code_mod.rollback(sid_g, USER)
    check("G6 rollback 成功", r.get("ok") is True and r.get("verify", {}).get("passed") is True, str(r)[:200])
    check("G7 内容字节还原（hash 断言）",
          sha(e2e_md.read_bytes()) == sha(md_before.encode("utf-8"))
          and sha(e2e_py.read_bytes()) == sha(py_before.encode("utf-8")))
finally:
    for f in (e2e_md, e2e_py):
        if f.exists():
            f.unlink()
check("G8 e2e 自建文件已清理", not e2e_md.exists() and not e2e_py.exists())

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
shutil.rmtree(_TMPDIR, ignore_errors=True)
sys.exit(1 if FAIL else 0)
