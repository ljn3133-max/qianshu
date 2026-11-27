"""自定义工作流：DB 定义 → 引擎 spec。

定位：与内置工作流注册表（access/analysis/improve）隔离——本模块只服务 workflow_defs
表里的自定义定义，engine.execute 照跑（params_passthrough 透传运行参数）。

步骤安全边界：
  - 步骤只能是「白名单工具调用」：tools_read 只读工具 + change_tools 变更工具；
    白名单外的工具名一律拒绝（没有任意代码/SQL/命令步骤，定义永不携带可执行代码）
  - 变更步骤只经 engine.change_step() 建待确认工单，执行由变更确认门完成；
    预览（preview_steps）纯计划展示，不执行任何工具、不建工单
  - 敏感参数名（password/token/ak/sk 类）只允许 {{运行参数}} 占位符值——
    凭证不落定义/日志/产物（运行时填入，进工单即加密）

步骤声明：{tool, label, params_template, continue_on_fail}
  params_template 字符串值可含 {{运行参数}} 占位：纯占位（整值=一个 {{x}}）运行时
  原样代入（保类型），内嵌占位转字符串拼接；缺参数=步骤失败（可按失败策略中止后续）。
  continue_on_fail=False（默认）：本步失败 → 后续步骤全部跳过；True → 后续照跑。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.workflow_def import WorkflowDef, WorkflowDefVersion
from app.services.copilot import tools_read
from app.services.copilot.workflows import engine

MAX_STEPS = 20          # 单工作流步骤上限（防编排跑飞）
MAX_PARAMS = 20         # 单步骤参数个数上限
MAX_LABEL = 120

# 保留 key：内置工作流 + 路由段（/workflows/custom、/workflows/runs 会撞路径）
RESERVED_KEYS = frozenset({"custom", "runs", "preview"})
# 允许中划线以兼容内置工作流「另存为自定义」生成的 -copy 副本 key；
# 字母开头、不以中划线结尾、2-64 位——其余校验（保留名/内置冲突）不变
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}[a-z0-9_]$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}")
_STEP_FIELDS = frozenset({"tool", "label", "params_template", "continue_on_fail"})

# 敏感参数名口径（与 change_tools._SENSITIVE_KEYS/_SENSITIVE_SUFFIXES 同源规则：
# 精确名单 + 后缀；不用 _key 后缀防误伤 profile_key 等业务键）
_SENSITIVE_KEYS = frozenset({
    "ak", "sk", "access_key", "access_key_secret", "accesskey", "accesskey_secret",
    "secret", "secret_key", "client_secret", "password", "new_password", "old_password",
    "passwd", "pwd", "token", "api_key", "apikey", "api_secret", "kube_token",
    "initial_password", "otp_secret", "password_hash", "private_key", "ssh_key",
})
_SENSITIVE_SUFFIXES = ("_password", "_secret", "_token", "_sk")


def is_sensitive_key(key) -> bool:
    k = str(key or "").strip().lower()
    return k in _SENSITIVE_KEYS or k.endswith(_SENSITIVE_SUFFIXES)


class WorkflowDefError(ValueError):
    """定义 schema 校验不过（API 层映射 400，message 面向用户）。"""


class DefNotFound(KeyError):
    """自定义工作流不存在（API 层映射 404）。"""

    def __str__(self) -> str:   # KeyError 的 str 带引号——给用户看要干净（与 MissingParam 同口径）
        return self.args[0] if self.args else ""


class DuplicateKey(WorkflowDefError):
    """key 已存在（API 层映射 409）。"""


class MissingParam(KeyError):
    """参数模板占位符缺运行参数。"""

    def __str__(self) -> str:   # KeyError 的 str 带引号——给用户看要干净
        return self.args[0] if self.args else ""


# ---------------- 白名单（只读工具 + 变更工具） ----------------

def _change_tools():
    """变更工具注册表（lazy import；变更层不可用时不放行变更类步骤）。"""
    try:
        from app.services.copilot import change_tools
    except ImportError:
        return None
    return getattr(change_tools, "CHANGE_TOOLS", None) or None


def _change_impl():
    try:
        from app.services.copilot import change_tools
    except ImportError:
        return None
    return change_tools


def tool_kind(tool: str) -> Optional[str]:
    """tool -> 'read' / 'change'；白名单外返回 None。"""
    if tool in tools_read.tool_names():
        return "read"
    wl = _change_tools()
    if wl is not None and tool in wl:
        return "change"
    return None


def _read_params_map() -> dict:
    return {t["name"]: dict(t.get("params") or {}) for t in tools_read.catalog()}


def _builtin_keys() -> set:
    from app.services.copilot.workflows import WORKFLOWS
    return set(WORKFLOWS)


# ---------------- 参数模板：占位符解析 ----------------

def _has_placeholder(value) -> bool:
    if isinstance(value, str):
        return "{{" in value
    if isinstance(value, list):
        return any(_has_placeholder(v) for v in value)
    if isinstance(value, dict):
        return any(_has_placeholder(v) for v in value.values())
    return False


def placeholders_in_steps(steps: list) -> list:
    """步骤里引用的全部运行参数名（params_hint 契约）。"""
    names: set = set()

    def _walk(v):
        if isinstance(v, str):
            names.update(_PLACEHOLDER_RE.findall(v))
        elif isinstance(v, list):
            for x in v:
                _walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x)

    for s in steps or []:
        _walk((s or {}).get("params_template") or {})
    return sorted(names)


def resolve_value(value, params: dict):
    """模板值 -> 运行值：纯占位原样代入（保类型），内嵌占位字符串拼接，容器递归。"""
    if isinstance(value, str):
        m = re.fullmatch(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}", value.strip())
        if m:
            key = m.group(1)
            if key not in params or params[key] is None or params[key] == "":
                raise MissingParam(key)
            return params[key]

        def _sub(mm):
            key = mm.group(1)
            if key not in params or params[key] is None or params[key] == "":
                raise MissingParam(key)
            return str(params[key])

        return _PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, list):
        return [resolve_value(v, params) for v in value]
    if isinstance(value, dict):
        return {k: resolve_value(v, params) for k, v in value.items()}
    return value


def resolve_params(template, params: dict) -> dict:
    return {k: resolve_value(v, params) for k, v in (template or {}).items()}


# ---------------- 定义校验（保存/导入/预览共用） ----------------

def _check_value_shape(field: str, value) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for v in value:
            _check_value_shape(field, v)
        return
    if isinstance(value, dict):
        for v in value.values():
            _check_value_shape(field, v)
        return
    raise WorkflowDefError(f"参数 {field} 只允许字符串/数字/布尔/数组/对象，收到 {type(value).__name__}")


def _check_placeholders(field: str, value) -> None:
    """占位符格式：{{标识符}}；出现 {{ 却不构成合法占位符=格式错误。"""
    if isinstance(value, str):
        if "{{" in value:
            stripped = _PLACEHOLDER_RE.sub("", value)
            if "{{" in stripped or "}}" in stripped:
                raise WorkflowDefError(f"参数 {field} 占位符格式不对（应为 {{{{参数名}}}}）")
    elif isinstance(value, list):
        for v in value:
            _check_placeholders(field, v)
    elif isinstance(value, dict):
        for v in value.values():
            _check_placeholders(field, v)


def validate_key(key: str) -> str:
    key = str(key or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise WorkflowDefError("key 只能是小写字母/数字/下划线/中划线，字母开头、不以中划线结尾，2-64 位")
    if key in RESERVED_KEYS or key in _builtin_keys():
        raise WorkflowDefError(f"key「{key}」与内置工作流/保留路由冲突，换一个")
    return key


def validate_steps(steps) -> list:
    """步骤 schema 校验 -> 归一化有序列表。白名单外工具/未知参数/字面量凭证一律拒。"""
    if not isinstance(steps, list) or not steps:
        raise WorkflowDefError("steps 至少要有 1 个步骤")
    if len(steps) > MAX_STEPS:
        raise WorkflowDefError(f"步骤最多 {MAX_STEPS} 个（收到 {len(steps)}）")
    out: list = []
    read_params = _read_params_map()
    wl = _change_tools()
    for i, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise WorkflowDefError(f"第 {i} 步必须是对象")
        unknown = set(raw) - _STEP_FIELDS
        if unknown:
            raise WorkflowDefError(f"第 {i} 步含未知字段 {sorted(unknown)}（只接受 {sorted(_STEP_FIELDS)}）")
        tool = str(raw.get("tool") or "").strip()
        kind = tool_kind(tool)
        if kind is None:
            raise WorkflowDefError(
                f"第 {i} 步工具「{tool or '(空)'}」不在白名单内"
                f"（只允许只读工具与变更工具白名单，不支持任意代码/命令步骤）")
        label = str(raw.get("label") or "").strip() or tool
        if len(label) > MAX_LABEL:
            raise WorkflowDefError(f"第 {i} 步 label 超长（≤{MAX_LABEL} 字符）")
        cof = raw.get("continue_on_fail", False)
        if not isinstance(cof, bool):
            raise WorkflowDefError(f"第 {i} 步 continue_on_fail 必须是布尔值")
        tmpl = raw.get("params_template")
        if tmpl is None:
            tmpl = {}
        if not isinstance(tmpl, dict):
            raise WorkflowDefError(f"第 {i} 步 params_template 必须是对象")
        if len(tmpl) > MAX_PARAMS:
            raise WorkflowDefError(f"第 {i} 步参数最多 {MAX_PARAMS} 个")

        # 参数名白名单：只读工具=注册表 params 键；变更工具=params_schema properties
        if kind == "read":
            allowed = set(read_params.get(tool) or {})
            req: set = set()
            field_specs: dict = {}
        else:
            spec = (wl or {}).get(tool) or {}
            schema = spec.get("params_schema") or {}
            field_specs = dict(schema.get("properties") or {})
            allowed = set(field_specs)
            req = set(schema.get("required") or [])
        unknown_p = set(tmpl) - allowed
        if unknown_p:
            raise WorkflowDefError(
                f"第 {i} 步参数 {sorted(unknown_p)} 不在工具 {tool} 的参数表内（可选: {sorted(allowed) or '无'}）")
        missing_req = req - set(tmpl)
        if missing_req:
            raise WorkflowDefError(f"第 {i} 步缺少必填参数 {sorted(missing_req)}（工具 {tool}）")

        for k, v in tmpl.items():
            _check_value_shape(k, v)
            _check_placeholders(k, v)
            if is_sensitive_key(k) and not _has_placeholder(v):
                raise WorkflowDefError(
                    f"第 {i} 步参数「{k}」是敏感项，只能用 {{{{运行参数}}}} 占位（凭证不落定义，运行时填入）")
            # 纯字面量（无占位符）才做完整 schema 类型/枚举校验；含占位符的运行时再校验
            if kind == "change" and not _has_placeholder(v):
                impl = _change_impl()
                if impl is not None:
                    try:
                        impl.validate_params(
                            {"properties": {k: field_specs.get(k, {})}, "required": [k]}, {k: v})
                    except Exception as e:  # noqa: BLE001  白名单工具自校验口径
                        raise WorkflowDefError(f"第 {i} 步参数「{k}」不合法: {e}")
        out.append({"tool": tool, "label": label,
                    "params_template": tmpl, "continue_on_fail": cof})
    return out


def validate_meta(key: str, name: str, description: str = "", domain: str = "") -> dict:
    key = validate_key(key)
    name = str(name or "").strip()
    if not name or len(name) > 100:
        raise WorkflowDefError("name 必填且 ≤100 字符")
    description = str(description or "")
    if len(description) > 500:
        raise WorkflowDefError("description ≤500 字符")
    domain = str(domain or "").strip()
    if len(domain) > 50:
        raise WorkflowDefError("domain ≤50 字符")
    return {"key": key, "name": name, "description": description, "domain": domain}


# ---------------- DB CRUD ----------------

def _steps_of(row: WorkflowDef) -> list:
    try:
        steps = json.loads(row.steps_json or "[]")
    except ValueError:
        steps = []
    return steps


def def_out(row: WorkflowDef) -> dict:
    steps = _steps_of(row)
    return {
        "key": row.key,
        "name": row.name,
        "description": row.description or "",
        "domain": row.domain or "",
        "steps": steps,
        "enabled": bool(row.enabled),
        "version": int(row.version or 1),
        "created_by": row.created_by or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "custom": True,
        "params_hint": placeholders_in_steps(steps),
        "has_change_steps": any(tool_kind(s.get("tool") or "") == "change" for s in steps),
    }


def list_defs(db: Session) -> list:
    rows = db.query(WorkflowDef).order_by(WorkflowDef.id).all()
    return [def_out(r) for r in rows]


def get_def(db: Session, key: str) -> dict:
    row = db.query(WorkflowDef).filter(WorkflowDef.key == key).first()
    if row is None:
        raise DefNotFound(f"自定义工作流「{key}」不存在")
    return def_out(row)


# ---------------- 版本历史（只增落档、列表与一键回滚） ----------------

def _as_bool(v, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    if v is None:
        return default
    return bool(v)


def payload_of(row: WorkflowDef) -> dict:
    """定义 -> 落档/导出载荷（def_json 格式；version 由服务端管理不进载荷）。"""
    return {
        "key": row.key, "name": row.name, "description": row.description or "",
        "domain": row.domain or "", "steps": _steps_of(row), "enabled": bool(row.enabled),
    }


def _log_version(db: Session, row: WorkflowDef, *, by: str, by_id: int = 0) -> None:
    """版本落档（只增）：该 (key, version) 已有记录则跳过（幂等防重复行）。

    落档口径：
      - 每次 UPDATE 前把旧版本落档（_ensure 型调用——存量定义首改兜底）；
      - 变更/恢复后把新状态也落一行（version+1 新行，恢复同样生成新版本）——
        合起来 = 每个当过当前版的状态在历史表里恰好一行，时间线完整可回滚。
    """
    hit = (db.query(WorkflowDefVersion.id)
           .filter(WorkflowDefVersion.key == row.key,
                   WorkflowDefVersion.version == int(row.version or 1))
           .first())
    if hit is not None:
        return
    db.add(WorkflowDefVersion(
        key=row.key, version=int(row.version or 1),
        def_json=json.dumps(payload_of(row), ensure_ascii=False),
        created_by=by or row.created_by or "",
        created_by_id=by_id or 0))   # 记录操作人用户 ID，未知时保留 0


def _next_version(db: Session, key: str) -> int:
    """同 key 下一个版本号：历史表与当前行取 max + 1。

    定义删除不清历史（只增）→ 同 key 重建续谱系，(key, version) 不撞，
    删除前的版本依旧可恢复。"""
    top = (db.query(func.max(WorkflowDefVersion.version))
           .filter(WorkflowDefVersion.key == key).scalar())
    cur = (db.query(func.max(WorkflowDef.version))
           .filter(WorkflowDef.key == key).scalar())
    return int(max(int(top or 0), int(cur or 0))) + 1


def list_versions(db: Session, key: str) -> dict:
    """版本历史列表（新在前）：version/时间/操作人/摘要/def 全文 + 当前版标注。"""
    row = db.query(WorkflowDef).filter(WorkflowDef.key == key).first()
    if row is None:
        raise DefNotFound(f"自定义工作流「{key}」不存在")
    rows = (db.query(WorkflowDefVersion)
            .filter(WorkflowDefVersion.key == key)
            .order_by(WorkflowDefVersion.version.desc()).all())
    items = []
    for v in rows:
        try:
            payload = json.loads(v.def_json or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items.append({
            "version": int(v.version),
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "created_by": v.created_by or "",
            "name": str(payload.get("name") or ""),
            "steps_n": len(payload.get("steps") or []),
            "enabled": _as_bool(payload.get("enabled"), True),
            "def": payload,
            "current": int(v.version) == int(row.version or 1),
        })
    if not any(it["current"] for it in items):
        # 存量定义（功能上线前创建且未改过）无落档行：当前状态补进列表（只读展示不写库）
        items.insert(0, {
            "version": int(row.version or 1),
            "created_at": (row.updated_at or row.created_at).isoformat()
            if (row.updated_at or row.created_at) else None,
            "created_by": row.created_by or "",
            "name": row.name,
            "steps_n": len(_steps_of(row)),
            "enabled": bool(row.enabled),
            "def": payload_of(row),
            "current": True,
        })
    return {"items": items, "current_version": int(row.version or 1)}


def restore_version(db: Session, key: str, version: int, *, by: str = "", by_id: int = 0) -> dict:
    """一键回滚：恢复为 version 版本内容 → 当前 version+1 落新行（只增），审计在 API 层。

    幂等（与幂等覆盖导入同口径）：重复恢复同一版本照常执行、内容收敛不变、
    每次各落一条新版本行、绝不产生重复 (key, version)。
    历史版本需过当前 schema 复校（工具白名单可能已变化，不再合规的历史版本拒绝恢复）。
    """
    row = db.query(WorkflowDef).filter(WorkflowDef.key == key).first()
    if row is None:
        raise DefNotFound(f"自定义工作流「{key}」不存在")
    vrow = (db.query(WorkflowDefVersion)
            .filter(WorkflowDefVersion.key == key,
                    WorkflowDefVersion.version == int(version))
            .first())
    if vrow is None:
        raise DefNotFound(f"「{key}」没有 v{version} 历史版本（可恢复范围见版本历史）")
    try:
        payload = json.loads(vrow.def_json or "{}")
    except ValueError:
        raise WorkflowDefError(f"v{version} 历史存档损坏，无法恢复")
    if not isinstance(payload, dict):
        raise WorkflowDefError(f"v{version} 历史存档损坏，无法恢复")
    meta = validate_meta(row.key, str(payload.get("name") or row.name),
                         str(payload.get("description") or ""),
                         str(payload.get("domain") or ""))
    clean_steps = validate_steps(payload.get("steps"))
    _log_version(db, row, by=row.created_by or by or "", by_id=by_id)   # UPDATE 前把旧版本落档
    row.name = meta["name"]
    row.description = meta["description"]
    row.domain = meta["domain"]
    row.steps_json = json.dumps(clean_steps, ensure_ascii=False)
    row.enabled = _as_bool(payload.get("enabled"), True)
    row.version = _next_version(db, key)                    # 恢复为该版本内容 → version+1
    row.updated_at = datetime.now()
    _log_version(db, row, by=by or "", by_id=by_id)         # 新行=恢复后状态（可再回滚）
    db.commit()
    db.refresh(row)
    return {**def_out(row), "restored_from": int(version)}


def import_defs(db: Session, entries: list, *, overwrite: bool = False, by: str = "",
                by_id: int = 0) -> dict:
    """批量导入（schema 校验同 create；key 冲突 409 + overwrite 语义照抄 create_def）。

    全量先校验后写入（要么全进要么全不进，不产生部分导入）；同文件 key 重复直接拒。
    """
    if not entries:
        raise WorkflowDefError("没有可导入的工作流定义")
    clean = []
    seen: set = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise WorkflowDefError("工作流定义必须是对象")
        meta = validate_meta(str(raw.get("key") or ""), str(raw.get("name") or ""),
                             str(raw.get("description") or ""),
                             str(raw.get("domain") or ""))
        if meta["key"] in seen:
            raise WorkflowDefError(f"导入内容里 key「{meta['key']}」重复")
        seen.add(meta["key"])
        clean.append((meta, validate_steps(raw.get("steps")),
                      _as_bool(raw.get("enabled"), True)))
    conflicts = [m["key"] for m, _, _ in clean
                 if db.query(WorkflowDef).filter(WorkflowDef.key == m["key"]).first() is not None]
    if conflicts and not overwrite:
        raise DuplicateKey(f"工作流 key「{'、'.join(conflicts)}」已存在（导入覆盖请带 overwrite=true）")
    outs = []
    for meta, clean_steps, enabled in clean:
        outs.append(create_def(db, key=meta["key"], name=meta["name"],
                               description=meta["description"], domain=meta["domain"],
                               steps=clean_steps, enabled=enabled,
                               created_by=by, created_by_id=by_id, overwrite=True))
    return {"imported": len(outs), "items": outs, "overwritten": conflicts}


def create_def(db: Session, *, key: str, name: str, description: str = "", domain: str = "",
               steps, enabled: bool = True, created_by: str = "", overwrite: bool = False,
               created_by_id: int = 0) -> dict:
    """创建（幂等：overwrite=True 时同 key 覆盖更新、version+1——导入可重复执行）。

    版本落档：新定义落 v1；覆盖更新=UPDATE，先把旧版本落档再落新状态（见 _log_version）。
    """
    meta = validate_meta(key, name, description, domain)
    clean_steps = validate_steps(steps)
    existing = db.query(WorkflowDef).filter(WorkflowDef.key == meta["key"]).first()
    if existing is not None:
        if not overwrite:
            raise DuplicateKey(f"工作流 key「{meta['key']}」已存在（导入覆盖请带 overwrite=true）")
        _log_version(db, existing, by=existing.created_by or created_by or "",
                     by_id=created_by_id)
        existing.name = meta["name"]
        existing.description = meta["description"]
        existing.domain = meta["domain"]
        existing.steps_json = json.dumps(clean_steps, ensure_ascii=False)
        existing.enabled = _as_bool(enabled, True)
        existing.version = _next_version(db, meta["key"])
        existing.updated_at = datetime.now()
        _log_version(db, existing, by=created_by or "", by_id=created_by_id)
        db.commit()
        db.refresh(existing)
        return def_out(existing)
    row = WorkflowDef(
        key=meta["key"], name=meta["name"], description=meta["description"],
        domain=meta["domain"], steps_json=json.dumps(clean_steps, ensure_ascii=False),
        created_by=created_by or "", created_by_id=created_by_id or 0,   # 同时记录创建人用户 ID，未知时保留 0
        enabled=_as_bool(enabled, True),
        version=_next_version(db, meta["key"]),
    )
    db.add(row)
    _log_version(db, row, by=created_by or "", by_id=created_by_id)
    db.commit()
    db.refresh(row)
    return def_out(row)


def update_def(db: Session, key: str, *, name: Optional[str] = None,
               description: Optional[str] = None, domain: Optional[str] = None,
               steps=None, enabled: Optional[bool] = None,
               updated_by: str = "", updated_by_id: int = 0) -> dict:
    """修改（version+1）。steps 传了就整表替换（有序步骤以最新声明为准）。

    版本落档：UPDATE 前把旧版本落档（存量定义首改兜底），改完新状态落 version+1 新行。
    """
    row = db.query(WorkflowDef).filter(WorkflowDef.key == key).first()
    if row is None:
        raise DefNotFound(f"自定义工作流「{key}」不存在")
    _log_version(db, row, by=row.created_by or updated_by or "", by_id=updated_by_id)
    if name is not None or description is not None or domain is not None:
        meta = validate_meta(row.key, name if name is not None else row.name,
                             description if description is not None else row.description,
                             domain if domain is not None else row.domain)
        row.name = meta["name"]
        row.description = meta["description"]
        row.domain = meta["domain"]
    if steps is not None:
        row.steps_json = json.dumps(validate_steps(steps), ensure_ascii=False)
    if enabled is not None:
        row.enabled = bool(enabled)
    row.version = _next_version(db, key)
    row.updated_at = datetime.now()
    _log_version(db, row, by=updated_by or "", by_id=updated_by_id)
    db.commit()
    db.refresh(row)
    return def_out(row)


def delete_def(db: Session, key: str) -> dict:
    row = db.query(WorkflowDef).filter(WorkflowDef.key == key).first()
    if row is None:
        raise DefNotFound(f"自定义工作流「{key}」不存在")
    out = def_out(row)
    db.delete(row)
    db.commit()
    return out


# ---------------- 引擎接入（run_workflow 回落路径） ----------------

def _rec(ctx: dict, label: str, status: str, detail: str) -> None:
    ctx.setdefault("_wf_records", []).append(
        {"label": label, "status": status, "detail": str(detail or "")[:300]})


def _fail(ctx: dict, label: str, continue_on_fail: bool, detail: str) -> dict:
    _rec(ctx, label, engine.STEP_FAILED, detail)
    if not continue_on_fail:
        ctx["_wf_abort"] = True   # 失败策略=中止：后续步骤跳过
    return {"status": engine.STEP_FAILED, "detail": detail}


def _make_read_step(tool: str, tmpl: dict, label: str, continue_on_fail: bool):
    def _fn(db: Session, params: dict, ctx: dict) -> dict:
        if ctx.get("_wf_abort"):
            return {"status": engine.STEP_SKIPPED,
                    "detail": "前序步骤失败（失败策略=中止），本步跳过"}
        try:
            resolved = resolve_params(tmpl, params)
        except MissingParam as e:
            return _fail(ctx, label, continue_on_fail,
                         f"参数模板缺少运行参数「{e}」——运行时请补充该参数")
        try:
            out = tools_read.execute(db, tool, resolved)
        except Exception as e:  # noqa: BLE001  单步异常=failed，不裸抛
            return _fail(ctx, label, continue_on_fail, f"{type(e).__name__}: {str(e)[:200]}")
        if isinstance(out, dict) and out.get("error"):
            return _fail(ctx, label, continue_on_fail, f"工具返回错误：{str(out['error'])[:200]}")
        detail = f"{label}（只读工具 {tool}）完成：" + tools_read.result_summary(out)[:400]
        _rec(ctx, label, engine.STEP_OK, detail)
        return {"status": engine.STEP_OK, "detail": detail, "data": out}
    return _fn


def _make_change_step(tool: str, tmpl: dict, label: str, continue_on_fail: bool):
    def _fn(db: Session, params: dict, ctx: dict) -> dict:
        if ctx.get("_wf_abort"):
            return {"status": engine.STEP_SKIPPED,
                    "detail": "前序步骤失败（失败策略=中止），本步跳过"}
        try:
            resolved = resolve_params(tmpl, params)
        except MissingParam as e:
            return _fail(ctx, label, continue_on_fail,
                         f"参数模板缺少运行参数「{e}」——运行时请补充该参数")
        wl = _change_tools() or {}
        spec = wl.get(tool) or {}
        item = {
            "idx": 0,
            "title": label,
            "impact": str(spec.get("impact") or ""),
            "rollback": str(spec.get("rollback") or ""),
            "tool": tool,
            "params": resolved,
            "status": "pending",
        }
        # 红线：变更步骤只建待确认工单（engine.change_step -> change_tools.build_ticket），
        # 绝不执行变更写入；执行由用户在变更确认卡过确认门完成
        r = engine.change_step(
            db, ctx, title=label, items=[item],
            ok_detail=f"已生成待确认变更工单（{label}，工具 {tool}）——"
                      f"自定义工作流的变更步骤只建工单不执行，需在变更确认卡确认后才执行")
        if r.get("status") == engine.STEP_FAILED:
            return _fail(ctx, label, continue_on_fail, str(r.get("detail") or "变更工单创建失败"))
        _rec(ctx, label, r.get("status") or engine.STEP_OK, str(r.get("detail") or ""))
        return r
    return _fn


def _md_cell(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")[:200]


def _make_artifact(name: str):
    def build(db: Session, params: dict, ctx: dict) -> dict:
        records = ctx.get("_wf_records") or []
        tickets = ctx.get("tickets") or []
        status_map = {"ok": "成功", "failed": "失败", "skipped": "跳过"}
        lines = [f"## 自定义工作流运行报告 · {name}", "",
                 "| # | 步骤 | 状态 | 说明 |", "|---|---|---|---|"]
        for i, r in enumerate(records, 1):
            lines.append(f"| {i} | {_md_cell(r['label'])} | "
                         f"{status_map.get(r['status'], r['status'])} | {_md_cell(r['detail'])} |")
        if tickets:
            lines += ["", "### 变更工单（待确认，不自动执行）"]
            for t in tickets:
                lines.append(f"- #{t.get('ticket_id')} {_md_cell(t.get('title') or '')}"
                             f"——需在变更确认卡确认后才执行")
        safe = {k: ("****" if is_sensitive_key(k) else v) for k, v in (params or {}).items()}
        if safe:
            lines += ["", "### 运行参数"]
            for k, v in safe.items():
                lines.append(f"- {k} = {_md_cell(str(v))}")
        highlights = [
            f"{r['label']}：{status_map.get(r['status'], r['status'])}" for r in records
        ]
        for t in tickets:
            highlights.append(f"变更工单 #{t.get('ticket_id')} 已生成（待确认，不自动执行）")
        return {"report_md": "\n".join(lines),
                "highlights": highlights[:10],
                "ticket_ids": [t.get("ticket_id") for t in tickets],
                "steps_summary": [{"label": r["label"], "status": r["status"]} for r in records],
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    return build


def compile_spec(row: WorkflowDef) -> dict:
    """workflow_defs 行 -> 引擎 spec（与内置 spec 同构；params_passthrough 透传运行参数）。"""
    steps_def = _steps_of(row)
    compiled = []
    for s in steps_def:
        tool = s.get("tool") or ""
        label = s.get("label") or tool
        tmpl = s.get("params_template") or {}
        cof = bool(s.get("continue_on_fail"))
        if tool_kind(tool) == "change":
            compiled.append((label, _make_change_step(tool, tmpl, label, cof)))
        else:
            compiled.append((label, _make_read_step(tool, tmpl, label, cof)))
    return {
        "name": row.name,
        "domain": row.domain,
        "description": row.description or "",
        "params_schema": {},          # 自定义工作流无固定入参 schema（params_hint 见 def_out）
        "params_passthrough": True,   # 引擎扩展：运行参数原样透传给参数模板（只加不改内置）
        "steps": compiled,
        "build_artifact": _make_artifact(row.name),
    }


def load_spec(db: Session, key: str) -> dict:
    """run_workflow 回落：DB 定义编译成 spec。不存在/已停用 -> WorkflowNotFound。"""
    row = db.query(WorkflowDef).filter(WorkflowDef.key == key).first()
    if row is None:
        raise engine.WorkflowNotFound(f"未知工作流 {key}（内置与自定义均未找到）")
    if not row.enabled:
        raise engine.WorkflowNotFound(f"自定义工作流「{key}」已停用（启用后可运行）")
    return compile_spec(row)


# ---------------- 预览执行（纯计划展示，不执行/不建工单） ----------------

def preview_steps(steps, params: Optional[dict] = None) -> dict:
    """逐步展开"将调用什么工具、解析出什么参数、是否建工单"。
    敏感参数值掩码；缺占位参数如实标 missing_param。零副作用（不调工具、不建单）。"""
    clean_steps = validate_steps(steps)
    run_params = params or {}
    plan = []
    for i, s in enumerate(clean_steps, 1):
        kind = tool_kind(s["tool"]) or "read"
        entry = {
            "idx": i,
            "label": s["label"],
            "tool": s["tool"],
            "kind": kind,
            "continue_on_fail": s["continue_on_fail"],
            "will_ticket": kind == "change",
        }
        try:
            resolved = resolve_params(s["params_template"], run_params)
            entry["params"] = {
                k: ("****" if is_sensitive_key(k) else v) for k, v in resolved.items()
            }
            entry["status"] = "ready"
            entry["error"] = ""
        except MissingParam as e:
            entry["params"] = {}
            entry["status"] = "missing_param"
            entry["error"] = f"缺少运行参数「{e}」"
        plan.append(entry)
    return {"steps": plan, "params_hint": placeholders_in_steps(clean_steps),
            "has_change_steps": any(tool_kind(s["tool"]) == "change" for s in clean_steps)}


# ---------------- 工具目录（编辑器工具选择器） ----------------

def tool_catalog() -> list:
    """只读工具 + 变更工具两组目录（含参数名/说明/必填/枚举/敏感标记）。"""
    out = []
    for t in tools_read.catalog():
        out.append({
            "name": t["name"],
            "kind": "read",
            "desc": t.get("desc", ""),
            "params": [{"name": k, "desc": str(v), "required": False,
                        "enum": None, "type": "string",
                        "sensitive": is_sensitive_key(k)}
                       for k, v in (t.get("params") or {}).items()],
        })
    wl = _change_tools()
    if wl is not None:
        for name, spec in wl.items():
            schema = spec.get("params_schema") or {}
            props = schema.get("properties") or {}
            req = set(schema.get("required") or [])
            out.append({
                "name": name,
                "kind": "change",
                "desc": str(spec.get("title") or name),
                "impact": str(spec.get("impact") or ""),
                "params": [{"name": k, "desc": str((v or {}).get("description") or ""),
                            "required": k in req,
                            "enum": (v or {}).get("enum"),
                            "type": str((v or {}).get("type") or "string"),
                            "sensitive": is_sensitive_key(k)}
                           for k, v in props.items()],
            })
    return out
