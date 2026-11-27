"""凭证管理与用户管理 API。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.credential import CloudCredential
from app.models.user import User
from app.schemas.settings import CredentialOut, UserCreate, UserUpdate
from app.services import audit

logger = logging.getLogger("qianshu.credentials")
router = APIRouter(tags=["settings"])


def _mask_ak(ak: str) -> str:
    if len(ak) <= 8:
        return ak[:2] + "****"
    return f"{ak[:8]}****{ak[-4:]}"


# 凭证写入口弃用提示：云账号管理是唯一凭证入口
_CRED_GONE = ("凭证管理已并入「云账号管理」（唯一凭证入口）：请在云账号管理中新增/编辑/测试云账号凭证；"
              "机器 SSH 凭证在「机器管理」维护")


# ---------------- 凭证：旧凭证表只读冻结，写入口 410 弃用 ----------------

@router.get("/credentials", response_model=list[CredentialOut])
def list_credentials(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """旧凭证只读摘要（设置页「凭证（已并入云账号管理）」页签专用）。

    旧凭证表数据已由 migrate_cloud_accounts_from_legacy 洗入云账号台账；
    本端点只读展示遗留行（AK 打码）+ 是否已并入台账（migrated）。
    增删改/揭示/测试均已 410 弃用——维护请到云账号管理。
    """
    import json as _json

    from app.models.cloud_account import CloudAccount

    rows = db.query(CloudCredential).order_by(CloudCredential.id).all()
    migrated_ids: set = set()
    for a in db.query(CloudAccount).all():
        try:
            extra = _json.loads(a.extra_json or "{}")
            if extra.get("legacy_credential_id") is not None:
                migrated_ids.add(int(extra["legacy_credential_id"]))
        except Exception:
            pass
    return [
        CredentialOut(
            id=r.id, name=r.name, vendor=r.vendor,
            access_key_id_masked=_mask_ak(r.access_key_id),
            remark=r.remark, is_default=r.is_default, enabled=r.enabled,
            migrated=r.id in migrated_ids,
            created_at=r.created_at, updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.post("/credentials")
def create_credential(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    raise HTTPException(status.HTTP_410_GONE, _CRED_GONE)


@router.put("/credentials/{cred_id}")
def update_credential(
    cred_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    raise HTTPException(status.HTTP_410_GONE, _CRED_GONE)


@router.delete("/credentials/{cred_id}")
def delete_credential(
    cred_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    raise HTTPException(status.HTTP_410_GONE, _CRED_GONE)


@router.get("/credentials/{cred_id}/secret")
def reveal_secret(
    cred_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """凭证明文揭示已弃用：云账号台账口径为密钥只进不出。"""
    raise HTTPException(status.HTTP_410_GONE, _CRED_GONE)


@router.post("/credentials/{cred_id}/test")
def test_credential(
    cred_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    raise HTTPException(status.HTTP_410_GONE, _CRED_GONE)


# ---------------- 用户 ----------------

@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    rows = db.query(User).order_by(User.id).all()
    return [
        {
            "id": r.id, "username": r.username, "display_name": r.display_name,
            "role": r.role, "is_active": r.is_active,
            "must_change_password": bool(r.must_change_password),
            "department_id": r.department_id,
            "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
        }
        for r in rows
    ]


@router.post("/users")
def create_user(
    body: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    from app.core.security import check_password_strength, generate_password, hash_password

    generated = not body.password
    password = body.password or generate_password()   # 留空即服务端生成一次性初始密码
    _weak = check_password_strength(password)
    if _weak:
        raise HTTPException(400, _weak)

    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    row = User(
        username=body.username,
        password_hash=hash_password(password),
        display_name=body.display_name or body.username,
        role=body.role,
        must_change_password=bool(body.must_change_password),   # 默认强制首登改密
    )
    db.add(row)
    db.flush()
    audit.record(
        db, action="user.create", user_id=user.id, username=user.username,
        resource_type="user", resource_id=row.id,
        detail={"new_username": body.username, "role": body.role,
                "password_source": "generated" if generated else "given"},
        client_ip=get_client_ip(request),
    )
    db.commit()
    out = {"id": row.id, "ok": True, "must_change_password": bool(body.must_change_password),
           "password_source": "generated" if generated else "given"}
    if generated:
        out["initial_password"] = password   # 一次性回显，此后无处可取（库存哈希）
    return out


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    from app.core.security import hash_password

    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    # 防呆：不允许把自己禁用或把自己降级（避免系统无人可管）
    if user_id == user.id:
        if body.is_active is False or (body.role is not None and body.role != "admin"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用或降级当前登录的管理员自己")
    if body.display_name is not None:
        row.display_name = body.display_name
    if body.role is not None:
        row.role = body.role
    if body.is_active is not None:
        row.is_active = body.is_active
    if body.password:
        row.password_hash = hash_password(body.password)
        row.must_change_password = True   # 管理员代设密码为一次性密码，一律首登改密，与重置口径一致
    audit.record(
        db, action="user.update", user_id=user.id, username=user.username,
        resource_type="user", resource_id=user_id,
        detail={"fields": [k for k, v in body.model_dump().items() if v is not None]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if user_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除当前登录用户自己")
    # 防呆：最后一个管理员不允许删除
    if row.role == "admin" and db.query(User).filter(User.role == "admin", User.is_active == True).count() <= 1:  # noqa: E712
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统至少需要保留一个可用的管理员")
    username = row.username
    db.delete(row)
    audit.record(
        db, action="user.delete", user_id=user.id, username=user.username,
        resource_type="user", resource_id=user_id, detail={"deleted_username": username},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """重置登录密码：服务端生成一次性密码，只在本次响应回显一次；
    强制首登改密并写审计，密码本身绝不入审计。"""
    from app.core.security import generate_password, hash_password

    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    temp_password = generate_password()
    row.password_hash = hash_password(temp_password)
    row.must_change_password = True   # 重置一律强制首登改密
    db.flush()
    audit.record(
        db, action="user.password.reset", user_id=user.id, username=user.username,
        resource_type="user", resource_id=user_id,
        detail={"target_username": row.username, "must_change_password": True},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {
        "id": row.id,
        "username": row.username,
        "temporary_password": temp_password,   # 一次性回显，此后无处可取（库存哈希）
        "must_change_password": True,
    }

# ---------------- 数据保留 · 系统参数 ----------------
# 7 个保留天数一个全局保存：env_store 持久化（照抄既有系统参数模式）+ settings 单例热更
# + 审计 settings.update + 保存即按新规则清存量（与 K8s 页「立即清存量」口径一致）。
# VM/VL **实时生效**（见 docs/active/deployment.md）：
# 查询口径过滤（retention.clamp_start_ts）+ 后台渐进清理（services/engine_retention.py
# 死序列清理/删除任务），保存即清存量；上限层 VM_RETENTION_MAX_DAYS/VL_RETENTION_MAX_DAYS
# 是部署时定的引擎上限（容器 -retentionPeriod），配置超上限才需重建容器。


class RetentionParamsIn(BaseModel):
    vm_days: int = Field(ge=0, le=3650)      # VM 指标（≤引擎上限内实时生效，0=永久至上限）
    vl_days: int = Field(ge=0, le=3650)      # VL 日志（同上）
    k8s_days: int = Field(ge=0, le=3650)     # K8s 容器日志
    llm_days: int = Field(ge=0, le=3650)     # LLM 调用明细
    audit_days: int = Field(ge=0, le=3650)   # 审计
    msg_days: int = Field(ge=0, le=3650)     # 站内信
    event_days: int = Field(ge=0, le=3650)   # 事件类（只删已收口）


_RETENTION_ENV_KEYS = {
    "vm_days": "VM_RETENTION_DAYS",
    "vl_days": "VL_RETENTION_DAYS",
    "k8s_days": "K8S_LOG_RETENTION_DAYS",
    "llm_days": "RETENTION_LLM_DAYS",
    "audit_days": "RETENTION_AUDIT_DAYS",
    "msg_days": "RETENTION_MSG_DAYS",
    "event_days": "RETENTION_EVENT_DAYS",
}


@router.get("/settings/retention")
def get_retention_params(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """当前保留天数（动态取值：.env 当前值 > settings，见 services/retention.py）
    + caps 引擎上限（部署时定，前端用于输入上限与文案）。"""
    from app.services import retention

    return {**retention.all_values(), "caps": retention.caps()}


@router.put("/settings/retention")
def put_retention_params(body: RetentionParamsIn, request: Request,
                         db: Session = Depends(get_db),
                         user: User = Depends(require_roles("admin"))):
    """保存保留策略：持久化 .env + 热生效 + 立即按新规则清存量（0=永久至引擎上限）。

    VM/VL 实时生效（查询口径 + 引擎渐进清理）；配置超过部署上限（caps）直接 400——
    调大上限属引擎启动参数（-retentionPeriod），需重建容器，此处如实拦截。"""
    from app.core.config import settings as st
    from app.services import engine_retention, retention as _ret
    from app.services.db_maint import prune_old_data
    from app.services.env_store import set_keys
    from app.services.k8s_monitor import cleanup_logs

    values = body.model_dump()
    # 0) 上限层校验：用户配置只在 ≤引擎上限 内生效（超上限=要重建容器，直接拒）
    caps = _ret.caps()
    for field, cap in (("vm_days", caps["vm"]), ("vl_days", caps["vl"])):
        if cap > 0 and values[field] > cap:
            raise HTTPException(
                400,
                f"{field} 超过存储引擎上限 {cap} 天（部署时定的 -retentionPeriod）。"
                f"调大上限需重建容器，请改为 ≤{cap} 天，或联系运维调上限后重建")
    # 1) settings 单例热更（同进程立即生效）+ .env 持久化（跨 worker/重启）
    for field, env_key in _RETENTION_ENV_KEYS.items():
        setattr(st, env_key, values[field])
    persisted = set_keys({_RETENTION_ENV_KEYS[k]: v for k, v in values.items()})
    # 2) 审计（settings.update）：值不含敏感信息
    audit.record(
        db, action="settings.update", user_id=user.id, username=user.username,
        resource_type="settings", resource_id="retention",
        detail={"scope": "retention", "values": values, "persisted": persisted},
        client_ip=get_client_ip(request),
    )
    db.commit()
    # 3) 立即按新规则清存量（"不是只影响新数据"；与 K8s 页既有口径一致）：
    #    库表（db_maint）+ K8s 日志 + VM/VL 引擎超期数据（渐进清理）
    cleaned_k8s = cleanup_logs(db)
    cleaned = prune_old_data(db)
    db.commit()
    cleaned = {k: v for k, v in cleaned.items() if v}
    # 引擎清理小预算同步跑（新超期段都在顶段增量里，3 片足够；积压由周期循环补完），
    # 保证保存接口不因大积压拖慢
    engine = engine_retention.clean_all(max_chunks=3)
    return {"ok": True, "values": values, "persisted": persisted,
            "cleaned": {"k8s_pod_logs": cleaned_k8s, **cleaned},
            "engine": engine, "caps": caps}

# ---------------- 产品卡片（产品集由配置下发，前端不再写死） ----------------

@router.get("/settings/product-cards")   # 前端约定路径（本路由无 prefix，直挂 /api 下）
def get_product_cards(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    import json
    from app.core.config import settings

    try:
        rows = json.loads(settings.PRODUCT_CARDS)
        return [{"key": r[0], "label": r[1], "icon": r[2], "color": r[3]} for r in rows if len(r) >= 4]
    except Exception:
        logger.warning("PRODUCT_CARDS 配置解析失败，返回空列表（前端走内置默认）")
        return []


# ---------------- 组件识别规则（拓扑架构图配置化 UI） ----------------

@router.get("/settings/components")
def get_components(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    import json
    import os

    from app.services.topology import load_component_rules

    path = os.path.join(os.getcwd(), "config", "components.json")
    return {"path": path, "rules": load_component_rules(force=True),
            "exists": os.path.exists(path)}


class ComponentsIn(BaseModel):
    rules: dict


@router.put("/settings/components")
def put_components(body: ComponentsIn, request: Request,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    """保存组件识别规则到 config/components.json（立即生效，拓扑按新规则聚类）。"""
    import json
    import os

    rules = body.rules
    for k in ("product_role", "name_rules", "clustered_components"):
        if k not in rules:
            raise HTTPException(400, f"缺少字段: {k}")
    # kind_role 可选，形如 {组件类型: [角色, 组件名]}——
    # 缺省由内置 COMPONENT_KINDS 语义兜底（load_component_rules setdefault）
    if "kind_role" in rules and not isinstance(rules["kind_role"], dict):
        raise HTTPException(400, "kind_role 需为对象：{组件类型: [角色, 组件名]}")
    # 基本校验：name_rules 的 pattern 是合法正则
    import re as _re
    for r in rules["name_rules"]:
        try:
            _re.compile(r.get("pattern", ""))
        except _re.error as e:
            raise HTTPException(400, f"正则不合法: {r.get('pattern')}（{e}）")

    os.makedirs("config", exist_ok=True)
    path = os.path.join(os.getcwd(), "config", "components.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

    from app.services.topology import load_component_rules
    load_component_rules(force=True)   # 热加载
    audit.record(
        db, action="settings.components_update", user_id=user.id, username=user.username,
        resource_type="config", resource_id="components.json",
        detail={"name_rules": len(rules["name_rules"])},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "reloaded": True}


# ---------------- 数据分组 ----------------
# 语义解耦：本表=数据归属分组（展示/顶栏视图切换/授权计数）；
# 数据列 account_id 显式指向 cloud_accounts（分组经 bridge_group_to_cloud 洗入台账保 id）。

class AccountIn(BaseModel):
    name: str = Field(max_length=64)
    kind: str = Field(default="cloud", pattern="^(cloud|idc)$")
    remark: str = Field(default="", max_length=255)


@router.get("/settings/accounts")
def list_accounts(db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator", "viewer"))):
    from app.models.account import Account

    rows = db.query(Account).order_by(Account.id).all()
    if not rows:
        # 默认账号种子（单账号部署无感）
        a = Account(id=1, name="默认账号", kind="cloud", remark="首个账号")
        db.add(a)
        db.commit()
        rows = [a]
    # 双概念唯一化：分组同步洗入云账号台账（同 id 镜像，幂等），数据列单一指向台账
    from app.services.tenant import bridge_group_to_cloud

    for r in rows:
        bridge_group_to_cloud(db, r)
    db.commit()
    return [{"id": r.id, "name": r.name, "kind": r.kind, "remark": r.remark} for r in rows]


@router.get("/settings/license")
def get_license(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """授权状态（社区/企业）。"""
    from app.services.license import load_license

    return load_license()


@router.post("/settings/accounts")
def create_account(body: AccountIn, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    from app.models.account import Account
    from app.services.license import check_account_limit

    _lim = check_account_limit(db)
    if not _lim["ok"]:
        raise HTTPException(403, _lim["message"])
    if db.query(Account).filter(Account.name == body.name).first():
        raise HTTPException(400, "数据分组名已存在")
    a = Account(**body.model_dump())
    db.add(a)
    db.flush()
    # 新分组同步进云账号台账（同 id 镜像），account_id 语义单轴
    from app.services.tenant import bridge_group_to_cloud

    bridge_group_to_cloud(db, a)
    db.commit()
    return {"id": a.id, "name": a.name, "kind": a.kind}


@router.put("/settings/accounts/{aid}/credential")
def assign_credential(aid: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_roles("admin"))):
    """凭证→数据分组归属已弃用。

    账号归属由云账号台账承载（数据分组仅作归属标签，经 bridge_group_to_cloud
    洗入台账保 id），旧"默认凭证挂分组"语义已废止。
    """
    raise HTTPException(status.HTTP_410_GONE,
                        "凭证与数据分组的归属已由云账号台账承载：请在云账号管理维护账号凭证")


# ---------------- Demo 沙箱 + 版本 ----------------

@router.post("/settings/seed-demo")
def seed_demo(db: Session = Depends(get_db),
              user: User = Depends(require_roles("admin"))):
    from app.services.demo_seed import seed_demo as _seed

    return _seed(db)


@router.delete("/settings/seed-demo")
def clear_demo(db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin"))):
    from app.services.demo_seed import clear_demo as _clear

    return _clear(db)


@router.get("/settings/version")
def version():
    """版本信息（升级通道用）。"""
    return {"name": "千枢 QianShu", "version": "0.3.1",
            "knowledge_version": __import__("app.services.playbooks", fromlist=["KNOWLEDGE_VERSION"]).KNOWLEDGE_VERSION,
            "build": "2026-11-12"}


# ---------------- 数据源配置 ----------------

# 数据源配置约束：
# - builtin-vm（千枢自带 VictoriaMetrics）不进用户可配列表，单独标注"系统内置指标库"，
#   保存时强制保留（不可删改），避免用户把自带指标库从配置里抹掉。
# - 类型白名单覆盖常见开源监控（指标：Prometheus/VM/Zabbix/InfluxDB/Graphite/ES/
#   OpenTSDB/自定义 HTTP；日志：Loki/SLS/ES/Graylog/Filebeat/自定义/APM）；
#   未实现适配器的类型明确降级为"适配器排期"（services/datasource.py），不伪装实现。
# - 探活统一真实获取版本/系统信息；查询适配状态与探活解耦
#   （prom/vm 全量、zabbix 最新监控项值、loki LogQL，其余排期）。
# - 可选凭证字段 user/password/token（Zabbix user.login / API Token），headers 走 HTTP 鉴权。

BUILTIN_METRIC_SOURCE = {"name": "builtin-vm", "type": "vm", "url": ""}
METRIC_SOURCE_TYPES = ("prometheus", "vm", "zabbix", "influxdb", "graphite",
                       "es", "opentsdb", "http")
LOG_SOURCE_TYPES = ("loki", "sls", "cls", "es", "graylog", "filebeat", "http",
                    "skywalking", "zipkin")


def _is_builtin(row) -> bool:
    return isinstance(row, dict) and row.get("type") == "vm" and row.get("name") == "builtin-vm"


class DatasourceConfigIn(BaseModel):
    metric_sources: list = Field(default_factory=list)
    log_sources: list = Field(default_factory=list)


@router.get("/settings/datasource-config")
def get_datasource_config(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    import json

    from app.core.config import settings as st

    def _p(raw):
        try:
            return json.loads(raw or "[]")
        except Exception:
            return []
    metric = _p(st.METRIC_SOURCES)
    builtin = next((r for r in metric if _is_builtin(r)), None)
    return {
        # builtin-vm 从用户可配列表剔除，以 builtin_metric_source 单独说明
        "metric_sources": [r for r in metric if not _is_builtin(r)],
        "log_sources": _p(st.LOG_SOURCES),
        "builtin_metric_source": dict(builtin or BUILTIN_METRIC_SOURCE,
                                      label="系统内置指标库（千枢自带）"),
    }


@router.put("/settings/datasource-config")
def put_datasource_config(body: DatasourceConfigIn, request: Request,
                          db: Session = Depends(get_db),
                          user: User = Depends(require_roles("admin"))):
    """保存数据源配置：热生效 + 写 .env 持久化。类型走白名单校验，builtin-vm 强制保留。"""
    import json

    from app.core.config import settings as st

    def _clean(rows, allowed, kind_label):
        out = []
        for i, raw in enumerate(rows):
            if not isinstance(raw, dict):
                raise HTTPException(400, f"{kind_label}第 {i + 1} 条格式不合法")
            name = str(raw.get("name") or "").strip()
            typ = str(raw.get("type") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not name:
                raise HTTPException(400, f"{kind_label}第 {i + 1} 条缺名称")
            if typ not in allowed:
                raise HTTPException(
                    400, f"{kind_label}「{name}」类型「{typ}」不在支持列表：{', '.join(allowed)}")
            if not url:
                raise HTTPException(400, f"{kind_label}「{name}」缺访问地址 URL")
            # headers=HTTP 鉴权（如云托管 Prom 的 Authorization）；user/password/token=
            # Zabbix 等源的查询适配器凭证（user.login 或 API Token；探活不需要）
            row = {k: v for k, v in raw.items()
                   if k in ("name", "type", "url", "headers", "user", "password", "token", "verify_ssl")}
            row.update({"name": name, "type": typ, "url": url,
                        "verify_ssl": bool(raw.get("verify_ssl", False))})
            out.append(row)
        return out

    metric_rows = _clean([r for r in body.metric_sources if not _is_builtin(r)],
                         METRIC_SOURCE_TYPES, "指标源")
    log_rows = _clean(body.log_sources, LOG_SOURCE_TYPES, "日志源")

    # 内置 VM 无论前端是否回传都保住（千枢自带指标库不可被删/改）
    try:
        existing = json.loads(st.METRIC_SOURCES or "[]")
    except Exception:
        existing = []
    builtin = next((r for r in existing if _is_builtin(r)), dict(BUILTIN_METRIC_SOURCE))

    values = {
        "METRIC_SOURCES": json.dumps([builtin] + metric_rows, ensure_ascii=False),
        "LOG_SOURCES": json.dumps(log_rows, ensure_ascii=False),
    }
    for k, v in values.items():
        setattr(st, k, v)
    from app.services.env_store import set_keys
    persisted = set_keys(values)
    return {"ok": True, "persisted": persisted}


@router.get("/settings/public/version")
def public_version():
    """公开版本接口（Agent 升级检查用，无需登录）。"""
    return {"version": "0.3.1", "agent_version": "1.1.0", "build": "2026-11-12"}


# ---------------- 访问域名白名单：保存即动态生效 ----------------
import os as _os

_ALLOWED_HOSTS_FILE = _os.path.normpath(_os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))),
    "..", "frontend", "vite.allowed-hosts.json"))


class AllowedHostsIn(BaseModel):
    hosts: list = []   # 域名列表；空=全放行（内网开箱）


@router.get("/settings/allowed-hosts")
def get_allowed_hosts(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    import json as _j

    try:
        arr = _j.loads(open(_ALLOWED_HOSTS_FILE, encoding="utf-8").read() or "[]")
    except Exception:
        arr = []
    return {"hosts": arr, "mode": "whitelist" if arr else "open",
            "note": "空列表=任意域名可访问（内网默认）；配置后=仅白名单+本机可访问"}


@router.put("/settings/allowed-hosts")
def put_allowed_hosts(body: AllowedHostsIn, request: Request,
                      user: User = Depends(require_roles("admin")),
                      db: Session = Depends(get_db)):
    """保存访问域名并**动态生效**：写白名单文件 + touch vite.config.js——
    vite 监听配置变化自动秒级重载（无需手动重启）。"""
    import json as _j
    import os as _os
    import re as _re

    from app.services import audit as audit_svc

    hosts = []
    for h in body.hosts:
        h = str(h).strip().lower()
        if not h:
            continue
        if not _re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,250}", h):
            raise HTTPException(400, f"域名非法: {h}")
        hosts.append(h)
    hosts = sorted(set(hosts))
    try:
        with open(_ALLOWED_HOSTS_FILE, "w", encoding="utf-8") as f:
            _j.dump(hosts, f, ensure_ascii=False)
        cfg = _os.path.join(_os.path.dirname(_ALLOWED_HOSTS_FILE), "vite.config.js")
        _os.utime(cfg, None)   # touch：vite 自动重载配置
    except Exception as e:
        raise HTTPException(500, f"写入失败: {str(e)[:80]}")
    audit_svc.record(db, action="settings.allowed_hosts", user_id=user.id,
                     username=user.username, resource_type="settings",
                     resource_id="allowed-hosts", detail={"hosts": hosts},
                     client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True, "hosts": hosts,
            "note": "已保存并触发热重载——新域名立即可访问（无需重启）"}


# ---------------- 节假日日历：基线分析的"同日型"依据可视化 ----------------

@router.get("/settings/holiday")
def holiday_status(year: str = "", user: User = Depends(require_roles("admin", "operator", "viewer")),
                   db: Session = Depends(get_db)):
    """节假日日历：同步状态 + 明细（法定节假日/调休）——基线分析按同日型比较的依据。"""
    from app.models.holiday_calendar import HolidayCalendar

    q = db.query(HolidayCalendar)
    year = str(year or "").strip()
    if year.isdigit() and len(year) == 4:
        q = q.filter(HolidayCalendar.day >= f"{year}-01-01", HolidayCalendar.day <= f"{year}-12-31")
    rows = q.order_by(HolidayCalendar.day).all()
    items = [{"day": r.day.isoformat(), "kind": r.kind, "name": r.name, "source": r.source}
             for r in rows]
    holidays = [i for i in items if i["kind"] == "holiday"]
    workdays = [i for i in items if i["kind"] == "workday"]   # 调休补班
    years = sorted({i["day"][:4] for i in items})
    last_sync = max((i["day"] for i in items), default="")
    return {"items": items, "total": len(items),
            "holidays": len(holidays), "adjusted_workdays": len(workdays),
            "years": years, "last_sync": last_sync,
            "source": "holiday-cn（国务院安排镜像）",
            "usage": "成本/CDN/画像的基线分析按同日型比较：工作日比工作日、节假日比节假日、调休按官方安排归类"}


@router.post("/settings/holiday/sync")
def holiday_sync_now(request: Request, user: User = Depends(require_roles("admin")),
                     db: Session = Depends(get_db)):
    """立即同步节假日（拉取国务院安排）。"""
    from app.services import audit as audit_svc

    try:
        from app.services.holiday import sync_holidays

        n = sync_holidays(db)
    except Exception as e:
        raise HTTPException(500, f"同步失败: {str(e)[:100]}")
    audit_svc.record(db, action="settings.holiday_sync", user_id=user.id, username=user.username,
                     resource_type="settings", resource_id="holiday", detail={"rows": n},
                     client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True, "result": n}


# ---------------- SDK 治理：云 SDK 版本守望（锁版本+周扫描+人工升级） ----------------

@router.get("/settings/sdk-report")
def sdk_report(db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """最近一次 SDK 版本扫描结果（每周一 09:00 自动扫，或手动触发）。"""
    from app.services.sdk_governance import get_latest_report

    return get_latest_report(db)


@router.post("/settings/sdk-report/scan")
def sdk_report_scan(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_roles("admin", "operator"))):
    """立即扫描：拉 PyPI 最新版对比当前版本（只读外部查询，不升级任何包）。"""
    from app.services import audit as audit_svc
    from app.services.sdk_governance import run_scan

    out = run_scan(db, trigger="manual")
    audit_svc.record(
        db, action="settings.sdk_scan", user_id=user.id, username=user.username,
        resource_type="settings", resource_id="sdk-report",
        detail={"note": f"可升级 {out['outdated']} / 废弃信号 {out['deprecated']} / 共 {out['total']}"},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return out
