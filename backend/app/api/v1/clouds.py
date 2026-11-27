"""云账号管理 API：多厂商多账号台账 + 连通测试 + 批量同步。

替换原"同步阿里云"单点入口：/clouds/sync 按账号批量走 Provider 层，
账号凭证 AK/SK 双加密只进不出（响应只回掩码）。
危险面收口：账号增删改仅 admin；测试/同步 admin+operator；全部落审计。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of, t
from app.core.security import decrypt_secret, encrypt_secret
from app.models.cloud_account import CloudAccount
from app.models.user import User
from app.schemas.onboarding import RetryRequest, RetryScope
from app.services import audit, onboarding
from app.services.cloud.provider import (RESOURCE_TYPES, capabilities_of,
                                          get_provider, vendor_label, vendors_payload)
from app.services.cloud.sync_job import ONBOARDING_STATUSES

logger = logging.getLogger("qianshu.clouds")
router = APIRouter(prefix="/clouds", tags=["clouds"])


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    vendor: str = Field(min_length=1, max_length=16)
    ak: str = Field(default="", max_length=256)      # 自建可空
    sk: str = Field(default="", max_length=256)      # 自建可空
    region: str = Field(default="", max_length=64)
    extra: dict = Field(default_factory=dict)
    enabled: bool = True
    ack: str = Field(default="", max_length=16)      # 安全警示确认（"我已知晓"），服务端强制
    # 新建账号默认高优先级、三类提醒全开，保持升级前行为
    priority: str = Field(default="high", pattern="^(high|medium|low)$")
    notify_overview: bool = True
    notify_inapp: bool = True
    notify_push: bool = True
    repeat_days: Optional[int] = Field(default=None, ge=0, le=3650)   # None=继承优先级


class AccountPatchIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    ak: str = ""        # 空=不改（密钥只进不出，不回显）
    sk: str = ""        # 空=不改
    region: Optional[str] = Field(default=None, max_length=64)
    extra: Optional[dict] = None
    ack: str = ""       # 改凭证时重新确认
    enabled: Optional[bool] = None
    # 分级监控字段仅更新配置列，不得触发 resync。
    priority: Optional[str] = Field(default=None, pattern="^(high|medium|low)$")
    notify_overview: Optional[bool] = None
    notify_inapp: Optional[bool] = None
    notify_push: Optional[bool] = None
    repeat_days: Optional[int] = Field(default=None, ge=0, le=3650)   # 显式 null=继承


class TestIn(BaseModel):
    """未保存账号的连通测试（首登向导/新建对话框「测试可用性」用）。"""
    vendor: str = Field(min_length=1, max_length=16)
    ak: str = Field(default="", max_length=256)
    sk: str = Field(default="", max_length=256)
    region: str = Field(default="", max_length=64)
    extra: dict = Field(default_factory=dict)   # vSphere 主机/数据中心（extra.host/extra.datacenter）


def _mask_ak(ak: str) -> str:
    if not ak:
        return ""
    return (ak[:4] + "****" + ak[-4:]) if len(ak) > 10 else "****"


def _account_out(row: CloudAccount, db: Session | None = None, run=None) -> dict:
    ak = ""
    if row.ak_cipher:
        try:
            ak = _mask_ak(decrypt_secret(row.ak_cipher))
        except Exception:
            ak = "****"   # 解密失败不阻断列表（MASTER_KEY 变更场景）
    try:
        extra = json.loads(row.extra_json) if row.extra_json else {}
    except ValueError:
        extra = {}
    out = {
        "id": row.id, "name": row.name, "vendor": row.vendor,
        "vendor_label": vendor_label(row.vendor),
        # 前端按能力位禁用测试/同步入口，不再维护第二份厂商清单
        "capability": {"tier": capabilities_of(row.vendor).tier,
                       **asdict(capabilities_of(row.vendor))},
        "ak_masked": ak, "region": row.region, "extra": extra,
        # 数据分组迁移到台账的镜像行（无 AK、不参与采集）
        "bridge_group": extra.get("migrated_from") == "accounts_p22",
        "enabled": row.enabled, "status": row.status,
        "last_test_at": row.last_test_at.isoformat(timespec="seconds") if row.last_test_at else None,
        # 最近同步时间与自动规则播种状态供页面提示
        "last_sync_at": (row.last_sync_at.isoformat(timespec="seconds")
                         if getattr(row, "last_sync_at", None) else None),
        "alert_seeded": bool(getattr(row, "alert_seeded_at", None)),
        "created_at": row.created_at.isoformat(timespec="seconds"),
        "updated_at": row.updated_at.isoformat(timespec="seconds"),
        # 分级监控配置
        "priority": getattr(row, "priority", None) or "high",
        "notify_overview": bool(getattr(row, "notify_overview", True)),
        "notify_inapp": bool(getattr(row, "notify_inapp", True)),
        "notify_push": bool(getattr(row, "notify_push", True)),
        "repeat_days": getattr(row, "repeat_days", None),
        "last_metrics_at": (row.last_metrics_at.isoformat(timespec="seconds")
                            if getattr(row, "last_metrics_at", None) else None),
        "last_alert_eval_at": (row.last_alert_eval_at.isoformat(timespec="seconds")
                               if getattr(row, "last_alert_eval_at", None) else None),
        "last_host_check_at": (row.last_host_check_at.isoformat(timespec="seconds")
                               if getattr(row, "last_host_check_at", None) else None),
        "last_cost_at": (row.last_cost_at.isoformat(timespec="seconds")
                         if getattr(row, "last_cost_at", None) else None),
    }
    if db is not None:
        from app.services.monitor_policy import cadence_label, repeat_days
        out["repeat_effective"] = repeat_days(db, row.id)
        out["cadence"] = cadence_label(db, row.id)
        # 管理列表与向导/智能体使用同一就绪结果，避免 Run 有缺口而这里仍绿色正常。
        run = run or onboarding.get_run(db, row.id)
        out["onboarding_run_id"] = run.id if run else None
        out["onboarding_status"] = run.status if run else None
    return out


def has_placeholder_credential(value: str) -> bool:
    """拒绝明确的占位/假凭证；真实有效性仍由后续只读探活证明。"""
    normalized = str(value or "").strip().lower()
    return (
        normalized in {"pending", "placeholder", "dummy", "fake", "待补充", "占位", "xxxxxxxx"}
        or normalized.startswith(("agent-pending-", "placeholder-", "dummy-", "fake-",
                                  "your-access-key", "your_access_key"))
    )


def _validate_change_source_extra(extra: dict, vendor: str = "") -> None:
    from app.services.change_source import ChangeSourceError, validate_change_source_config
    try:
        validate_change_source_config(extra or {}, vendor=vendor)
    except ChangeSourceError as exc:
        raise HTTPException(400, str(exc))


def _validate_creds(body, lang: str = "zh") -> None:
    """需要云凭证的厂商必须成对提供 AK/SK（8 位起，对齐凭证表口径）。

    是否需要凭证由 provider 能力位唯一决定：自建/ESXi/vCenter/KVM 的账号密码
    语义不同，不按 AK/SK 长度校验。
    """
    try:
        get_provider(body.vendor)   # 未知厂商直接拒
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 裸机/IDC 不是云账号：没有可采集的云凭证，登记应走机器管理/资产台账
    if body.vendor == "onprem":
        raise HTTPException(400, t("cloud.onprem_not_here", lang))
    if not capabilities_of(body.vendor).needs_ak_sk:
        return
    if has_placeholder_credential(body.ak) or has_placeholder_credential(body.sk):
        raise HTTPException(400, "AK/SK 不能使用占位值，请提供真实只读凭证")
    if len(body.ak) < 8 or len(body.sk) < 8:
        raise HTTPException(400, t("cloud.ak_pair", lang))


# ---- 云凭证安全警示与免责审计 ----
# 保存凭证前必须输入「我已知晓」；确认记入审计日志（action=cloud.account.ack），
# 作为"平台已履行告知义务、用户已知悉用途与只读建议"的追责保护凭证。
CRED_WARN_VERSION = "v1-20261104"
ACK_TEXT = "我已知晓"


def _require_ack(ack: str, user, request, db: Session, op: str, resource_id: str) -> None:
    if (ack or "").strip() != ACK_TEXT:
        raise HTTPException(400, t("cloud.ack_required", lang_of(request)))
    audit.record(
        db, action="cloud.account.ack", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=resource_id,
        detail={"warn_version": CRED_WARN_VERSION, "ack": ACK_TEXT, "op": op,
                "use": "资产/账单采集、智能体分析与导入等场景", "readonly_advised": True},
        client_ip=get_client_ip(request),
    )


@router.get("/vendors")
def list_vendors(
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """厂商能力清单（前端唯一事实源）：档位 + 各能力位，供分组/禁用/文案使用。"""
    items = vendors_payload()
    return {"total": len(items), "items": items}


@router.get("/accounts")
def list_accounts(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    rows = db.query(CloudAccount).order_by(CloudAccount.id).all()
    from app.models.onboarding_run import OnboardingRun
    account_ids = [r.id for r in rows]
    runs = (db.query(OnboardingRun)
            .filter(OnboardingRun.account_id.in_(account_ids)).all()) if account_ids else []
    run_by_account = {r.account_id: r for r in runs}
    return {"total": len(rows),
            "items": [_account_out(r, db, run_by_account.get(r.id)) for r in rows]}


@router.get("/onboarding-runs")
def list_onboarding_runs(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """所有云账号的当前接入任务；向导和智能体读取同一持久状态。"""
    from app.models.onboarding_run import OnboardingRun

    runs = db.query(OnboardingRun).order_by(OnboardingRun.updated_at.desc()).all()
    account_ids = {r.account_id for r in runs}
    accounts = {a.id: a for a in db.query(CloudAccount).filter(CloudAccount.id.in_(account_ids)).all()} if account_ids else {}
    return {"total": len(runs), "items": [onboarding.serialize(r, accounts.get(r.account_id)) for r in runs]}


@router.get("/accounts/{aid}/sls-discovery")
def get_sls_discovery(
    aid: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """账号级完整目录和派生语义；大结果不塞进所有 Run 的轮询响应。"""
    from app.services import sls_discovery

    account = db.get(CloudAccount, aid)
    if account is None:
        raise HTTPException(404, "云账号不存在")
    return sls_discovery.get_discovery(onboarding.get_run(db, aid))


@router.post("/accounts/{aid}/sls-discovery", status_code=202)
def start_sls_discovery(
    aid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    from app.services import sls_discovery

    account = db.get(CloudAccount, aid)
    if account is None:
        raise HTTPException(404, "云账号不存在")
    try:
        result = sls_discovery.discover(aid, background=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit.record(db, action="cloud.sls_discover", user_id=user.id, username=user.username,
                 resource_type="cloud_account", resource_id=str(aid),
                 detail={"vendor": account.vendor}, client_ip=get_client_ip(request))
    db.commit()
    return result


@router.post("/accounts/{aid}/sls-discovery/skip")
def skip_sls_targets(
    aid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """明确暂不配置是用户决定，记为 skipped；不等于发现成功或目标已验证。"""
    from app.services import onboarding

    account = db.get(CloudAccount, aid)
    if account is None or account.vendor != "aliyun":
        raise HTTPException(404, "阿里云账号不存在")
    onboarding.record_sls_targets(db, account, skipped=True)
    audit.record(db, action="cloud.sls_targets_skip", user_id=user.id, username=user.username,
                 resource_type="cloud_account", resource_id=str(aid),
                 detail={"reason": "user_deferred"}, client_ip=get_client_ip(request))
    db.commit()
    return {"ok": True, "onboarding": onboarding.serialize(onboarding.get_run(db, aid), account)}


@router.post("/accounts")
def create_account(
    body: AccountIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    _validate_creds(body, lang_of(request))
    _validate_change_source_extra(body.extra, body.vendor)
    _require_ack(body.ack, user, request, db, op="create", resource_id=body.name)
    if db.query(CloudAccount).filter(CloudAccount.name == body.name).first():
        raise HTTPException(400, t("cloud.name_exists", lang_of(request)))
    row = CloudAccount(
        name=body.name, vendor=body.vendor,
        ak_cipher=encrypt_secret(body.ak) if body.ak else "",
        secret_cipher=encrypt_secret(body.sk) if body.sk else "",
        region=body.region,
        extra_json=json.dumps(body.extra, ensure_ascii=False),
        # 接入状态机：建号先进入 probing，同步与播种未完成前不得显示正常；
        # 未启用账号保持 untested（用户显式启停才动）
        enabled=body.enabled, status="probing" if body.enabled else "untested",
        priority=body.priority,
        notify_overview=body.notify_overview,
        notify_inapp=body.notify_inapp,
        notify_push=body.notify_push,
        repeat_days=body.repeat_days,
    )
    db.add(row)
    audit.record(
        db, action="cloud_account.create", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=body.name,
        detail={"vendor": body.vendor, "region": body.region},
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    run = onboarding.ensure_run(db, row)
    db.commit()
    if row.enabled:
        # 保存响应不阻塞，后台完成测试→资产同步→规则播种。
        # 独立 SessionLocal，不持有请求事务；失败只回写 status，页面可手动重试。
        from app.services.cloud.sync_job import bootstrap_account

        threading.Thread(
            target=bootstrap_account, args=(row.id,), daemon=True,
            name=f"cloud-bootstrap-{row.id}",
        ).start()
    out = _account_out(row, db)
    out["onboarding_run_id"] = run.id
    out["onboarding_status"] = run.status
    return out


@router.post("/accounts/test")
def test_unsaved(
    body: TestIn,
    request: Request,
    _user: User = Depends(require_roles("admin", "operator")),
):
    """连通测试（不保存）：向导逐厂商「测试可用性」按钮。"""
    lang = lang_of(request)
    _validate_creds(body, lang)
    _validate_change_source_extra(body.extra, body.vendor)
    if not capabilities_of(body.vendor).probe:
        return {"ok": False, "supported": False,
                "message": f"{vendor_label(body.vendor)}：本期仅支持登记，"
                           f"连通测试与采集能力随版本更新自动启用"}
    provider = get_provider(body.vendor)
    # 未保存实例仅作调用载体、不入库：provider 测试需要 region/extra 上下文（vSphere 主机等）
    transient = CloudAccount(name="未保存测试", vendor=body.vendor, region=body.region,
                             extra_json=json.dumps(body.extra, ensure_ascii=False))
    try:
        return provider.test_account(transient, body.ak, body.sk)
    except NotImplementedError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": _test_error_reason(str(e), lang)}


@router.get("/accounts/{aid}")
def get_account(
    aid: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    row = db.get(CloudAccount, aid)
    if row is None:
        raise HTTPException(404, t("cloud.not_found", lang_of(request)))
    return _account_out(row, db)


@router.get("/accounts/{aid}/onboarding")
def get_account_onboarding(
    aid: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    row = db.get(CloudAccount, aid)
    if row is None:
        raise HTTPException(404, t("cloud.not_found", lang_of(request)))
    run = onboarding.get_run(db, aid)
    if run is None:
        raise HTTPException(404, "该账号尚未建立接入任务")
    return onboarding.serialize(run, row)


@router.post("/accounts/{aid}/onboarding/retry", status_code=202)
def retry_onboarding_capabilities(
    aid: int,
    body: RetryRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """按账号×能力×范围恢复任务；同范围运行中直接返回当前 Run。"""
    row = db.get(CloudAccount, aid)
    if row is None:
        raise HTTPException(404, t("cloud.not_found", lang_of(request)))
    if not row.enabled:
        raise HTTPException(409, "账号已停用，不能启动接入任务")
    if capabilities_of(row.vendor).needs_ak_sk:
        try:
            ak = decrypt_secret(row.ak_cipher) if row.ak_cipher else ""
            sk = decrypt_secret(row.secret_cipher) if row.secret_cipher else ""
        except Exception as exc:
            raise HTTPException(400, "该账号凭证不可用，请由管理员重新提交") from exc
        if not ak or not sk:
            raise HTTPException(400, "该账号缺少凭证，请由管理员重新提交")

    # PostgreSQL 下锁定当前 Run；不会在请求线程传递凭证上下文。
    from app.models.onboarding_run import OnboardingRun
    db.query(OnboardingRun).filter(OnboardingRun.account_id == aid).with_for_update().first()
    prepared = onboarding.prepare_retries(
        db, row, [item.model_dump() for item in body.scopes])
    run = onboarding.get_run(db, aid)
    audit.record(
        db, action="cloud.sync", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=str(aid),
        detail={"accounts": [row.name], "onboarding_retry": {
            "started": prepared["started"],
            "reused": [x["capability"] for x in prepared["reused"]],
            "unchanged": [x["capability"] for x in prepared["unchanged"]],
        }}, client_ip=get_client_ip(request))
    db.commit()
    if prepared["started"]:
        from app.services.onboarding_tasks import retry_scopes
        threading.Thread(
            target=retry_scopes, args=(row.id, run.id, prepared["started"]),
            daemon=True, name=f"onboarding-retry-{row.id}",
        ).start()
    return {"run_id": run.id, "account_id": row.id, **prepared,
            "onboarding": onboarding.serialize(run, row)}


@router.put("/accounts/{aid}")
def update_account(
    aid: int,
    body: AccountPatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.get(CloudAccount, aid)
    if row is None:
        raise HTTPException(404, t("cloud.not_found", lang_of(request)))
    was_enabled = row.enabled
    if body.name and body.name != row.name:
        if db.query(CloudAccount).filter(CloudAccount.name == body.name).first():
            raise HTTPException(400, t("cloud.name_exists", lang_of(request)))
        row.name = body.name
    if body.ak or body.sk:
        _require_ack(body.ack, user, request, db, op="update_creds", resource_id=row.name)
        if has_placeholder_credential(body.ak) or has_placeholder_credential(body.sk):
            raise HTTPException(400, "AK/SK 不能使用占位值，请提供真实只读凭证")
        if capabilities_of(row.vendor).needs_ak_sk and (len(body.ak) < 8 or len(body.sk) < 8):
            raise HTTPException(400, t("cloud.ak_pair_keep", lang_of(request)))
        if body.ak:
            row.ak_cipher = encrypt_secret(body.ak)
        if body.sk:
            row.secret_cipher = encrypt_secret(body.sk)
    if body.region is not None:
        row.region = body.region
    if body.extra is not None:
        _validate_change_source_extra(body.extra, row.vendor)
        row.extra_json = json.dumps(body.extra, ensure_ascii=False)
    if body.enabled is not None:
        row.enabled = body.enabled
        # 停用时不能留着接入非终态（轮询会一直转，且实际已不采）
        if not row.enabled and row.status in ONBOARDING_STATUSES:
            row.status = "untested"
    # 分级监控配置：只改列，不算重新接入（不触发 resync/重采）
    if body.priority:
        row.priority = body.priority
    for dest in ("overview", "inapp", "push"):
        val = getattr(body, f"notify_{dest}")
        if val is not None:
            setattr(row, f"notify_{dest}", val)
    if "repeat_days" in body.model_fields_set:
        row.repeat_days = body.repeat_days     # 显式 null=继承优先级
    # 凭证/地址变更或重新启用等同"重新接入"：
    # 后台重测+同步+补播种（播种已一次性，不会重建用户删掉的规则）
    resync = bool(body.ak or body.sk or body.extra is not None or body.region is not None
                  or (row.enabled and not was_enabled))
    if resync and row.enabled:
        row.status = "probing"
        onboarding.reset_run(db, row)
    elif not row.enabled:
        onboarding.set_account_stage(db, row, "untested")
    row.updated_at = datetime.now()
    audit.record(
        db, action="cloud_account.update", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=row.name,
        detail={"fields": [k for k, v in body.model_dump().items() if v not in (None, "", {})]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    if resync and row.enabled:
        from app.services.cloud.sync_job import bootstrap_account

        threading.Thread(
            target=bootstrap_account, args=(row.id,), daemon=True,
            name=f"cloud-bootstrap-{row.id}",
        ).start()
    out = _account_out(row, db)
    run = onboarding.get_run(db, row.id)
    out["onboarding_run_id"] = run.id if run else None
    out["onboarding_status"] = run.status if run else None
    return out


@router.delete("/accounts/{aid}")
def delete_account(
    aid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.get(CloudAccount, aid)
    if row is None:
        raise HTTPException(404, t("cloud.not_found", lang_of(request)))
    name, vendor = row.name, row.vendor
    run = onboarding.get_run(db, aid)
    if run is not None:
        db.delete(run)
    db.delete(row)
    audit.record(
        db, action="cloud_account.delete", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=name,
        detail={"vendor": vendor},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}


def _test_error_reason(msg: str, lang: str = "zh") -> str:
    """SDK 报错转人话（与 settings.py 凭证测试同口径）。文案按 lang 出文。"""
    if "SignatureNotMatch" in msg:
        return t("cloud.sig_mismatch", lang)
    if "InvalidAccessKeyId" in msg or "NotFound" in msg:
        return t("cloud.bad_ak", lang)
    return msg[:120]


@router.post("/accounts/{aid}/test")
def test_account(
    aid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """已保存账号连通测试：走 provider.test_account，结果回写 status/last_test_at。"""
    lang = lang_of(request)
    row = db.get(CloudAccount, aid)
    if row is None:
        raise HTTPException(404, t("cloud.not_found", lang))
    try:
        ak = decrypt_secret(row.ak_cipher) if row.ak_cipher else ""
        sk = decrypt_secret(row.secret_cipher) if row.secret_cipher else ""
    except Exception:
        row.status = "error"
        row.last_test_at = datetime.now()
        onboarding.record_probe(db, row, ok=False, message="凭证解密失败")
        onboarding.set_account_stage(db, row, "error")
        db.commit()
        return {"ok": False, "message": t("cloud.decrypt_fail", lang)}
    if not capabilities_of(row.vendor).probe:
        # 无探活能力时确定性返回"仅登记"，不依赖 stub 抛 NotImplementedError
        result = {"ok": False, "supported": False,
                  "message": f"{vendor_label(row.vendor)}：本期仅支持登记，"
                             f"连通测试与采集能力随版本更新自动启用"}
        row.status = "unsupported"
        row.last_test_at = datetime.now()
        row.updated_at = datetime.now()
        onboarding.record_probe(db, row, ok=False, supported=False,
                                message=result["message"])
        onboarding.set_account_stage(db, row, "unsupported")
        audit.record(
            db, action="cloud_account.test", user_id=user.id, username=user.username,
            resource_type="cloud_account", resource_id=row.name, result="skipped",
            detail={"vendor": row.vendor, "reason": "vendor_not_supported"},
            client_ip=get_client_ip(request),
        )
        db.commit()
        return result
    provider = get_provider(row.vendor)
    try:
        result = provider.test_account(row, ak, sk)
        # 手动探活与后台接入并发时，探活成功不能把后续同步/播种阶段抹成正常。
        if not (result.get("ok") and row.status in ONBOARDING_STATUSES):
            row.status = "ok" if result.get("ok") else "error"
    except NotImplementedError as e:
        result = {"ok": False, "message": str(e)}
        row.status = "unsupported"
    except Exception as e:
        result = {"ok": False, "message": t("cloud.test_failed", lang, reason=_test_error_reason(str(e), lang))}
        row.status = "error"
    row.last_test_at = datetime.now()
    row.updated_at = datetime.now()
    onboarding.record_probe(db, row, ok=bool(result.get("ok")),
                            message=result.get("message", ""),
                            supported=result.get("supported", True))
    onboarding.set_account_stage(db, row, row.status)
    audit.record(
        db, action="cloud_account.test", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=row.name,
        result="ok" if result.get("ok") else "failed",
        detail={"vendor": row.vendor, "message": result.get("message", "")[:200]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result


@router.post("/sync")
def sync_cloud_assets(
    request: Request,
    account_id: int = 0,
    product: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """按账号批量同步（替换"同步阿里云"）。

    account_id=0 全部启用账号；product 为空=该厂商全部支持产品。
    单账号/单产品失败不阻断其余批次，逐条结果 + 汇总返回。
    """
    if product and product not in RESOURCE_TYPES and product not in ("domain", "ssl_cert"):
        raise HTTPException(400, t("cloud.sync_unsupported", lang_of(request),
                                   product=product, options=sorted(RESOURCE_TYPES)))
    q = db.query(CloudAccount).filter(CloudAccount.enabled == True)  # noqa: E712
    if account_id:
        q = q.filter(CloudAccount.id == account_id)
    accounts = q.order_by(CloudAccount.id).all()
    if not accounts:
        raise HTTPException(400, t("cloud.sync_no_accounts", lang_of(request)))

    # 同步实现收口 services/cloud/sync_job.py（手动/接入触发/定时三入口同源）
    from app.services.cloud.sync_job import sync_accounts

    out = sync_accounts(db, account_ids=[a.id for a in accounts], product=product,
                        force=True)   # 手动同步不受数据源优先级节奏限制
    results, summary = out["results"], out["summary"]
    # 存量账号补播种：账号在自动接入能力上线前就存在、或资产刚被
    # 归属自愈拉回本账号时，手动同步也补齐一次性自动规则（已播种/用户删了不复活）。
    from app.services.cloud.sync_job import seed_rules_for_account

    for a in accounts:
        # 部分产品失败仍可基于已有资产补播种；只有“没有任何产品成功返回”
        # 才跳过，避免一个可选产品失败把默认规则证据永久卡住。
        if (not capabilities_of(a.vendor).sync
                or a.id in summary.get("no_asset_accounts", [])):
            continue
        if getattr(a, "alert_seeded_at", None) is None:
            try:
                seed_rules_for_account(db, a)
            except Exception:
                logger.exception("账号 %s 自动规则播种失败", a.name)
                onboarding.record_seed_result(db, a, error="默认规则播种失败")
                onboarding.set_account_stage(db, a, a.status)
    audit.record(
        db, action="cloud.sync", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=product or "all",
        detail={"accounts": [a.name for a in accounts], "summary": {
            k: v for k, v in summary.items() if k != "failed_regions"}},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"results": results, "summary": summary}
