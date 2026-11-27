"""变更工具白名单注册表。

智能体变更只能使用本注册表中的结构化工具，每个工具都是现有 API/服务的封装
（handler 直接调 api/v1 里的既有端点函数，不复制业务逻辑）；不提供任意
SQL/HTTP/shell 工具，LLM 产出只作为参数走 schema 校验，绝不执行原文。

每个工具：
  title/impact/rollback   变更清单卡三要素（改什么/影响面/回滚点，人话）
  params_schema           JSON-Schema 风格参数契约（validate_params 强校验，未知参数拒绝）
  ack_required            True / callable(params)——凭证类变更须「我已知晓」警示确认
  target_type/ref_fn      快照定位（agent_change_snapshots.target_type/target_ref）
  snapshot_fn(db, params) 变更前状态（新建=空 dict {}；存在={"exists": True, ...}）
  handler(db, params, user, ack)  执行（复用 API 函数；ack 透传给 _require_ack 语义）
  rollback_fn(db, params, before, after, user)  逆向写回（新建=删除，改=恢复 before）

build_ticket()：把计划落成 pending 工单（change_list_json 含
idx/title/impact/rollback/tool/params/ack_required/sensitive/status/result）——**只建单不执行**，
执行必须走 change_gate.execute_ticket 的确认门。
凭证口径：create/update 云账号复用 clouds.py 的 _require_ack（CRED_WARN_VERSION/
ACK_TEXT/cloud.account.ack 审计），不在本模块另立口径。

敏感参数落库：password/AK 类参数**加密入 change_list_json**
（app.core.security.encrypt_secret，enc:v1: 前缀标记），ticket_out 展示层掩码（****），
执行/回滚前 decrypt_params 还原，工单表不得保存明文凭证。
change item 带 sensitive 标记（凭证/密码/权限/删除类=True）；sensitive 项强制
ack_required（敏感二次确认）。check_fn(params, user) 为可选构建期语义校验
（白名单级拒绝，如 platform_api_call 黑名单/越权）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.models.agent_change import AgentChangeTicket

MAX_ITEMS = 5   # 单工单变更项上限（防计划跑飞）


class ChangeToolError(ValueError):
    """变更工具参数/白名单校验失败（面向用户可展示）。"""


# ---------------- 参数 schema 校验（白名单工具专用） ----------------

def validate_params(schema: dict, params: dict) -> dict:
    """按 params_schema 校验并归一参数。

    - 未知参数直接拒（防 LLM 夹带）；缺必填拒；类型不符拒（数值/布尔容忍字符串形态）；
    - array+items 递归校验元素（对象元素按 item schema）。
    返回归一后的 params（原对象不改）。
    """
    if not isinstance(params, dict):
        raise ChangeToolError("params 必须是对象")
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    unknown = set(params) - set(props)
    if unknown:
        raise ChangeToolError(f"未知参数 {sorted(unknown)}（只接受 {sorted(props)}）")
    out: dict = {}
    for key, spec in props.items():
        if key in params:
            out[key] = _check_value(key, spec, params[key])
        elif key in required:
            raise ChangeToolError(f"缺少必填参数: {key}")
    return out


def _check_value(key: str, spec: dict, value):
    t = spec.get("type", "string")
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        raise ChangeToolError(f"参数 {key} 只能是 {enum}，收到 {value!r}")
    if t == "string":
        if not isinstance(value, str):
            raise ChangeToolError(f"参数 {key} 必须是字符串")
        return value
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, str)) or (
                isinstance(value, str) and not value.strip().lstrip("-").isdigit()):
            raise ChangeToolError(f"参数 {key} 必须是整数")
        return int(value)
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ChangeToolError(f"参数 {key} 必须是数值")
        try:
            return float(value) if isinstance(value, str) else value
        except ValueError:
            raise ChangeToolError(f"参数 {key} 必须是数值")
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ChangeToolError(f"参数 {key} 必须是布尔值")
    if t == "array":
        if not isinstance(value, list):
            raise ChangeToolError(f"参数 {key} 必须是数组")
        item_spec = spec.get("items")
        if item_spec:
            return [_check_value(f"{key}[{i}]", item_spec, v) for i, v in enumerate(value)]
        return list(value)
    if t == "object":
        if not isinstance(value, dict):
            raise ChangeToolError(f"参数 {key} 必须是对象")
        if spec.get("properties"):
            return validate_params(spec, value)
        return dict(value)
    return value


# ---------------- 敏感参数加密落库（password/AK 类，enc:v1: 前缀标记） ----------------

ENC_PREFIX = "enc:v1:"

# 按键名识别敏感值：精确名单 + 后缀（_password/_secret/_token/_sk）。
# 不用 _key 后缀——profile_key/key_value 等业务键不是凭证，不能误伤。
_SENSITIVE_KEYS = frozenset({
    "ak", "sk", "access_key", "access_key_secret", "accesskey", "accesskey_secret",
    "secret", "secret_key", "client_secret", "password", "new_password", "old_password",
    "passwd", "pwd", "token", "api_key", "apikey", "api_secret", "kube_token",
    "initial_password", "otp_secret", "password_hash", "private_key", "ssh_key",
})
_SENSITIVE_SUFFIXES = ("_password", "_secret", "_token", "_sk")


def _is_sensitive_key(key) -> bool:
    k = str(key or "").strip().lower()
    return k in _SENSITIVE_KEYS or k.endswith(_SENSITIVE_SUFFIXES)


def _transform(value, leaf_fn, key=None):
    if isinstance(value, dict):
        return {k: _transform(v, leaf_fn, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_transform(v, leaf_fn, key=key) for v in value]
    if isinstance(value, str) and key is not None and _is_sensitive_key(key) and value:
        return leaf_fn(value)
    return value


def _enc_leaf(v: str):
    return v if v.startswith(ENC_PREFIX) else ENC_PREFIX + _encrypt(v)


def _dec_leaf(v: str):
    if not v.startswith(ENC_PREFIX):
        return v   # 老工单明文兼容：原样返回
    try:
        return _decrypt(v[len(ENC_PREFIX):])
    except Exception:   # noqa: BLE001  解不开按原样返回（不阻断执行链路排查）
        return v


def _encrypt(plaintext: str) -> str:
    from app.core.security import encrypt_secret

    return encrypt_secret(plaintext)


def _decrypt(cipher_b64: str) -> str:
    from app.core.security import decrypt_secret

    return decrypt_secret(cipher_b64)


def encrypt_params(params: dict) -> dict:
    """敏感字段加密（深拷贝，入 change_list_json/快照外存储用）。"""
    return _transform(dict(params or {}), _enc_leaf)


def decrypt_params(params: dict) -> dict:
    """敏感字段解密（执行/回滚前还原；老明文行兼容原样返回）。"""
    return _transform(dict(params or {}), _dec_leaf)


def mask_params(params: dict) -> dict:
    """敏感字段掩码（展示层；**掩码不解密**，密文也不回显）。"""
    return _transform(dict(params or {}), lambda v: "****")


# 自由文本凭据掩码：工单标题和审计 detail 可能包含凭据，params 加密无法覆盖这些字段。
_SECRET_KV_RE = re.compile(
    r"((?:密码|口令|password|passwd|pwd|secret|token|access[_-]?key|ak|sk)"
    r"\s*[:：=]\s*|（(?:密码|口令)\s*|[\(（](?:password|passwd|pwd)\s*)(\S{3,})", re.I)


def mask_secrets_text(text) -> str:
    """标题/摘要等自由文本落库前调用：凭据值一律掩码，明文绝不进工单/审计。"""
    if not text:
        return ""
    s = str(text)

    def _rep(m):
        head, val = m.group(1), m.group(2)
        # 无显式分隔符时，中文叙述（"密码策略要求…"）不掩，凭据形态（含数字/符号、无中文）才掩
        if not re.search(r"[:：=（(]", head) and re.search(r"[一-鿿]", val):
            return m.group(0)
        return head + "****"

    s = _SECRET_KV_RE.sub(_rep, s)
    s = re.sub(r"LTAI[A-Za-z0-9]{6,}", "LTAI****", s)          # 阿里云 AK 形态
    s = re.sub(r"\b[A-Za-z0-9_\-]{28,}\b", "****", s)           # 长随机串（token/SK 形态）
    return s


def item_params(item: dict) -> dict:
    """变更项参数（执行/回滚用）：落库形态 → 明文形态。"""
    return decrypt_params(item.get("params") or {})


def secure_store(obj):
    """工单落库快照：敏感叶子加密（深拷贝，不改内存对象——响应仍回显明文一次）。"""
    return _transform(obj, _enc_leaf)


def mask_out(obj):
    """出参脱敏：敏感叶子掩码（ticket_out 用）。"""
    return _transform(obj, lambda v: "****")


def _gen_password() -> str:
    """一次性初始密码：复用 app.core.security.generate_password，与重置密码 API 同源。"""
    from app.core.security import generate_password

    return generate_password()


# ---------------- 快照（变更前/后状态，凭证永不入快照） ----------------

def _dt(v) -> str:
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else (v or "")


def _snap_cloud_account(db: Session, params: dict) -> dict:
    from app.models.cloud_account import CloudAccount

    row = None
    if params.get("name"):
        row = db.query(CloudAccount).filter(CloudAccount.name == params["name"]).first()
    if row is None and params.get("account_id"):
        row = db.get(CloudAccount, int(params["account_id"]))
    if row is None:
        return {}
    try:
        extra = json.loads(row.extra_json or "{}")
    except ValueError:
        extra = {}
    return {
        "exists": True, "id": row.id, "name": row.name, "vendor": row.vendor,
        "region": row.region, "extra": extra, "enabled": bool(row.enabled),
        "status": row.status,
    }   # ak/sk 密文不入快照（凭证只进不出）


def _snap_cloud_sync(db: Session, params: dict) -> dict:
    """同步工具的快照 = 资产台账计数（同步前后对比；同步是幂等采集，无对象可回滚）。"""
    from sqlalchemy import func
    from app.models.asset import Asset

    total = db.query(func.count(Asset.id)).scalar() or 0
    by_product = dict(db.query(Asset.product, func.count(Asset.id))
                      .group_by(Asset.product).all())
    return {"exists": True, "asset_total": int(total),
            "by_product": {str(k): int(v) for k, v in by_product.items()}}


def _snap_ownership_rule(db: Session, params: dict) -> dict:
    from app.models.ownership import OwnershipRule
    from app.services.ownership import norm_key

    value = norm_key(params.get("key_type", ""), params.get("key_value", ""))
    row = (db.query(OwnershipRule)
           .filter(OwnershipRule.key_type == params.get("key_type"),
                   OwnershipRule.key_value == value).first())
    if row is None:
        return {}
    return {
        "exists": True, "id": row.id, "key_type": row.key_type, "key_value": row.key_value,
        "match_type": row.match_type, "vendor": row.vendor, "account_name": row.account_name,
        "account_id": row.account_id, "business": row.business, "note": row.note,
    }


def _snap_profile_known(db: Session, params: dict) -> dict:
    from app.models.behavior_profile import BehaviorProfile

    row = (db.query(BehaviorProfile)
           .filter(BehaviorProfile.profile_key == params.get("profile_key")).first())
    if row is None:
        return {}
    return {
        "exists": True, "id": row.id, "profile_key": row.profile_key, "status": row.status,
        "known_at": _dt(row.known_at), "silence_until": _dt(row.silence_until),
        "notify_state": row.notify_state or "",
    }


def _snap_alert_rule(db: Session, params: dict) -> dict:
    from app.models.alert_rule import AlertRule

    row = db.query(AlertRule).filter(AlertRule.name == params.get("name")).first()
    if row is None:
        return {}
    return {
        "exists": True, "id": row.id, "name": row.name, "source": row.source,
        "level": row.level, "product": row.product, "metric": row.metric,
        "project": row.project, "logstore": row.logstore, "query_expr": row.query_expr,
        "comparator": row.comparator, "threshold": row.threshold, "duration": row.duration,
        "channel_ids": json.loads(row.channel_ids or "[]"),
        "silences": json.loads(row.silences or "[]"),
        "enabled": bool(row.enabled),
    }


def _snap_expiry_watch(db: Session, params: dict) -> dict:
    from app.models.asset import Asset

    domain = (params.get("domain") or "").strip().lower()
    row = (db.query(Asset)
           .filter(Asset.product == "domain", Asset.instance_id == domain).first())
    if row is None:
        return {}
    return {
        "exists": True, "id": row.id, "instance_id": row.instance_id, "name": row.name,
        "status": row.status, "expire_time": _dt(row.expire_time), "extra": row.extra or "",
    }


def _snap_biz_map(db: Session, params: dict) -> dict:
    from app.models.business import BizIdentifier, Business

    row = db.query(Business).filter(Business.name == params.get("name")).first()
    if row is None:
        return {}
    idents = (db.query(BizIdentifier)
              .filter(BizIdentifier.business_id == row.id)
              .order_by(BizIdentifier.id).all())
    return {
        "exists": True, "id": row.id, "name": row.name, "code": row.code,
        "description": row.description, "status": row.status,
        "identifiers": [{"id": i.id, "ident_type": i.ident_type,
                         "ident_value": i.ident_value, "note": i.note or ""} for i in idents],
    }


def _snap_edge_status(db: Session, params: dict) -> dict:
    from app.models.link_edge import LinkEdge

    ids = [int(i) for i in (params.get("ids") or [])]
    rows = (db.query(LinkEdge).filter(LinkEdge.id.in_(ids)).order_by(LinkEdge.id).all()
            if ids else [])
    return {
        "exists": True,
        "edges": [{"id": r.id, "host": r.host, "status": r.status, "note": r.note or "",
                   "confirmed_by": r.confirmed_by or "",
                   "confirmed_by_id": getattr(r, "confirmed_by_id", 0) or 0,
                   "confirmed_at": _dt(r.confirmed_at)} for r in rows],
    }


def _snap_inspection_rule(db: Session, params: dict) -> dict:
    from app.core.config import settings

    return {
        "exists": True,
        "health_warn": float(settings.INSPECTION_HEALTH_WARN),
        "health_crit": float(settings.INSPECTION_HEALTH_CRIT),
        "idle_cpu": float(settings.INSPECTION_IDLE_CPU),
        "idle_qps": float(settings.INSPECTION_IDLE_QPS),
        "expiry_days": int(settings.INSPECTION_EXPIRY_DAYS),
    }


def _snap_dashboard_cards(db: Session, params: dict) -> dict:
    from app.core.config import settings

    try:
        rows = json.loads(settings.PRODUCT_CARDS or "[]")
    except ValueError:
        rows = []
    cards = [{"key": r[0], "label": r[1], "icon": r[2], "color": r[3]}
             for r in rows if isinstance(r, (list, tuple)) and len(r) >= 4]
    return {"exists": True, "cards": cards}


def _snap_user(db: Session, params: dict) -> dict:
    from app.models.user import User

    row = _find_user(db, params)
    if row is None:
        return {}
    return {
        "exists": True, "id": row.id, "username": row.username,
        "display_name": row.display_name or "", "role": row.role,
        "is_active": bool(row.is_active),
        "must_change_password": bool(row.must_change_password),
    }   # password_hash 永不入快照（凭证只进不出）


def _snap_permission_policy(db: Session, params: dict) -> dict:
    from app.models.permission import PermissionPolicy

    row = (db.query(PermissionPolicy)
           .filter(PermissionPolicy.user_id == int(params.get("user_id") or 0),
                   PermissionPolicy.code == params.get("code")).first())
    if row is None:
        return {}
    return {"exists": True, "id": row.id, "user_id": row.user_id,
            "code": row.code, "effect": row.effect}


def _snap_platform_call(db: Session, params: dict) -> dict:
    """通用平台调用的尽力快照：同路径 GET 现场（拿不到就只留路径与说明）。"""
    from app.services.copilot import platform_api

    method = str(params.get("method") or "").upper()
    path = str(params.get("path") or "")
    before_get = platform_api.try_get(path, db=db, user=None) if method != "GET" else None
    return {"exists": True, "method": method, "path": path,
            "before_get": before_get,
            "note": "通用平台调用快照=同路径 GET 现场（尽力而为），无对象级 before/after"}


def _find_user(db: Session, params: dict):
    from app.models.user import User

    row = None
    if params.get("user_id"):
        row = db.get(User, int(params["user_id"]))
    if row is None and params.get("username"):
        row = db.query(User).filter(User.username == params["username"]).first()
    return row


# ---------------- 执行（封装现有 API 端点函数） ----------------

def _req() -> SimpleNamespace:
    """API 函数的 request 占位（client_ip 记 copilot，区别于浏览器操作）。"""
    return SimpleNamespace(client=SimpleNamespace(host="copilot"))


def _h_create_cloud_account(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.clouds import AccountIn, create_account

    body = AccountIn(
        name=params["name"], vendor=params["vendor"],
        ak=params.get("ak", ""), sk=params.get("sk", ""),
        region=params.get("region", ""), extra=params.get("extra") or {},
        enabled=bool(params.get("enabled", True)),
        ack=ack,   # 复用 _require_ack 语义（cloud.account.ack 审计）
    )
    out = create_account(body, _req(), db, user)
    return {"ref": out.get("name", params["name"]), "result": out}


def _h_update_cloud_account(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.clouds import AccountPatchIn, update_account

    body = AccountPatchIn(
        name=params.get("name"), ak=params.get("ak", ""), sk=params.get("sk", ""),
        region=params.get("region"), extra=params.get("extra"),
        enabled=params.get("enabled"), ack=ack,
    )
    out = update_account(int(params["account_id"]), body, _req(), db, user)
    return {"ref": out.get("name", str(params["account_id"])), "result": out}


def _h_sync_cloud(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.clouds import sync_cloud_assets

    out = sync_cloud_assets(
        _req(), account_id=int(params.get("account_id") or 0),
        product=str(params.get("product") or ""), db=db, user=user)
    return {"ref": f"account:{int(params.get('account_id') or 0)}/{params.get('product') or '*'}",
            "result": out}


def _h_save_ownership_rule(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.ownership import OwnershipRuleIn, create_rule, update_rule
    from app.models.ownership import OwnershipRule
    from app.services.ownership import norm_key

    body = OwnershipRuleIn(
        key_type=params["key_type"], key_value=params["key_value"],
        match_type=params.get("match_type", "exact"), vendor=params.get("vendor", ""),
        account_name=params.get("account_name", ""), account_id=params.get("account_id", 0),
        business=params.get("business", ""), note=params.get("note", ""),
    )
    value = norm_key(body.key_type, body.key_value)
    existing = (db.query(OwnershipRule)
                .filter(OwnershipRule.key_type == body.key_type,
                        OwnershipRule.key_value == value).first())
    if existing is not None:
        out = update_rule(existing.id, body, _req(), db, user)
    else:
        out = create_rule(body, _req(), db, user)
    return {"ref": f"{body.key_type}:{value}", "result": out}


def _h_mark_profile_known(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.profiles import know

    out = know(_req(), profile_key=params["profile_key"],
               silence_3d=bool(params.get("silence_3d", False)), db=db, user=user)
    return {"ref": params["profile_key"], "result": out}


def _h_save_alert_rule(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.alerts import create_rule, update_rule
    from app.models.alert_rule import AlertRule
    from app.schemas.alerts import RuleCreate, RuleUpdate

    name = params["name"]
    fields = {k: params[k] for k in (
        "name", "source", "level", "product", "metric", "project", "logstore",
        "query_expr", "comparator", "threshold", "duration", "channel_ids",
        "silences", "enabled") if k in params}
    existing = db.query(AlertRule).filter(AlertRule.name == name).first()
    if existing is not None:
        out = update_rule(existing.id, RuleUpdate(**fields), _req(), db, user)
    else:
        out = create_rule(RuleCreate(**fields), _req(), db, user)
    return {"ref": name, "result": out}


def _h_save_expiry_watch(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.expiry import ExternalDomainIn, register_external_domain

    out = register_external_domain(
        ExternalDomainIn(domain=params["domain"], expire_time=params["expire_time"],
                         note=params.get("note", "")),
        _req(), db, user)
    return {"ref": params["domain"], "result": out}


def _h_save_biz_map(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.link import add_identifier, confirm_business_api, create_business, update_business
    from app.models.business import Business
    from app.schemas.link import BusinessIn, IdentifierIn

    name = params["name"]
    body = BusinessIn(name=name, code=params.get("code", ""),
                      description=params.get("description", ""))
    existing = db.query(Business).filter(Business.name == name).first()
    if existing is not None:
        biz_out = update_business(existing.id, body, _req(), db, user)
        if existing.status == "draft":
            # 此入口只在变更工单获人工确认后执行；保存权威映射必须同时解除草稿门。
            biz_out = confirm_business_api(existing.id, _req(), db, user)["business"]
    else:
        biz_out = create_business(body, _req(), db, user)
    business_id = int(biz_out.get("id") or 0)
    added = []
    for ident in params.get("identifiers") or []:
        ibody = IdentifierIn(ident_type=ident["ident_type"],
                             ident_value=ident["ident_value"],
                             note=ident.get("note", ""))
        try:
            r = add_identifier(business_id, ibody, _req(), db, user)
            added.append(r)
        except Exception as e:   # 幂等：重复标识跳过不阻断（409 等）
            added.append({"ident_type": ibody.ident_type, "ident_value": ibody.ident_value,
                          "skipped": f"{type(e).__name__}: {str(e)[:100]}"})
    return {"ref": name, "result": {"business": biz_out, "identifiers": added}}


def _h_save_edge_status(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.link import set_edge_status
    from app.schemas.link import EdgeStatusIn

    out = set_edge_status(
        EdgeStatusIn(ids=[int(i) for i in params["ids"]],
                     status=params["status"], note=params.get("note") or ""),
        _req(), db, user)
    return {"ref": f"边{len(params['ids'])}条/{params['status']}", "result": out}


# 巡检规则 = 巡检阈值配置（env 持久化，settings 热生效；与 improve.py 同源）
INSPECTION_RULE_SETTINGS = {
    "health_warn": ("INSPECTION_HEALTH_WARN", float),   # 使用率预警阈值 %
    "health_crit": ("INSPECTION_HEALTH_CRIT", float),   # 使用率严重阈值 %
    "idle_cpu": ("INSPECTION_IDLE_CPU", float),         # 闲置 CPU 阈值 %
    "idle_qps": ("INSPECTION_IDLE_QPS", float),         # 无流量 QPS 阈值
    "expiry_days": ("INSPECTION_EXPIRY_DAYS", int),     # 到期预警天数
}


def _check_inspection_rule(params: dict, user) -> None:
    if not any(k in params for k in INSPECTION_RULE_SETTINGS):
        raise ChangeToolError("未给出任何巡检阈值参数（health_warn/health_crit/idle_cpu/idle_qps/expiry_days）")
    for k in ("health_warn", "health_crit", "idle_cpu"):
        if k in params and not (0 < float(params[k]) <= 100):
            raise ChangeToolError(f"{k} 取值须在 (0,100]，收到 {params[k]}")
    if "idle_qps" in params and float(params["idle_qps"]) < 0:
        raise ChangeToolError(f"idle_qps 不能为负，收到 {params['idle_qps']}")
    if "expiry_days" in params and not (1 <= int(params["expiry_days"]) <= 365):
        raise ChangeToolError(f"expiry_days 取值须在 [1,365]，收到 {params['expiry_days']}")
    if "health_warn" in params and "health_crit" in params \
            and float(params["health_warn"]) >= float(params["health_crit"]):
        raise ChangeToolError("health_warn 必须小于 health_crit（预警先于严重）")


def _h_save_inspection_rule(db: Session, params: dict, user, ack: str) -> dict:
    from app.core.config import settings
    from app.services import audit as audit_svc
    from app.services import env_store

    applied: dict = {}
    for key, (attr, typ) in INSPECTION_RULE_SETTINGS.items():
        if key not in params:
            continue
        value = typ(params[key])
        setattr(settings, attr, value)
        applied[key] = value
    persisted = env_store.set_keys({INSPECTION_RULE_SETTINGS[k][0]: str(v)
                                    for k, v in applied.items()})
    audit_svc.record(
        db, action="inspection.rule.update", user_id=getattr(user, "id", None),
        username=getattr(user, "username", ""), resource_type="inspection",
        resource_id=",".join(sorted(applied)), detail=dict(applied),
    )
    db.commit()
    return {"ref": "、".join(f"{k}={v}" for k, v in applied.items()),
            "result": {"applied": applied, "persisted": bool(persisted)}}


def _check_dashboard_cards(params: dict, user) -> None:
    cards = params.get("cards") or []
    if not cards:
        raise ChangeToolError("卡片列表为空")
    keys = [c.get("key") for c in cards]
    if len(set(keys)) != len(keys):
        raise ChangeToolError(f"卡片 key 重复：{sorted(keys)}")


def _h_update_dashboard_cards(db: Session, params: dict, user, ack: str) -> dict:
    from app.core.config import settings
    from app.services import audit as audit_svc
    from app.services import env_store

    cards = params["cards"]
    value = json.dumps(
        [[c["key"], c["label"], c.get("icon") or "Monitor", c.get("color") or "#409eff"]
         for c in cards], ensure_ascii=False)
    setattr(settings, "PRODUCT_CARDS", value)
    persisted = env_store.set_keys({"PRODUCT_CARDS": value})
    audit_svc.record(
        db, action="settings.product_cards_update", user_id=getattr(user, "id", None),
        username=getattr(user, "username", ""), resource_type="config",
        resource_id="PRODUCT_CARDS",
        detail={"cards": [c["key"] for c in cards]},
    )
    db.commit()
    return {"ref": f"{len(cards)} 张卡（{'、'.join(c['key'] for c in cards[:6])}）",
            "result": {"cards": len(cards), "persisted": bool(persisted)}}


# ---------------- 平台管理域：用户/权限/通用平台接口 ----------------

def _check_create_user(params: dict, user) -> None:
    if params.get("password") is not None and len(params["password"]) < 8:
        raise ChangeToolError("password 至少 8 位（留空则自动生成一次性初始密码）")


def _check_update_user(params: dict, user) -> None:
    if not params.get("user_id") and not params.get("username"):
        raise ChangeToolError("须给出 user_id 或 username 定位用户")
    if not any(k in params for k in ("role", "enabled", "display_name")):
        raise ChangeToolError("未给出任何可改字段（role/enabled/display_name）")


def _check_reset_user_password(params: dict, user) -> None:
    if not params.get("user_id") and not params.get("username"):
        raise ChangeToolError("须给出 user_id 或 username 定位用户")
    if params.get("new_password") is not None and len(params["new_password"]) < 8:
        raise ChangeToolError("new_password 至少 8 位（留空则自动生成一次性初始密码）")


def _check_platform_call(params: dict, user) -> None:
    from app.services.copilot import platform_api

    try:
        platform_api.guard(str(params.get("method") or ""),
                           str(params.get("path") or ""), user)
    except platform_api.PlatformApiError as e:
        raise ChangeToolError(str(e))


def _h_create_user(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.settings import create_user
    from app.schemas.settings import UserCreate

    generated = not params.get("password")
    password = params.get("password") or _gen_password()
    out = create_user(
        UserCreate(username=params["username"], password=password,
                   display_name=params.get("display_name") or "",
                   role=params.get("role") or "viewer",
                   must_change_password=True),   # 无论密码来源都强制首登改密
        _req(), db, user)
    result = {"id": out.get("id"), "username": params["username"],
              "role": params.get("role") or "viewer",
              "must_change_password": True,
              "password_source": "generated" if generated else "given"}
    if generated:
        result["initial_password"] = password   # 一次性回显（展示层此后掩码）
    return {"ref": params["username"], "result": result}


def _h_update_user(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.settings import update_user
    from app.schemas.settings import UserUpdate

    row = _find_user(db, params)
    if row is None:
        raise ChangeToolError(f"用户不存在: {params.get('username') or params.get('user_id')}")
    body = UserUpdate(
        display_name=params.get("display_name"),
        role=params.get("role"),
        is_active=params.get("enabled"),
    )
    out = update_user(row.id, body, _req(), db, user)
    return {"ref": row.username, "result": out}


def _h_reset_user_password(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.settings import update_user
    from app.schemas.settings import UserUpdate

    row = _find_user(db, params)
    if row is None:
        raise ChangeToolError(f"用户不存在: {params.get('username') or params.get('user_id')}")
    generated = not params.get("new_password")
    password = params.get("new_password") or _gen_password()
    update_user(row.id, UserUpdate(password=password), _req(), db, user)
    row.must_change_password = True   # 重置密码一律强制首登改密
    db.commit()
    result = {"id": row.id, "username": row.username, "must_change_password": True,
              "password_source": "generated" if generated else "given"}
    if generated:
        result["initial_password"] = password
    return {"ref": row.username, "result": result}


def _h_save_permission_policy(db: Session, params: dict, user, ack: str) -> dict:
    from app.api.v1.auth import PermPolicyIn, manage_permissions

    out = manage_permissions(
        PermPolicyIn(user_id=int(params["user_id"]), code=params["code"],
                     effect=params["effect"]),
        _req(), user=user, db=db)
    return {"ref": f"user:{params['user_id']} {params['code']}={params['effect']}",
            "result": out}


def _h_platform_api_call(db: Session, params: dict, user, ack: str) -> dict:
    from app.services.copilot import platform_api

    out = platform_api.call(str(params["method"]).upper(), str(params["path"]),
                            body=params.get("body") or {}, db=db, user=user)
    return {"ref": f"{params['method'].upper()} {params['path']}", "result": out}


# ---------------- 回滚（按快照逆向写回：新建=删除，改=恢复 before） ----------------

def _restore_or_delete(db: Session, before: dict, get_row, delete_row) -> dict:
    """通用回滚骨架：before 无 exists 表示原对象不存在，回滚时删除；否则逐字段恢复。"""
    row = get_row()
    if row is None:
        return {"restored": False, "note": "对象已不存在"}
    if not (before or {}).get("exists"):
        delete_row(row)
        return {"restored": True, "action": "delete"}
    for key, value in (before or {}).items():
        if key in ("exists", "id"):
            continue
        setattr(row, key, value)
    return {"restored": True, "action": "restore"}


def _rb_cloud_account(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.models.cloud_account import CloudAccount

    def get_row():
        name = (after or {}).get("name") or params.get("name")
        row = db.query(CloudAccount).filter(CloudAccount.name == name).first() if name else None
        if row is None and (after or {}).get("id"):
            row = db.get(CloudAccount, int(after["id"]))
        return row

    def to_row(row):
        if "region" in (before or {}):
            row.region = before.get("region") or ""
        if "extra" in (before or {}):
            row.extra_json = json.dumps(before.get("extra") or {}, ensure_ascii=False)
        if "enabled" in (before or {}):
            row.enabled = bool(before.get("enabled"))
        if "status" in (before or {}):
            row.status = before.get("status") or "untested"

    row = get_row()
    if row is None:
        return {"restored": False, "note": "对象已不存在"}
    if not (before or {}).get("exists"):
        db.delete(row)
        return {"restored": True, "action": "delete",
                "note": "凭证随账号一并清除"}
    to_row(row)
    return {"restored": True, "action": "restore",
            "note": "凭证只进不出：旧 AK/SK 不随回滚恢复"}


def _rb_cloud_sync(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    """同步是幂等采集（只读拉取资产入库），无需回滚——不做任何删除，留说明即可。"""
    return {"restored": False, "action": "none",
            "note": "同步是幂等采集（只读拉取资产入库），无需回滚；资产增删由云同步对账自动处理"}


def _rb_ownership_rule(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.models.ownership import OwnershipRule

    def get_row():
        rid = (after or {}).get("id") or (before or {}).get("id")
        row = db.get(OwnershipRule, int(rid)) if rid else None
        if row is None:
            value = (after or {}).get("key_value") or params.get("key_value")
            key_type = (after or {}).get("key_type") or params.get("key_type")
            from app.services.ownership import norm_key
            row = (db.query(OwnershipRule)
                   .filter(OwnershipRule.key_type == key_type,
                           OwnershipRule.key_value == norm_key(key_type, value)).first())
        return row

    return _restore_or_delete(db, before, get_row, db.delete)


def _rb_profile_known(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from datetime import timedelta  # noqa: F401  （解析占位）
    from app.models.behavior_profile import BehaviorProfile

    row = (db.query(BehaviorProfile)
           .filter(BehaviorProfile.profile_key == params.get("profile_key")).first())
    if row is None:
        return {"restored": False, "note": "画像不存在"}
    if not (before or {}).get("exists"):
        return {"restored": False, "note": "画像本就不存在，无从删除（只读画像不回删）"}

    def _parse(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None

    row.known_at = _parse(before.get("known_at"))
    row.silence_until = _parse(before.get("silence_until"))
    row.notify_state = before.get("notify_state") or ""
    return {"restored": True, "action": "restore",
            "note": "已知/静默状态已恢复；已收口的漂移事件不复活（事件收口不可逆）"}


def _rb_alert_rule(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.models.alert_rule import AlertRule

    def get_row():
        rid = (after or {}).get("id") or (before or {}).get("id")
        row = db.get(AlertRule, int(rid)) if rid else None
        if row is None:
            name = (after or {}).get("name") or (before or {}).get("name") or params.get("name")
            row = db.query(AlertRule).filter(AlertRule.name == name).first() if name else None
        return row

    def to_row(row):
        for key in ("name", "source", "level", "product", "metric", "project", "logstore",
                    "query_expr", "comparator", "threshold", "duration", "enabled"):
            if key in (before or {}):
                setattr(row, key, before.get(key))
        row.channel_ids = json.dumps(before.get("channel_ids") or [])
        row.silences = json.dumps(before.get("silences") or [])

    row = get_row()
    if row is None:
        return {"restored": False, "note": "对象已不存在"}
    if not (before or {}).get("exists"):
        db.delete(row)
        return {"restored": True, "action": "delete"}
    to_row(row)
    return {"restored": True, "action": "restore"}


def _rb_expiry_watch(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from datetime import datetime as _dtm
    from app.models.asset import Asset

    domain = (params.get("domain") or "").strip().lower()
    row = (db.query(Asset)
           .filter(Asset.product == "domain", Asset.instance_id == domain).first())
    if row is None:
        return {"restored": False, "note": "对象已不存在"}
    if not (before or {}).get("exists"):
        db.delete(row)
        return {"restored": True, "action": "delete"}
    row.name = before.get("name") or domain
    row.status = before.get("status") or ""
    exp = before.get("expire_time") or ""
    try:
        row.expire_time = _dtm.fromisoformat(exp) if exp else None
    except ValueError:
        row.expire_time = None
    row.extra = before.get("extra") or ""
    return {"restored": True, "action": "restore"}


def _rb_biz_map(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.models.business import BizIdentifier, Business

    name = (after or {}).get("name") or (before or {}).get("name") or params.get("name")
    row = db.query(Business).filter(Business.name == name).first() if name else None
    if row is None:
        return {"restored": False, "note": "对象已不存在"}
    if not (before or {}).get("exists"):
        db.delete(row)   # identifiers 级联删除（relationship cascade）
        return {"restored": True, "action": "delete"}
    row.name = before.get("name") or row.name
    row.code = before.get("code") or ""
    row.description = before.get("description") or ""
    if before.get("status") in ("draft", "confirmed"):
        # 保存映射可能同时确认草稿；回滚不能只删标识而留下已生效归属。
        row.status = before["status"]
    keep = {(i.get("ident_type"), i.get("ident_value")) for i in (before.get("identifiers") or [])}
    removed = []
    for ident in (db.query(BizIdentifier)
                  .filter(BizIdentifier.business_id == row.id).all()):
        if (ident.ident_type, ident.ident_value) not in keep:
            removed.append(f"{ident.ident_type}={ident.ident_value}")
            db.delete(ident)
    return {"restored": True, "action": "restore", "removed_identifiers": removed}


def _rb_edge_status(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from datetime import datetime as _dtm

    from app.models.link_edge import LinkEdge

    restored = []
    for e in (before or {}).get("edges") or []:
        row = db.get(LinkEdge, int(e.get("id") or 0))
        if row is None:
            continue
        row.status = e.get("status") or "draft"
        row.note = e.get("note") or ""
        row.confirmed_by = e.get("confirmed_by") or ""
        row.confirmed_by_id = int(e.get("confirmed_by_id") or 0)   # 恢复统一的用户身份引用
        try:
            row.confirmed_at = _dtm.fromisoformat(e["confirmed_at"]) if e.get("confirmed_at") else None
        except (ValueError, TypeError):
            row.confirmed_at = None
        restored.append(row.id)
    return {"restored": True, "action": "restore", "edge_ids": restored}


def _rb_inspection_rule(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.core.config import settings
    from app.services import env_store

    restored: dict = {}
    for key in (params or {}):
        spec = INSPECTION_RULE_SETTINGS.get(key)
        if spec is None or key not in (before or {}):
            continue
        attr, typ = spec
        value = typ(before[key])
        setattr(settings, attr, value)
        restored[key] = value
    persisted = env_store.set_keys({INSPECTION_RULE_SETTINGS[k][0]: str(v)
                                    for k, v in restored.items()}) if restored else False
    return {"restored": True, "action": "restore", "restored": restored,
            "persisted": bool(persisted)}


def _rb_dashboard_cards(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.core.config import settings
    from app.services import env_store

    cards = (before or {}).get("cards") or []
    value = json.dumps(
        [[c.get("key"), c.get("label"), c.get("icon") or "Monitor", c.get("color") or "#409eff"]
         for c in cards], ensure_ascii=False)
    setattr(settings, "PRODUCT_CARDS", value)
    persisted = env_store.set_keys({"PRODUCT_CARDS": value})
    return {"restored": True, "action": "restore", "cards": len(cards),
            "persisted": bool(persisted)}


def _rb_create_user(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.models.permission import PermissionPolicy
    from app.models.user import User

    if (before or {}).get("exists"):
        return {"restored": False, "note": "用户本就存在（非本次新建），未删除；仅可走 update_user 回滚字段"}
    row = _find_user(db, {"username": params.get("username"),
                          "user_id": (after or {}).get("id")})
    if row is None:
        return {"restored": False, "note": "对象已不存在"}
    uid = row.id
    db.query(PermissionPolicy).filter(PermissionPolicy.user_id == uid).delete()
    db.delete(row)
    return {"restored": True, "action": "delete", "note": "账号与其权限策略一并删除"}


def _rb_update_user(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.models.user import User

    if not (before or {}).get("exists"):
        return {"restored": False, "note": "对象已不存在"}
    row = db.get(User, int(before.get("id") or 0))
    if row is None:
        return {"restored": False, "note": "对象已不存在"}
    row.display_name = before.get("display_name") or ""
    row.role = before.get("role") or row.role
    row.is_active = bool(before.get("is_active", True))
    return {"restored": True, "action": "restore",
            "note": "角色/显示名/启用状态已恢复（密码不随回滚变化）"}


def _rb_reset_user_password(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    return {"restored": False, "action": "none",
            "note": "密码重置不可逆：旧密码哈希不入快照（凭证只进不出），无法恢复旧密码；"
                    "如需撤销请再次重置为新密码（同样是敏感操作）"}


def _rb_permission_policy(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    from app.models.permission import PermissionPolicy

    uid = int(params.get("user_id") or 0)
    code = params.get("code") or ""
    rows = (db.query(PermissionPolicy)
            .filter(PermissionPolicy.user_id == uid, PermissionPolicy.code == code).all())
    if not (before or {}).get("exists"):
        for r in rows:
            db.delete(r)
        return {"restored": True, "action": "delete", "note": "本次新建的策略覆盖已删除（回到角色默认）"}
    for r in rows[1:]:
        db.delete(r)
    if rows:
        rows[0].effect = before.get("effect") or "deny"
    return {"restored": True, "action": "restore"}


def _rb_platform_call(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    return {"restored": False, "action": "none",
            "note": "通用平台调用无自动回滚（目标对象各异）——已留同路径 GET 现场供人工比对；"
                    "请优先使用专用工具（有精确快照回滚），或由人工在对应页面恢复"}


# ---------------- 代码变更域：平台自改管道 ----------------
# 工单侧 plan 格式（LLM/工单/展示）：{files:[{path, edits:[{old,new}]}], summary, impact}
#   - old 必须逐字节等于现文件内容片段（构建期+执行期双校验，防 LLM 幻觉 old）
#   - 执行期把 edits 确定性替换为全文新内容，再走 code_mod 管道（快照→应用→验证门）
# code_mod.py 提供 validate_plan/snapshot_and_apply/verify_gate/auto_rollback/rollback/
# snapshot_out 契约；通过 lazy import 解耦，未就绪时拒建单或拒执行。
# 计划全文（files/edits）入 change_list_json：代码非凭证不加密；ticket_out 展示截断 diff。

MAX_CODE_FILES = 5     # 单次修改文件数上限（方案 §2.2 动作边界，与 code_mod.MAX_FILES 同值）
MAX_CODE_LINES = 200   # 单次修改行数上限（同上）
_DIFF_TEXT_MAX = 200   # ticket_out 展示层 diff 文本截断长度（全文只入库/入快照）

# 进程内辅助账：plan 指纹 -> 最近一次应用的 code_mod 快照 id（handler 写、snapshot_fn 收）。
# 权威链路在工单项 result 与 agent_code_snapshots 表；本字典只补 execute 内 after_blob 的关联。
_APPLIED_CODE: dict = {}


def code_mod_module():
    """code_mod 管道（lazy import）：未就绪返回 None（调用方优雅降级）。"""
    try:
        from app.services.copilot import code_mod as m
    except Exception:   # noqa: BLE001  未实现/依赖缺失=未就绪
        return None
    needed = ("code_search", "validate_plan", "snapshot_and_apply", "verify_gate",
              "auto_rollback", "rollback", "snapshot_out")
    return m if all(callable(getattr(m, n, None)) for n in needed) else None


def _require_code_mod():
    m = code_mod_module()
    if m is None:
        raise ChangeToolError("代码修改管道（code_mod）未就绪，暂不能执行代码变更——请人工修改或稍后再试")
    return m


def _code_boundaries():
    """可改域/禁改域（优先用 code_mod 契约常量，未就绪用同值内置副本）。"""
    m = code_mod_module()
    allow = tuple(getattr(m, "CODE_ALLOW", ()) or ()) or (
        "frontend/src/", "backend/app/api/", "backend/app/services/",
        "docs/active/", "docs/tmp/")
    deny = tuple(getattr(m, "CODE_DENY", ()) or ()) or (
        "backend/app/core/security.py", "backend/app/core/permissions.py",
        "backend/app/core/client_ip.py", "backend/app/core/deps.py",
        "backend/app/services/copilot/change_tools.py",
        "backend/app/services/copilot/code_mod.py",
        "backend/app/copilotd/", "requirements.txt", ".env")
    return allow, deny


def code_boundary_violations(paths: list) -> list:
    """路径边界违规（禁改域/非可改域/越界）——审查与构建期共用一套判定。"""
    allow, deny = _code_boundaries()
    out = []
    for raw in paths or []:
        s = str(raw or "").replace("\\", "/").strip()
        norm = "/".join(x for x in s.split("/") if x not in ("", "."))
        if not norm or norm.startswith("/") or ".." in norm.split("/"):
            out.append(f"{raw}: 路径非法（只接受仓库根相对路径，禁止 ..）")
            continue
        hit = ""
        for d in deny:
            if norm == d or (d.endswith("/") and norm.startswith(d)):
                hit = f"{norm} 属禁改域（安全边界，直接拒绝）"
                break
        if not hit:
            base = norm.rsplit("/", 1)[-1]
            if base.startswith(".env"):
                hit = f"{norm} 禁改（.env* 环境配置）"
            elif base.startswith("requirements"):
                hit = f"{norm} 禁改（依赖清单——需要新依赖=出提案人工装）"
            elif not any(norm.startswith(a) for a in allow):
                hit = f"{norm} 不在可改域（白名单：{', '.join(allow)}）"
        if hit:
            out.append(hit)
    return out


def _code_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[4]   # /opt/qianshu（与 code_mod.PROJECT_ROOT 同源）


def _resolve_code_file(path) -> "Path":
    """计划路径 -> 磁盘文件（只认仓库根相对路径；不存在/越界抛 ValueError）。"""
    from pathlib import Path

    s = str(path or "").replace("\\", "/").strip()
    norm = "/".join(x for x in s.split("/") if x not in ("", "."))
    if not norm or norm.startswith("/") or ".." in norm.split("/"):
        raise ValueError(f"路径非法: {path}")
    fp = (_code_root() / norm).resolve()
    root = _code_root().resolve()
    if fp != root and root not in fp.parents:
        raise ValueError(f"路径越界: {path}")
    return fp


def plan_shape_errors(plan) -> list:
    """plan 结构/上限校验。文件项接受两种契约格式：
    ① 编辑片段 {path, edits:[{old,new}]}（防幻觉校验 old）；
    ② 全文替换 {path, new: 全文新内容}（交 code_mod 的最终格式）。"""
    errors = []
    p = plan if isinstance(plan, dict) else {}
    files = p.get("files")
    if not isinstance(files, list) or not files:
        return ["计划为空（plan.files 必填，每项 {path, edits:[{old,new}]} 或 {path, new}）"]
    if len(files) > MAX_CODE_FILES:
        errors.append(f"文件数 {len(files)} 超上限 {MAX_CODE_FILES}——拆分提案分批确认")
    total_lines = 0
    for i, f in enumerate(files):
        tag = f"第 {i + 1} 项"
        if not isinstance(f, dict):
            errors.append(f"{tag}: 必须是 {{path, edits|new}} 对象")
            continue
        path = str(f.get("path") or "")
        edits = f.get("edits")
        if not path:
            errors.append(f"{tag}: 缺 path")
            continue
        if not edits and not isinstance(f.get("new"), str):
            errors.append(f"{path}: 缺 edits（old→new 编辑）或 new（全文新内容）")
            continue
        if isinstance(f.get("new"), str) and f.get("new") == "":
            errors.append(f"{path}: new 为空 = 删除文件——删除类一律拒绝（只改不删）")
        for j, e in enumerate(edits or []):
            if not isinstance(e, dict):
                errors.append(f"{path} 第 {j + 1} 处编辑: 必须是 {{old,new}} 对象")
                continue
            old, new = e.get("old"), e.get("new")
            if not isinstance(old, str) or not old:
                errors.append(f"{path} 第 {j + 1} 处编辑: old 必须是非空字符串（现文件逐字节片段）")
                continue
            if not isinstance(new, str):
                errors.append(f"{path} 第 {j + 1} 处编辑: new 必须是字符串")
                continue
            if old == new:
                errors.append(f"{path} 第 {j + 1} 处编辑: old 与 new 相同（空编辑）")
                continue
            total_lines += len(old.splitlines()) + len(new.splitlines())
    if total_lines > MAX_CODE_LINES * 2:
        errors.append(f"编辑片段合计约 {total_lines} 行超上限——拆分提案分批确认")
    return errors


def apply_edits(content: str, edits: list) -> str:
    """顺序应用 old→new 替换（构建/执行同语义）。

    old 必须逐字节存在且唯一（防幻觉/防误替换）；任何一处失败抛 ValueError（不动文件）。
    """
    out = content
    for idx, e in enumerate(edits or [], 1):
        old = e.get("old") if isinstance(e, dict) else None
        new = e.get("new") if isinstance(e, dict) else None
        if not isinstance(old, str) or not old:
            raise ValueError(f"第 {idx} 处编辑 old 非法")
        if not isinstance(new, str):
            raise ValueError(f"第 {idx} 处编辑 new 非法")
        n = out.count(old)
        if n == 0:
            raise ValueError(f"第 {idx} 处编辑 old 与现文件不一致（未找到该片段——禁止凭记忆编造 old）")
        if n > 1:
            raise ValueError(f"第 {idx} 处编辑 old 出现 {n} 次不唯一（请扩大 old 上下文使其唯一）")
        out = out.replace(old, new, 1)
    return out


def plan_olds_errors(plan) -> list:
    """逐处校验 old 逐字节等于现文件片段（顺序应用语义）——防 LLM 幻觉 old。

    只对 edits 格式校验（全文 new 格式无 old）；文件必须已存在（只改不删不新建）。
    """
    errors = []
    for f in (plan or {}).get("files") or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        try:
            fp = _resolve_code_file(path)
        except ValueError as e:
            errors.append(str(e))
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except FileNotFoundError:
            errors.append(f"{path}: 文件不存在（只支持修改现有文件，不支持新建/删除）")
            continue
        except Exception as e:   # noqa: BLE001
            errors.append(f"{path}: 读取失败 {e}")
            continue
        edits = f.get("edits")
        if edits:
            try:
                apply_edits(content, edits)
            except ValueError as e:
                errors.append(f"{path}: {e}")
    return errors


def build_codeplan(plan) -> tuple:
    """工单 plan → code_mod plan（{path, new: 全文新内容}）。

    edits 格式=读现文件→逐处替换 old 拼出全文（确定性替换，绝不执行任何代码；
    任何一处 old 对不上即拒——建单/执行期同语义，防幻觉+防文件漂移）；
    new 格式=全文直接采用。-> (codeplan, errors)。
    """
    files_out, errors = [], []
    for f in (plan or {}).get("files") or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        try:
            fp = _resolve_code_file(path)
            content = fp.read_text(encoding="utf-8")
            edits = f.get("edits")
            if edits:
                new_content = apply_edits(content, edits)
            else:
                new_content = f.get("new")
                if not isinstance(new_content, str) or new_content == "":
                    raise ValueError("缺 edits/new，或 new 为空（删除一律拒绝）")
        except Exception as e:   # noqa: BLE001  幻觉/读失败→拒（执行期文件可能已变化）
            errors.append(f"{path}: {e}")
            continue
        norm = "/".join(x for x in path.replace("\\", "/").split("/") if x not in ("", "."))
        files_out.append({"path": norm, "new": new_content})
    if errors:
        return None, errors
    if not files_out:
        return None, ["计划为空（无可应用的文件）"]
    return {"title": str((plan or {}).get("summary") or "")[:100], "files": files_out}, []


_DIFF_HEAD_LINES = 20          # diff_head 展示行数（前端预览口径，与 code_mod 同值）
_DIFF_LINE_MAX = 300


def _file_new_content(path: str, f: dict):
    """文件项 -> (现文件全文, new 全文)。edits 拼全文；new 直接用。失败抛 ValueError。"""
    fp = _resolve_code_file(path)
    try:
        content = fp.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    edits = f.get("edits")
    if edits:
        return content, apply_edits(content, edits)
    new = f.get("new")
    if not isinstance(new, str):
        raise ValueError("缺 edits/new")
    return content, new


def plan_diff_summary(plan) -> dict:
    """diff 摘要（SSE change 事件 / ticket_out 契约）：
    {"files": [{"path", "add_lines", "del_lines", "diff_head"}]}——现文件全文 vs new 全文，
    diff_head=unified diff 首 20 行（前端 AgentChangeCard 优先渲染此结构）。"""
    import difflib

    out_files = []
    for f in (plan or {}).get("files") or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        try:
            before, new = _file_new_content(path, f)
        except Exception:   # noqa: BLE001  算不出 diff 不阻断展示（前端有 edits 兜底）
            continue
        diff = list(difflib.unified_diff(
            before.splitlines(), new.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
        add = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        rem = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        head = [ln[:_DIFF_LINE_MAX] for ln in diff[:_DIFF_HEAD_LINES]]
        out_files.append({"path": path, "add_lines": add, "del_lines": rem,
                          "diff_head": "\n".join(head)})
    return {"files": out_files}


def _plan_fp(plan) -> str:
    import hashlib

    raw = json.dumps(plan or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _locate_ticket(db: Session, plan: dict):
    """在工单表里定位本计划所在工单（执行态窗口内按计划指纹匹配）。

    handler 无 ticket_id 透传（handler 契约固定），snapshot_and_apply 需要工单号
    关联快照——以 change_list 内容指纹匹配最近工单；找不到返回 (0, {})。
    """
    fp = _plan_fp(plan)
    rows = (db.query(AgentChangeTicket)
            .order_by(AgentChangeTicket.id.desc()).limit(50).all())
    for t in rows:
        try:
            items = json.loads(t.change_list_json or "[]")
        except ValueError:
            continue
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or it.get("tool") != "code_modify":
                continue
            ip = (it.get("params") or {}).get("plan")
            if _plan_fp(ip) == fp:
                return int(t.id or 0), it
    return 0, {}


def _snapshot_id_from_receipt(result) -> int:
    """从执行回执（done 结构化 / failed 错误串）找回 code_mod 快照 id。"""
    if isinstance(result, dict):
        r = result.get("result") if isinstance(result.get("result"), dict) else result
        sid = r.get("snapshot_id")
        if isinstance(sid, int):
            return sid
        m = re.search(r"snapshot_id[\"'=:\s#]+(\d+)", json.dumps(result, ensure_ascii=False, default=str))
        if m:
            return int(m.group(1))
    return 0


def _check_code_modify(params: dict, user) -> None:
    """构建期语义校验：结构/上限 + old 字节校验 + 边界（禁改域/可改域）+ 行数上限。"""
    plan = (params or {}).get("plan")
    errors = plan_shape_errors(plan)
    if errors:
        raise ChangeToolError("；".join(errors)[:400])
    errors = plan_olds_errors(plan)
    if errors:
        raise ChangeToolError("编辑 old 与现文件不一致（防幻觉校验）：" + "；".join(errors)[:400])
    codeplan, errors = build_codeplan(plan)
    if errors:
        raise ChangeToolError("；".join(errors)[:400])
    cm = _require_code_mod()
    v = cm.validate_plan(codeplan) or {}
    if not v.get("ok"):
        raise ChangeToolError("计划校验未过（禁改域/上限）：" + "；".join(v.get("violations") or [])[:400])


def _snap_code_modify(db: Session, params: dict) -> dict:
    """快照 = 计划摘要（代码全文在 agent_code_snapshots 表；此处只留摘要+快照 id 关联）。"""
    plan = (params or {}).get("plan") or {}
    out = {
        "exists": True,
        "plan_summary": str(plan.get("summary") or "")[:200],
        "impact": str(plan.get("impact") or "")[:200],
        "diff_summary": plan_diff_summary(plan),
        "files": [str(f.get("path") or "") for f in plan.get("files") or [] if isinstance(f, dict)],
    }
    sid = _APPLIED_CODE.pop(_plan_fp(plan), None)
    if sid:
        out["snapshot_id"] = sid
    return out


def _h_code_modify(db: Session, params: dict, user, ack: str) -> dict:
    """执行流程：validate_plan → snapshot_and_apply → verify_gate → 过不了自动回滚。

    不过 → auto_rollback + 抛错（change_gate 置该项 failed，回执含失败原因）；
    过 → done + 回执含「需重启生效」提示 + 自改经验沉淀进知识包。
    绝不执行 LLM 产出的命令/SQL 原文。
    """
    cm = _require_code_mod()
    plan = (params or {}).get("plan") or {}
    codeplan, errors = build_codeplan(plan)   # 执行期再校验 old 字节（建单后文件可能已变化）
    if errors:
        raise ChangeToolError("编辑 old 与现文件不一致（防幻觉校验，已拒执行）：" + "；".join(errors)[:250])
    v = cm.validate_plan(codeplan) or {}
    if not v.get("ok"):
        raise ChangeToolError("计划校验未过（禁改域/上限）：" + "；".join(v.get("violations") or [])[:250])

    ticket_id, _item = _locate_ticket(db, plan)
    r = cm.snapshot_and_apply(ticket_id, codeplan, user) or {}
    if not r.get("ok"):
        raise ChangeToolError("快照/应用失败（现场未改或已回滚）："
                              + str(r.get("error") or r.get("violations") or "")[:200])
    sid = int(r.get("snapshot_id") or 0)
    _APPLIED_CODE[_plan_fp(plan)] = sid
    changed = [str(f.get("path") or "") for f in (plan.get("files") or []) if isinstance(f, dict)]
    g = cm.verify_gate(changed, snapshot_id=sid) or {}
    if not g.get("passed"):
        rb = cm.auto_rollback(sid, user) or {}
        reason = "；".join(
            f"{x.get('check')}: {'过' if x.get('ok') else (x.get('detail') or '不过')}"
            for x in g.get("results") or [])[:150]
        restored = bool(rb.get("ok"))
        if not restored and rb.get("conflicts"):
            raise ChangeToolError(
                f"验证门未过，且自动回滚被拒（快照 #{sid} 目标文件被第三方改动，不覆盖）："
                + "；".join(f"{c.get('path')}: {c.get('reason')}" for c in rb["conflicts"])[:150]
                + f"；验证明细：{reason}")
        raise ChangeToolError(
            f"验证门未过（快照 #{sid} 已自动回滚 restored={restored}）：{reason}")

    # 自改经验沉淀（诉求→改动摘要→文件 进知识包）；失败不阻断执行回执
    try:
        from app.services import knowledge_log

        knowledge_log.record_self_change(
            intent=str(plan.get("summary") or (params or {}).get("title") or ""),
            snapshot_id=sid, ticket_id=ticket_id, files=changed,
            lines=sum(int(a.get("lines") or 0) for a in (r.get("applied") or [])),
            verify_passed=True)
    except Exception:   # noqa: BLE001  沉淀是收尾动作，异常吞掉不碰主流程
        pass
    return {
        "ref": f"{len(changed)} 文件（快照 #{sid}）",
        "result": {
            "snapshot_id": sid,
            "applied": r.get("applied"),
            "verify": {"passed": True, "results": g.get("results")},
            "note": f"需重启生效：代码改动已落盘并通过验证门，重启后端服务（8810）后生效"
                    f"（可在改动快照弹窗点「重启生效」委托 qianshu-copilotd 自动重启）；"
                    f"可随时一键回滚（快照 #{sid}）",
        },
    }


def _rb_code_modify(db: Session, params: dict, before: dict, after: dict, user) -> dict:
    """一键回滚：code_mod.rollback(snapshot_id) 逐文件字节恢复 before。

    跨快照冲突（目标文件被第三方改动）→ 抛错拒绝强还原（工单项不标 rolled_back），
    绝不默默覆盖第三方改动。"""
    cm = _require_code_mod()
    plan = (params or {}).get("plan") or {}
    sid = _snapshot_id_from_receipt(after) or int((after or {}).get("snapshot_id") or 0) \
        or _APPLIED_CODE.get(_plan_fp(plan)) or 0
    if not sid:
        _tid, item = _locate_ticket(db, plan)
        sid = _snapshot_id_from_receipt(item.get("result"))
    if not sid:
        return {"restored": False, "action": "none",
                "note": "找不到代码快照 id（该变更可能从未成功应用），无从回滚"}
    r = cm.rollback(sid, user) or {}
    ok = bool(r.get("ok"))
    if not ok and r.get("conflicts"):
        raise ChangeToolError(
            f"拒绝回滚快照 #{sid}：目标文件在应用后被改动过（不覆盖第三方修改）——"
            + "；".join(f"{c.get('path')}: {c.get('reason')}" for c in r["conflicts"])[:200])
    return {"restored": ok, "action": "code_rollback", "snapshot_id": sid,
            "files": [e.get("path") for e in (r.get("restored") or []) if isinstance(e, dict)],
            "note": "代码快照已逐文件字节恢复" if ok else str(r.get("error") or "")[:150]}


# ---------------- 代码快照 API 支撑（快照列表/一键回滚，确认门语义） ----------------

def code_snapshots_out(db: Session, ticket_id: int = 0) -> list:
    """代码快照列表（含 diff_summary）：AgentCodeSnapshot × code_mod.snapshot_out 截断口径。

    前端契约（AgentChangeCard 快照弹窗）：items=[{id, ticket_id, created_at, status,
    verified, files:[{path, add_lines, del_lines, diff_head}], diff_summary}]——
    files 项给结构化 diff（code_mod 的 diff_summary 字符串解析/快照 before/after 现算），
    全文 before/after 只入快照不出参。
    """
    from app.models.agent_code_snapshot import AgentCodeSnapshot

    qry = db.query(AgentCodeSnapshot)
    if int(ticket_id or 0):
        qry = qry.filter(AgentCodeSnapshot.ticket_id == int(ticket_id))
    rows = qry.order_by(AgentCodeSnapshot.id.desc()).limit(100).all()
    cm = code_mod_module()
    out = []
    for row in rows:
        item = {"id": row.id, "ticket_id": row.ticket_id,
                "created_at": _dt(row.created_at), "status": row.status or ""}
        verified = None
        if cm is not None:
            try:
                detail = cm.snapshot_out(row.id) or {}
            except Exception:   # noqa: BLE001  展示尽力而为，不阻断列表
                detail = {}
            if isinstance(detail, dict):
                verified = detail.get("verified")
        item["verified"] = verified
        files = _snapshot_files_structured(row)
        item["files"] = files
        item["diff_summary"] = {"files": files}
        out.append(item)
    return out


def _snapshot_files_structured(row) -> list:
    """快照 files_json -> [{path, add_lines, del_lines, diff_head}]（截断，不出全文）。"""
    import difflib

    try:
        entries = json.loads(row.files_json or "[]")
    except ValueError:
        entries = []
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        path = str(e.get("path") or "")
        stat = str(e.get("diff_summary") or "").splitlines()
        m = re.match(r"\+(\d+)/-(\d+)", stat[0] or "") if stat else None
        head = "\n".join(stat[1:1 + _DIFF_HEAD_LINES])
        if m is None:
            diff = list(difflib.unified_diff(
                str(e.get("before") or "").splitlines(),
                str(e.get("after") or "").splitlines(),
                fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
            add = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
            rem = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
            head = "\n".join(ln[:_DIFF_LINE_MAX] for ln in diff[:_DIFF_HEAD_LINES])
        else:
            add, rem = int(m.group(1)), int(m.group(2))
        out.append({"path": path, "add_lines": add, "del_lines": rem, "diff_head": head})
    return out


def code_snapshot_rollback(db: Session, snapshot_id: int, user, force: bool = False) -> dict:
    """代码快照一键回滚（复用确认门语义）：API 层限 admin；此处强制走 code_mod.rollback
    （逐文件字节恢复 + 内部验证门 + code.rollback 审计），并补记确认门侧审计留痕
    （agent.change.rollback）——无旁路、不可重放语义由快照 status=rolled_back 承担。

    force=True 才覆盖第三方改动；跨快照冲突（目标文件应用后被改过）默认拒绝并抛错说明。"""
    cm = _require_code_mod()
    r = cm.rollback(int(snapshot_id), user, force=force) or {}
    ok = bool(r.get("ok"))
    from app.services import audit

    audit.record(
        db, action="agent.change.rollback",
        user_id=getattr(user, "id", None), username=getattr(user, "username", ""),
        resource_type="code_mod", resource_id=str(int(snapshot_id)),
        result="ok" if ok else "fail",
        detail={"snapshot_id": int(snapshot_id), "trigger": "user_api",
                "forced": bool(force and r.get("conflicts")),
                "restored": r.get("restored"), "verify_passed": (r.get("verify") or {}).get("passed"),
                **({"conflicts": r.get("conflicts")} if r.get("conflicts") else {})},
    )
    db.commit()
    if not ok and r.get("conflicts"):
        raise ChangeToolError(
            f"拒绝回滚快照 #{int(snapshot_id)}：目标文件在应用后被改动过，强还原会覆盖第三方修改"
            f"（确认无误可带 force=true 强制回滚）——"
            + "；".join(f"{c.get('path')}: {c.get('reason')}" for c in r["conflicts"])[:200])
    return {"rolled_back": ok, "restored": ok, "snapshot_id": int(snapshot_id),
            "items": r.get("restored") or [], "verify": r.get("verify"),
            "already": bool(r.get("already")), "error": str(r.get("error") or "")}


# ---------------- 重启生效：委托 copilotd 本地通道重启平台 ----------------
# 「需重启生效」的闭环：工单完成后 admin 二次确认 → 后端委托 qianshu-copilotd
# （127.0.0.1:18810 + X-Copilotd-Token，token 走 config/qianshu-copilotd.json 的
# token_file）POST /restart 优雅重启平台。重启会终止本进程，故委托在响应送达后
# 异步发起；回执由 copilotd 落动作日志并经桥上报（站内信+审计）。

def _copilotd_channel() -> tuple:
    """copilotd 本地通道 -> (base_url, token)。配置/token 从 qianshu-copilotd.json 侧读取。"""
    from app.copilotd.config import ensure_token, load_or_create

    cfg = load_or_create()
    return f"http://{cfg.listen_host}:{cfg.listen_port}", ensure_token(cfg.token_file)


def _copilotd_call(method: str, path: str, payload: dict = None, timeout: float = 8.0) -> dict:
    """调 copilotd 生命周期通道（X-Copilotd-Token 鉴权）。失败抛 ChangeToolError。"""
    import urllib.error
    import urllib.request

    base, token = _copilotd_channel()
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method != "GET" else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("X-Copilotd-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:   # noqa: BLE001
            pass
        raise ChangeToolError(f"copilotd 通道拒绝（HTTP {e.code}）: {body}") from e
    except Exception as e:   # noqa: BLE001
        raise ChangeToolError(f"copilotd 通道不可用: {type(e).__name__}: {str(e)[:150]}") from e


def copilotd_health(timeout: float = 8.0) -> dict:
    """平台自愈只读体检：复用 copilotd 本地 token 通道，不触碰主机。"""
    return _copilotd_call("GET", "/health", timeout=timeout)


def copilotd_restart_delegated(trigger: str = "code_apply_restart") -> dict:
    """异步委托体：POST /restart（copilotd 优雅重启平台）——响应送达后才执行。"""
    return _copilotd_call("POST", "/restart", {"trigger": trigger}, timeout=300.0)


def code_apply_restart(db: Session, snapshot_id: int, user, *, confirm: bool,
                       defer=None) -> dict:
    """「重启生效」（admin+二次确认在 API 层强制，此处只做业务编排）：
    ①快照须 applied（代码已落盘）；②通道预检 GET /health（token/可达性不过=拒绝，
    不留半途重启）；③审计 code.restart（先留痕再委托，重启进程也不丢账）；
    ④defer(fn) 排队在响应送达后委托 POST /restart（缺省同步直调）。
    """
    cm = _require_code_mod()
    detail = cm.snapshot_out(int(snapshot_id)) or {}
    if detail.get("error"):
        raise ChangeToolError(f"快照不存在: {snapshot_id}")
    if detail.get("status") != "applied":
        raise ChangeToolError(
            f"快照 #{snapshot_id} 状态为 {detail.get('status')}（只有已应用的改动才需重启生效）")
    health = _copilotd_call("GET", "/health", timeout=8.0)   # 预检：token/可达性
    from app.services import audit

    audit.record(
        db, action="code.restart",
        user_id=getattr(user, "id", None), username=getattr(user, "username", ""),
        resource_type="code_mod", resource_id=str(int(snapshot_id)),
        detail={"snapshot_id": int(snapshot_id), "ticket_id": detail.get("ticket_id"),
                "files": [f.get("path") for f in detail.get("files") or []],
                "channel": "qianshu-copilotd", "confirm": bool(confirm),
                "health_ok": bool(health.get("ok", True))},
    )
    db.commit()
    if defer is not None:
        defer(copilotd_restart_delegated)
        delegated = "queued"
    else:
        copilotd_restart_delegated()
        delegated = "sent"
    return {"ok": True, "delegated": delegated, "snapshot_id": int(snapshot_id),
            "note": "已委托 qianshu-copilotd 重启平台，改动即将生效——服务会在数秒内重启，"
                    "恢复后站内信会有重启回执（也可在守护动作日志查看）"}


# ---------------- 注册表 ----------------

def _ack_on_creds(params: dict) -> bool:
    return bool((params or {}).get("ak") or (params or {}).get("sk"))


CHANGE_TOOLS: dict = {
    "create_cloud_account": {
        "title": "新增云账号",
        "impact": "资产/账单采集面（新增一个云厂商账号，凭证加密入库只进不出）",
        "rollback": "删除该云账号（凭证一并清除）",
        "ack_required": True,
        "sensitive": True,
        "target_type": "cloud_account",
        "ref_fn": lambda p: str(p.get("name") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "账号名称（唯一）"},
                "vendor": {"type": "string",
                           "description": "厂商码 aliyun/tencent/volc/baidu/huawei/aws/azure/onprem/esxi/vcenter/kvm"},
                "ak": {"type": "string",
                       "description": "真实只读 AccessKey（≥8 位；自建厂商可空；禁止编造或占位，缺失时先向用户索取）"},
                "sk": {"type": "string", "description": "Secret（与 ak 成对）"},
                "region": {"type": "string", "description": "主区域（可选）"},
                "extra": {"type": "object", "description": "厂商扩展（可选）"},
                "enabled": {"type": "boolean", "description": "是否启用（可选，默认 true）"},
            },
            "required": ["name", "vendor", "ak", "sk"],
        },
        "snapshot_fn": _snap_cloud_account,
        "handler": _h_create_cloud_account,
        "rollback_fn": _rb_cloud_account,
    },
    "update_cloud_account": {
        "title": "修改云账号",
        "impact": "资产/账单采集面（改账号配置；改凭证时重新警示确认）",
        "rollback": "恢复账号配置（凭证只进不出，不恢复旧 AK/SK）",
        "ack_required": _ack_on_creds,   # 仅当改 AK/SK 时要「我已知晓」
        "sensitive": _ack_on_creds,
        "target_type": "cloud_account",
        "ref_fn": lambda p: str(p.get("name") or p.get("account_id") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer", "description": "云账号 ID（必填）"},
                "name": {"type": "string", "description": "新名称（可选）"},
                "ak": {"type": "string", "description": "新 AccessKey（可选；与 sk 成对，改凭证需确认）"},
                "sk": {"type": "string", "description": "新 Secret（可选）"},
                "region": {"type": "string", "description": "主区域（可选）"},
                "extra": {"type": "object", "description": "厂商扩展（可选）"},
                "enabled": {"type": "boolean", "description": "启用开关（可选）"},
            },
            "required": ["account_id"],
        },
        "snapshot_fn": _snap_cloud_account,
        "handler": _h_update_cloud_account,
        "rollback_fn": _rb_cloud_account,
    },
    "sync_cloud": {
        "title": "同步云资产",
        "impact": "资产台账（按账号拉取云资产入库，只读采集，不改云上资源）",
        "rollback": "同步是幂等采集，无需回滚（资产增删由同步对账自动处理）",
        "ack_required": False,
        "target_type": "cloud_sync",
        "ref_fn": lambda p: f"account:{p.get('account_id') or 0}/{p.get('product') or '*'}",
        "params_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer",
                               "description": "云账号 ID（可选；0 或缺省=全部启用账号）"},
                "product": {"type": "string",
                            "description": "产品码（可选 ecs/rds/redis/slb/domain/ssl_cert…；缺省=全部支持产品）"},
            },
            "required": [],
        },
        "snapshot_fn": _snap_cloud_sync,
        "handler": _h_sync_cloud,
        "rollback_fn": _rb_cloud_sync,
    },
    "save_ownership_rule": {
        "title": "保存归属登记",
        "impact": "归属/业务标签解析（实体键 → 厂商/账号/业务）",
        "rollback": "新建=删除该登记；修改=恢复原登记",
        "ack_required": False,
        "target_type": "ownership_rule",
        "ref_fn": lambda p: f"{p.get('key_type', '')}:{p.get('key_value', '')}",
        "params_schema": {
            "type": "object",
            "properties": {
                "key_type": {"type": "string", "enum": ["instance", "ip", "domain", "name"],
                             "description": "实体键类型"},
                "key_value": {"type": "string", "description": "键值（IP/域名/实例 ID/名称）"},
                "match_type": {"type": "string", "enum": ["exact", "pattern"],
                               "description": "匹配方式（可选，默认 exact）"},
                "vendor": {"type": "string", "description": "厂商码（可选）"},
                "account_name": {"type": "string", "description": "账号名（可选）"},
                "account_id": {"type": "integer", "description": "绑定云账号 ID（可选）"},
                "business": {"type": "string", "description": "归属业务（可选）"},
                "note": {"type": "string", "description": "备注（可选）"},
            },
            "required": ["key_type", "key_value"],
        },
        "snapshot_fn": _snap_ownership_rule,
        "handler": _h_save_ownership_rule,
        "rollback_fn": _rb_ownership_rule,
    },
    "mark_profile_known": {
        "title": "标记画像已知道",
        "impact": "行为画像状态机（确认知晓当前变化，按关键度静默；不改基线）",
        "rollback": "恢复已知/静默状态（已收口的漂移事件不复活）",
        "ack_required": False,
        "target_type": "behavior_profile",
        "ref_fn": lambda p: str(p.get("profile_key") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "profile_key": {"type": "string", "description": "画像键（必填）"},
                "silence_3d": {"type": "boolean",
                               "description": "aux 辅助画像勾选才静默 3 天（可选；core 固定 1 天）"},
            },
            "required": ["profile_key"],
        },
        "snapshot_fn": _snap_profile_known,
        "handler": _h_mark_profile_known,
        "rollback_fn": _rb_profile_known,
    },
    "save_alert_rule": {
        "title": "保存告警规则",
        "impact": "告警评估面（新增/更新一条阈值规则，影响后续告警产生）",
        "rollback": "新建=删除该规则；修改=恢复原规则",
        "ack_required": False,
        "target_type": "alert_rule",
        "ref_fn": lambda p: str(p.get("name") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "规则名（唯一，必填）"},
                "source": {"type": "string", "enum": ["metric", "sls"],
                           "description": "指标阈值 / SLS 聚合（必填）"},
                "level": {"type": "string", "enum": ["critical", "warning", "info"],
                          "description": "级别（可选，默认 warning）"},
                "product": {"type": "string", "description": "产品码（metric 必填，如 ecs/rds）"},
                "metric": {"type": "string", "description": "指标名（metric 必填）"},
                "project": {"type": "string", "description": "SLS project（sls 必填）"},
                "logstore": {"type": "string", "description": "SLS logstore（sls 必填）"},
                "query_expr": {"type": "string", "description": "SLS 检索式（可选）"},
                "comparator": {"type": "string", "enum": [">", ">=", "<", "<="],
                               "description": "比较符（可选，默认 >）"},
                "threshold": {"type": "number", "description": "阈值（必填）"},
                "duration": {"type": "integer", "description": "持续窗口秒（可选，默认 300）"},
                "channel_ids": {"type": "array", "items": {"type": "integer"},
                                "description": "通知渠道 ID 列表（可选）"},
                "silences": {"type": "array", "items": {"type": "string"},
                             "description": "每日静默时段如 23:00-07:00（可选）"},
                "enabled": {"type": "boolean", "description": "是否启用（可选）"},
            },
            "required": ["name", "source", "threshold"],
        },
        "snapshot_fn": _snap_alert_rule,
        "handler": _h_save_alert_rule,
        "rollback_fn": _rb_alert_rule,
    },
    "save_expiry_watch": {
        "title": "登记到期关注",
        "impact": "到期总账（登记外部域名到期时间，到期账照常预警）",
        "rollback": "新建=删除该登记；修改=恢复原到期登记",
        "ack_required": False,
        "target_type": "expiry_watch",
        "ref_fn": lambda p: str(p.get("domain") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "域名（如 example.com，必填）"},
                "expire_time": {"type": "string", "description": "到期时间 YYYY-MM-DD（必填）"},
                "note": {"type": "string", "description": "备注（可选）"},
            },
            "required": ["domain", "expire_time"],
        },
        "snapshot_fn": _snap_expiry_watch,
        "handler": _h_save_expiry_watch,
        "rollback_fn": _rb_expiry_watch,
    },
    "save_biz_map": {
        "title": "保存业务映射",
        "impact": "业务标签解析（业务线+逻辑标识，影响告警/成本/巡检业务归属）",
        "rollback": "新建=删除该业务线（含标识）；修改=恢复原名与描述并摘除新增标识",
        "ack_required": False,
        "target_type": "biz_map",
        "ref_fn": lambda p: str(p.get("name") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "业务线名（必填）"},
                "code": {"type": "string", "description": "短码（可选）"},
                "description": {"type": "string", "description": "描述（可选）"},
                "identifiers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ident_type": {"type": "string",
                                           "enum": ["host", "app_name", "db_name",
                                                    "ecs_name", "rds_instance"]},
                            "ident_value": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["ident_type", "ident_value"],
                    },
                    "description": "逻辑标识列表（可选）",
                },
            },
            "required": ["name"],
        },
        "snapshot_fn": _snap_biz_map,
        "handler": _h_save_biz_map,
        "rollback_fn": _rb_biz_map,
    },
    # ---------------- 改进域 ----------------
    "save_edge_status": {
        "title": "确认/忽略链路边",
        "impact": "拓扑链路边状态（确认/忽略/恢复草稿，影响拓扑视图与野域名识别）",
        "rollback": "恢复各边原状态、备注与确认人",
        "ack_required": False,
        "target_type": "link_edge",
        "ref_fn": lambda p: "边" + ",".join(str(i) for i in (p.get("ids") or [])[:8]),
        "params_schema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"},
                        "description": "链路边 ID 列表（link_edges.id）"},
                "status": {"type": "string", "enum": ["confirmed", "ignored", "draft"],
                           "description": "目标状态：confirmed 确认 / ignored 忽略 / draft 恢复草稿"},
                "note": {"type": "string", "description": "备注（可选）"},
            },
            "required": ["ids", "status"],
        },
        "snapshot_fn": _snap_edge_status,
        "handler": _h_save_edge_status,
        "rollback_fn": _rb_edge_status,
    },
    "save_inspection_rule": {
        "title": "保存巡检规则",
        "impact": "巡检评估面（健康/闲置/到期阈值，影响后续巡检判定）",
        "rollback": "恢复本次改动的阈值配置",
        "ack_required": False,
        "target_type": "inspection_rule",
        "ref_fn": lambda p: "、".join(f"{k}={p[k]}" for k in INSPECTION_RULE_SETTINGS if k in p)[:80],
        "params_schema": {
            "type": "object",
            "properties": {
                "health_warn": {"type": "number", "description": "使用率预警阈值 %（0-100）"},
                "health_crit": {"type": "number", "description": "使用率严重阈值 %（> health_warn）"},
                "idle_cpu": {"type": "number", "description": "闲置 CPU 阈值 %（7 天均值低于此视为闲置）"},
                "idle_qps": {"type": "number", "description": "SLB 无流量 QPS 阈值"},
                "expiry_days": {"type": "integer", "description": "到期预警天数（1-365）"},
                "note": {"type": "string", "description": "调参依据备注（可选）"},
            },
            "required": [],
        },
        "check_fn": _check_inspection_rule,
        "snapshot_fn": _snap_inspection_rule,
        "handler": _h_save_inspection_rule,
        "rollback_fn": _rb_inspection_rule,
    },
    "update_dashboard_cards": {
        "title": "更新首页产品卡",
        "impact": "首页产品卡/仪表盘卡片配置（卡片组合与顺序）",
        "rollback": "恢复原卡片配置",
        "ack_required": False,
        "target_type": "dashboard_cards",
        "ref_fn": lambda p: f"{len(p.get('cards') or [])} 张卡",
        "params_schema": {
            "type": "object",
            "properties": {
                "cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "产品码（如 ecs/rds/cdn）"},
                            "label": {"type": "string", "description": "卡片显示名"},
                            "icon": {"type": "string", "description": "图标名（可选，默认 Monitor）"},
                            "color": {"type": "string", "description": "主题色（可选）"},
                        },
                        "required": ["key", "label"],
                    },
                    "description": "完整卡片组合（整体替换，含顺序）",
                },
            },
            "required": ["cards"],
        },
        "check_fn": _check_dashboard_cards,
        "snapshot_fn": _snap_dashboard_cards,
        "handler": _h_update_dashboard_cards,
        "rollback_fn": _rb_dashboard_cards,
    },
    # ---------------- 平台管理域 ----------------
    "create_user": {
        "title": "创建平台用户",
        "impact": "账号与权限面（新增登录账号；首次登录强制修改密码——无论密码是自动生成还是显式给定）",
        "rollback": "删除该账号（含其权限策略）",
        "ack_required": True,
        "sensitive": True,
        "target_type": "user",
        "ref_fn": lambda p: str(p.get("username") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "用户名（必填，平台内唯一）"},
                "role": {"type": "string", "enum": ["admin", "operator", "viewer"],
                         "description": "角色（可选，默认 viewer）"},
                "display_name": {"type": "string", "description": "显示名（可选）"},
                "password": {"type": "string",
                             "description": "初始密码（可选，≥8 位；留空=自动生成一次性密码，执行结果回显一次；"
                                            "无论密码来源，首次登录一律强制修改密码）"},
            },
            "required": ["username"],
        },
        "check_fn": _check_create_user,
        "snapshot_fn": _snap_user,
        "handler": _h_create_user,
        "rollback_fn": _rb_create_user,
    },
    "update_user": {
        "title": "修改平台用户",
        "impact": "账号与权限面（角色/启用状态/显示名）",
        "rollback": "恢复角色/显示名/启用状态（密码不随回滚变化）",
        "ack_required": True,
        "sensitive": True,
        "target_type": "user",
        "ref_fn": lambda p: str(p.get("username") or p.get("user_id") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "用户 ID（与 username 二选一）"},
                "username": {"type": "string", "description": "用户名（与 user_id 二选一）"},
                "role": {"type": "string", "enum": ["admin", "operator", "viewer"],
                         "description": "新角色（可选）"},
                "enabled": {"type": "boolean", "description": "启用/禁用（可选）"},
                "display_name": {"type": "string", "description": "显示名（可选）"},
            },
            "required": [],
        },
        "check_fn": _check_update_user,
        "snapshot_fn": _snap_user,
        "handler": _h_update_user,
        "rollback_fn": _rb_update_user,
    },
    "reset_user_password": {
        "title": "重置用户密码",
        "impact": "账号安全（重置登录密码，一律强制首登改密；旧密码立即失效）",
        "rollback": "不可逆——旧密码哈希不入快照（凭证只进不出）；如需撤销请再次重置",
        "ack_required": True,
        "sensitive": True,
        "target_type": "user_password",
        "ref_fn": lambda p: str(p.get("username") or p.get("user_id") or ""),
        "params_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "用户 ID（与 username 二选一）"},
                "username": {"type": "string", "description": "用户名（与 user_id 二选一）"},
                "new_password": {"type": "string",
                                 "description": "新密码（可选，≥8 位；留空=自动生成一次性密码，"
                                                "执行结果回显一次并强制首登改密）"},
            },
            "required": [],
        },
        "check_fn": _check_reset_user_password,
        "snapshot_fn": _snap_user,
        "handler": _h_reset_user_password,
        "rollback_fn": _rb_reset_user_password,
    },
    "save_permission_policy": {
        "title": "保存权限策略",
        "impact": "账号与权限面（用户级 allow/deny 覆盖角色默认，deny 优先）",
        "rollback": "新建=删除该覆盖（回角色默认）；修改=恢复原覆盖",
        "ack_required": True,
        "sensitive": True,
        "target_type": "permission_policy",
        "ref_fn": lambda p: f"user:{p.get('user_id')} {p.get('code')}={p.get('effect')}",
        "params_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "目标用户 ID（必填）"},
                "code": {"type": "string",
                         "description": "权限码（k8s.write/k8s.dangerous/machines.ssh/"
                                        "agent.query/agent.change）"},
                "effect": {"type": "string", "enum": ["allow", "deny", "default"],
                           "description": "allow 放行 / deny 拒绝（优先）/ default 恢复角色默认"},
            },
            "required": ["user_id", "code", "effect"],
        },
        "snapshot_fn": _snap_permission_policy,
        "handler": _h_save_permission_policy,
        "rollback_fn": _rb_permission_policy,
    },
    "platform_api_call": {
        "title": "调用平台接口",
        "impact": "平台自身配置/管理面（只允许 openapi 注册路径；黑名单恒拒；权限随会话用户）",
        "rollback": "无自动回滚（目标对象各异）——留同路径 GET 现场供人工比对，优先用专用工具",
        "ack_required": True,
        "sensitive": True,
        "target_type": "platform_api",
        "ref_fn": lambda p: f"{str(p.get('method') or '').upper()} {p.get('path')}",
        "params_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"],
                           "description": "HTTP 方法（GET 走只读工具 platform_api_query；DELETE 黑名单恒拒）"},
                "path": {"type": "string", "description": "平台 API 路径（须 openapi 注册，如 /api/links/edges/status）"},
                "body": {"type": "object", "description": "请求体（可选，按目标端点 schema）"},
            },
            "required": ["method", "path"],
        },
        "check_fn": _check_platform_call,
        "snapshot_fn": _snap_platform_call,
        "handler": _h_platform_api_call,
        "rollback_fn": _rb_platform_call,
    },
    # ---------------- 平台自改：代码级变更只走 change_code 专用路径 ----------------
    "code_modify": {
        "title": "修改平台代码",
        "impact": "平台自改（L3b 管道：快照→应用→验证门→不过自动回滚；需重启生效）",
        "rollback": "按代码快照逐文件字节恢复 before（一键回滚）",
        "ack_required": True,
        "sensitive": True,
        "hidden_from_prompt": True,   # 代码变更只走 change_code 计划路径（code_search+审查），
                                      # 不进配置级变更计划提示词（防无上下文乱出 plan）
        "target_type": "code_mod",
        "ref_fn": lambda p: f"{len(((p.get('plan') or {}).get('files') or []))} 文件",
        "params_schema": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "代码变更计划：{files:[{path, edits:[{old,new}]} 或 {path, new}], "
                                   "summary, impact}——old 须逐字节等于现文件片段（防幻觉双校验）；"
                                   "计划全文入工单不加密（代码非凭证），展示层截断 diff",
                },
            },
            "required": ["plan"],
        },
        "check_fn": _check_code_modify,
        "snapshot_fn": _snap_code_modify,
        "handler": _h_code_modify,
        "rollback_fn": _rb_code_modify,
    },
}


def tool_names() -> list:
    return list(CHANGE_TOOLS.keys())


def prompt_block() -> str:
    """给 LLM 变更计划提示词用的白名单工具表（紧凑）。"""
    lines = []
    for name, spec in CHANGE_TOOLS.items():
        if spec.get("hidden_from_prompt"):
            continue   # 代码变更工具只走 change_code 专用路径，不进配置级计划提示词
        props = spec["params_schema"].get("properties") or {}
        params = "; ".join(f"{k}: {v.get('description', '')}" for k, v in props.items())
        req = ",".join(spec["params_schema"].get("required") or [])
        ack = "需「我已知晓」确认" if spec["ack_required"] else "无需确认（仍走变更清单）"
        lines.append(f"- {name}: {spec['title']}（{ack}）\n  params: {params}\n  required: {req}")
    return "\n".join(lines)


def item_title(spec: dict, params: dict) -> str:
    try:
        ref = spec["ref_fn"](params)
    except Exception:
        ref = ""
    return f"{spec['title']}：{ref}" if ref else spec["title"]


# ---------------- 工单落库（只建单不执行） ----------------

def build_ticket(db: Session, session_id: int, msg_id: int, items: list, user,
                 title: str = "") -> dict:
    """把变更计划落成 pending 工单。items=[{"tool","params"},...]。

    未知工具/参数不合格 → 抛 ChangeToolError（调用方拒建工单并给用户解释）；
    工单落库后**绝不执行**——执行只走 change_gate.execute_ticket 确认门。
    """
    if not items:
        raise ChangeToolError("变更清单为空")
    if len(items) > MAX_ITEMS:
        raise ChangeToolError(f"单次变更最多 {MAX_ITEMS} 项（收到 {len(items)} 项）")
    change_list = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            raise ChangeToolError("变更项必须是对象")
        tool = (it.get("tool") or "").strip()
        spec = CHANGE_TOOLS.get(tool)
        if spec is None:
            raise ChangeToolError(f"工具 {tool or '(空)'} 不在变更白名单内，拒绝执行")
        params = validate_params(spec["params_schema"], it.get("params") or {})
        check_fn = spec.get("check_fn")
        if check_fn is not None:
            check_fn(params, user)   # 构建期语义校验（黑名单/越权/取值），不过即拒建单
        sensitive = (spec["sensitive"](params) if callable(spec.get("sensitive"))
                     else bool(spec.get("sensitive")))
        ack_required = (spec["ack_required"](params) if callable(spec["ack_required"])
                        else bool(spec["ack_required"]))
        if sensitive:
            ack_required = True   # 敏感二次确认：凭证/密码/权限类一律「我已知晓」
        change_list.append({
            "idx": idx,
            "title": mask_secrets_text(item_title(spec, params)),
            "impact": spec["impact"],
            "rollback": spec["rollback"],
            "tool": tool,
            "params": encrypt_params(params),   # password/AK 类加密落库
            "ack_required": ack_required,
            "sensitive": sensitive,
            "status": "pending",
            "result": None,
        })
    n = len(change_list)
    ticket_title = mask_secrets_text((title or "").strip())[:200] or (
        change_list[0]["title"] + (f" 等 {n} 项" if n > 1 else ""))
    ticket = AgentChangeTicket(
        session_id=int(session_id or 0), msg_id=int(msg_id or 0),
        title=ticket_title, change_list_json=json.dumps(change_list, ensure_ascii=False),
        status="pending",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    from app.services import audit
    audit.record(
        db, action="agent.change.create",
        user_id=getattr(user, "id", None), username=getattr(user, "username", ""),
        resource_type="agent_change_ticket", resource_id=str(ticket.id),
        detail={"title": ticket_title, "items": [c["title"] for c in change_list],
                "ack_required": any(c["ack_required"] for c in change_list)},
    )
    db.commit()
    return ticket_out(ticket)


def ticket_out(ticket: AgentChangeTicket) -> dict:
    """工单出参（展示层）：change_list.params / result 里 password/AK 类一律掩码（****），
    密文也不回显——完整参数只在工单库（加密）与执行链路内部流转。"""
    try:
        change_list = json.loads(ticket.change_list_json or "[]")
    except ValueError:
        change_list = []
    result = None
    if ticket.result_json:
        try:
            result = json.loads(ticket.result_json)
        except ValueError:
            result = None
    change_list = [
        ({k: (mask_params(v) if k == "params" else mask_out(v) if k == "result" else v)
          for k, v in c.items()} if isinstance(c, dict) else c)
        for c in change_list]
    change_list = [_display_item(c) for c in change_list]   # code 项补充 diff_summary、截断和重启标记
    result = mask_out(result)
    diff_files = []
    for c in change_list:
        if isinstance(c, dict) and isinstance(c.get("diff_summary"), dict):
            diff_files += [f for f in (c["diff_summary"].get("files") or []) if isinstance(f, dict)]
    return {
        "id": ticket.id, "session_id": ticket.session_id, "msg_id": ticket.msg_id,
        "title": ticket.title, "status": ticket.status, "change_list": change_list,
        "diff_summary": {"files": diff_files},   # 工单级 diff，在前端项级数据缺失时兜底
        "confirmed_by": ticket.confirmed_by or "",
        "confirmed_by_id": getattr(ticket, "confirmed_by_id", 0) or 0,
        "confirmed_at": _dt(ticket.confirmed_at),
        "result": result,
        "created_at": _dt(ticket.created_at),
    }


# ---------------- ticket_out 展示层：code_modify 的 diff 预览、截断和重启标记 ----------------

def _trunc_diff_text(s) -> str:
    s = str(s or "")
    return s if len(s) <= _DIFF_TEXT_MAX else s[:_DIFF_TEXT_MAX] + f"…（截断，全文共 {len(s)} 字符）"


def _display_item(c: dict) -> dict:
    """code_modify 项展示补丁：①diff_summary（前端契约 {files:[{path,add_lines,del_lines,diff_head}]}，
    项内 plan.diff_summary 优先，旧工单现算兜底）②restart_required=True（需重启生效标记）
    ③diff 文本截断（edits.old/new 与全文 new；库里与执行链路仍是全文——前端注释明示
    "params.plan 可能被 ticket_out 截断，预览按到手数据渲染"）。非 code 项原样返回。"""
    if not isinstance(c, dict) or c.get("tool") != "code_modify":
        return c
    c = dict(c)
    params = dict(c.get("params") or {})
    plan = params.get("plan") if isinstance(params.get("plan"), dict) else {}
    diff = plan.get("diff_summary")
    if not (isinstance(diff, dict) and isinstance(diff.get("files"), list)):
        diff = plan_diff_summary(plan)   # 旧工单/异路建单：展示时现算兜底
    c["diff_summary"] = diff
    c["restart_required"] = True
    files_out = []
    for f in plan.get("files") or []:
        if not isinstance(f, dict):
            continue
        nf = {k: v for k, v in f.items() if k not in ("edits", "new")}
        if isinstance(f.get("edits"), list):
            nf["edits"] = [
                {"old": _trunc_diff_text(e.get("old", "")), "new": _trunc_diff_text(e.get("new", ""))}
                for e in f["edits"] if isinstance(e, dict)]
        if isinstance(f.get("new"), str):
            nf["new"] = _trunc_diff_text(f["new"])
        files_out.append(nf)
    if files_out:
        params["plan"] = {**plan, "files": files_out}
        c["params"] = params
    return c
