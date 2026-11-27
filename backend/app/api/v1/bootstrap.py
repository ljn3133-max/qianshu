"""安装向导与底座体检 API。

鉴权策略：
- GET /status          公开（只暴露初始化状态布尔量，无敏感信息）
- GET /report          需登录（含资产/账单计数等环境概况）
- POST /probe          传了 AK/SK 时公开（凭证只用于本次探测，不落库不缓存）；
                       未传时用已存凭证，需登录（防止匿名消耗平台凭证）
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import bearer, get_current_user, require_roles
from app.models.user import User
from app.services import bootstrap as bootstrap_svc

logger = logging.getLogger("qianshu.bootstrap_api")
router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])


def _optional_user(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """带 token 则要求有效，不带则匿名（向导首步无登录态）。"""
    if cred is None:
        return None
    try:
        return get_current_user(cred, db)
    except HTTPException:
        raise


@router.get("/status")
def bootstrap_status(db: Session = Depends(get_db)):
    import os

    from app.core.config import settings as st
    from app.models.cloud_account import CloudAccount
    from app.models.user import User as U
    from app.models.credential import CloudCredential

    # 凭证主源为云账号台账（启用中且带密钥的 aliyun），
    # 旧凭证表只读兜底（旧行为向后兼容：旧表空时行为不变）
    has_db_cred = (
        db.query(CloudAccount)
        .filter(CloudAccount.enabled == True,  # noqa: E712
                CloudAccount.vendor == "aliyun",
                CloudAccount.ak_cipher != "", CloudAccount.secret_cipher != "")
        .count() > 0
        or db.query(CloudCredential)
        .filter(CloudCredential.enabled == True,  # noqa: E712
                CloudCredential.vendor == "aliyun").count() > 0
    )
    has_env_cred = bool(os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
                        and os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET"))
    n_accounts = db.query(CloudAccount).count()
    # 停用账号仍算「已接入」（不改变 init_done 语义），但就绪摘要要能看出启用数，
    # 避免账号总数被理解为全部都在采集。
    n_enabled = (db.query(CloudAccount)
                 .filter(CloudAccount.enabled == True).count())  # noqa: E712
    # init_done 只表示“首次向导不再强制出现”，不能当作云接入/数据面就绪。
    # 路由守卫继续用它防锁死；真实账号接入结果由下方独立字段和 Onboarding Run 表达。
    init_done = n_accounts > 0 or str(getattr(st, "BOOTSTRAP_INIT_DONE", "0")) == "1"
    # 就绪只认共享 Run 的完整证据，不能再看账号 ok；手动探活成功也可能仍缺资产/规则。
    from app.models.onboarding_run import OnboardingRun
    enabled_ids = [row.id for row in db.query(CloudAccount.id)
                   .filter(CloudAccount.enabled == True).all()]  # noqa: E712
    runs = (db.query(OnboardingRun)
            .filter(OnboardingRun.account_id.in_(enabled_ids)).all()) if enabled_ids else []
    accounts_ready = sum(run.status == "ready" for run in runs)
    accounts_running = sum(run.status == "running" for run in runs)
    from app.services import onboarding
    accounts = {row.id: row for row in db.query(CloudAccount).filter(CloudAccount.id.in_(enabled_ids)).all()} if enabled_ids else {}
    capability_summary = {
        name: {state: 0 for state in ("available", "no_data", "running", "attention", "unverified")}
        for name in ("assets", "metrics", "logs", "cost", "changes")
    }
    for run in runs:
        for capability in onboarding.capability_results(run, accounts.get(run.account_id)):
            if capability["execution"] in ("running", "waiting"):
                state = "running" if capability["execution"] == "running" else "unverified"
            elif capability["execution"] == "failed" or capability["data_state"] == "stale":
                state = "attention"
            elif capability["data_state"] == "available":
                state = "available"
            elif capability["data_state"] == "no_data":
                state = "no_data"
            else:
                state = "unverified"
            capability_summary[capability["capability"]][state] += 1
    return {
        "initialized": db.query(U).count() > 0,
        "has_admin": db.query(U).filter(U.role == "admin").count() > 0,
        "has_credential": has_db_cred or has_env_cred,
        "credential_source": "db" if has_db_cred else ("env" if has_env_cred else "none"),
        "init_done": init_done,
        "accounts_total": n_accounts,
        "accounts_enabled": n_enabled,
        "onboarding_ready": n_enabled > 0 and accounts_ready == n_enabled,
        "accounts_ready": accounts_ready,
        "accounts_running": accounts_running,
        "accounts_with_gaps": max(0, n_enabled - accounts_ready - accounts_running),
        "capability_summary": capability_summary,
    }


@router.post("/complete")
def bootstrap_complete(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """显式完成初始化（零账号接入的场景）：置 init_done 并持久化。

    向导"暂不接入，完成初始化"按钮走这里，避免无账号可存的用户被路由守卫
    永远强跳回向导。
    """
    from app.core.config import settings as st
    from app.models.cloud_account import CloudAccount
    from app.services import audit as audit_svc

    st.BOOTSTRAP_INIT_DONE = "1"
    from app.services.env_store import set_keys
    persisted = set_keys({"BOOTSTRAP_INIT_DONE": "1"})
    audit_svc.record(
        db, action="bootstrap.complete", user_id=user.id, username=user.username,
        resource_type="config", resource_id="init",
        detail={"accounts_total": db.query(CloudAccount).count()},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "init_done": True, "persisted": persisted}


class ProbeRequest(BaseModel):
    ak: Optional[str] = Field(default=None, max_length=128,
                              description="临时 AK（可选；不传则用平台已存凭证，需登录）")
    sk: Optional[str] = Field(default=None, max_length=128)


@router.post("/probe")
def bootstrap_probe(
    body: ProbeRequest,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(_optional_user),
):
    ak, sk = (body.ak or "").strip(), (body.sk or "").strip()
    if not ak:
        # 未传凭证 -> 必须登录后用平台已存凭证
        if user is None:
            raise HTTPException(status_code=401, detail="请登录后探测，或在向导中直接填 AK/SK")
        from app.collector.credential import resolve_aliyun_credential, CredentialError
        try:
            ak, sk, _ = resolve_aliyun_credential(db)
        except CredentialError as e:
            raise HTTPException(status_code=400, detail=f"平台无可用凭证：{e}")
    try:
        faces = bootstrap_svc.probe_five_faces(ak, sk)
    except Exception as e:
        logger.warning("五面探测异常: %s", str(e)[:200])
        raise HTTPException(status_code=502, detail=f"探测失败：{str(e)[:120]}")
    return {"faces": faces, "probed_at": datetime.now().isoformat(timespec="seconds")}


@router.get("/report")
def bootstrap_report(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return bootstrap_svc.local_report(db)

# ---------------- 补数据闭环：SLS 目录 + 配置写回 ----------------

class SlsTarget(BaseModel):
    """[[project, logstore], ...] 或 [[project, logstore, chain], ...]"""
    project: str = Field(min_length=1, max_length=128)
    logstore: str = Field(min_length=1, max_length=128)
    chain: str = Field(default="", pattern="^(|outer|inner)$")


class ApplyConfigRequest(BaseModel):
    account_id: int = Field(default=0, ge=0,
                            description="SLS 目标归属云账号；多账号环境必须显式指定")
    slb_targets: list[SlsTarget] = Field(default_factory=list,
                                         description="SLB 访问日志目标（带 outer/inner）")
    app_log: list[str] = Field(default_factory=list, min_length=0, max_length=2,
                               description="[project, logstore] 应用日志")
    cdn_target: list[str] = Field(default_factory=list, min_length=0, max_length=2,
                                  description="[project, logstore] CDN 实时日志")


def _resolve_ak_sk(db: Session, body_ak: str = "", body_sk: str = "",
                   account_id: int = 0) -> tuple:
    if body_ak and body_sk:
        return body_ak, body_sk
    from app.collector.credential import resolve_aliyun_credential, CredentialError
    from app.core.security import decrypt_secret
    from app.models.cloud_account import CloudAccount

    if account_id:
        account = db.get(CloudAccount, account_id)
        if account is None or account.vendor != "aliyun" or not account.enabled:
            raise HTTPException(400, "SLS 目录账号不存在、已停用或不是阿里云账号")
        try:
            return decrypt_secret(account.ak_cipher), decrypt_secret(account.secret_cipher)
        except Exception as exc:
            raise HTTPException(400, "该账号凭证不可用") from exc
    try:
        ak, sk, _ = resolve_aliyun_credential(db)
        return ak, sk
    except CredentialError as e:
        raise HTTPException(status_code=400, detail=f"无可用凭证：{e}")


@router.get("/sls-catalog")
def sls_catalog(
    account_id: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """兼容的高级手选目录：不截断，逐项保留失败；新手主路径使用账号级语义发现。"""
    from app.collector.sls import list_logstores, list_projects_detailed
    from app.models.cloud_account import CloudAccount
    from app.services.sls_discovery import account_sls_endpoint

    ak, sk = _resolve_ak_sk(db, account_id=account_id)
    endpoint = account_sls_endpoint(db.get(CloudAccount, account_id)) if account_id else ""
    listing = list_projects_detailed(ak, sk, endpoint=endpoint)

    def _stores(proj: str) -> dict:
        try:
            return {"project": proj, "status": "ready",
                    "logstores": list_logstores(ak, sk, proj, endpoint=endpoint), "error": ""}
        except Exception as exc:
            return {"project": proj, "status": "failed", "logstores": [], "error": str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(_stores, listing["projects"]))
    failures = sum(x["status"] == "failed" for x in out)
    return {"projects": out, "account_id": account_id,
            "coverage": {"projects": len(out), "project_failures": failures,
                         "logstores": sum(len(x["logstores"]) for x in out),
                         "complete": listing["complete"] and not failures,
                         "project_listing_error": listing["error"]},
            "truncated": not listing["complete"]}


@router.post("/apply-config")
def apply_config(
    body: ApplyConfigRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """把向导选择的 SLS 目标写回配置：更新 .env（持久）+ 热更新内存 settings（立即生效）。

    同时自动生成 SLS_INSPECTION_TARGETS（巡检日志摘要）——一个 SLB 目标配一条 5xx 摘要，
    应用日志配一条 ERROR 摘要，用户不用再配第二遍。
    """
    import json
    import time

    from app.core.config import settings as st
    from app.models.cloud_account import CloudAccount
    from app.services import audit as audit_svc, onboarding, sls_discovery

    if not body.slb_targets and not body.app_log and not body.cdn_target:
        raise HTTPException(status_code=400, detail="至少配置一类 SLS 目标")
    if body.app_log and len(body.app_log) != 2:
        raise HTTPException(400, "应用日志目标必须同时包含 Project 和 Logstore")
    if body.cdn_target and len(body.cdn_target) != 2:
        raise HTTPException(400, "CDN 日志目标必须同时包含 Project 和 Logstore")

    accounts = (db.query(CloudAccount)
                .filter(CloudAccount.vendor == "aliyun", CloudAccount.enabled == True)  # noqa: E712
                .order_by(CloudAccount.id).all())
    account = None
    if body.account_id:
        account = next((x for x in accounts if x.id == body.account_id), None)
        if account is None:
            raise HTTPException(400, "SLS 目标账号不存在、已停用或不是阿里云账号")
    elif len(accounts) == 1:
        account = accounts[0]  # 只有一个账号时归属无歧义
    elif len(accounts) > 1:
        raise HTTPException(400, "存在多个阿里云账号，必须显式指定 SLS 目标所属账号")
    if account is not None and any(not t.chain for t in body.slb_targets):
        raise HTTPException(400, "字段指纹不能判断 SLB 位于外层还是内层，请逐项确认链路位置")

    slb = [[t.project, t.logstore, t.chain or "outer"] for t in body.slb_targets]
    app_log = list(body.app_log) if body.app_log else ""
    cdn = list(body.cdn_target) if body.cdn_target else ""

    # 账号级新手链路只能确认当前发现结果中可采纳且角色匹配的候选，不能任意绑定。
    confirmations = {}
    candidates = ([(t.project, t.logstore, {"slb"}) for t in body.slb_targets]
                  + ([(app_log[0], app_log[1], {"application_json", "application_text"})] if app_log else [])
                  + ([(cdn[0], cdn[1], {"cdn"})] if cdn else []))
    if account is not None:
        for project, logstore, expected_roles in candidates:
            try:
                confirmations[(project, logstore)] = sls_discovery.confirmation_for_target(
                    db, account, project, logstore, expected_roles)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
    else:
        # 无账号台账的旧高级入口保持原行为；新账号一律走上面的候选与映射校验。
        for project, logstore, expected_roles in candidates:
            role = sorted(expected_roles)[0]
            confirmations[(project, logstore)] = {
                "role": role, "field_mapping": {},
                "semantic_supported": True, "semantic_reason": ""}
    if app_log and account is not None:
        app_log.append(confirmations[(app_log[0], app_log[1])]["field_mapping"])

    # 先验证、后写配置：任一目标失败时 settings、.env 和 Run 均不改变，避免半套“已生效”。
    targets = [(t.project, t.logstore) for t in body.slb_targets]
    if app_log:
        targets.append((app_log[0], app_log[1]))
    if cdn:
        targets.append(tuple(cdn))
    verified = []
    legacy_ak = legacy_sk = ""
    if account is None:
        legacy_ak, legacy_sk = _resolve_ak_sk(db)
    for project, logstore in targets:
        try:
            if account is not None:
                checked = sls_discovery.verify_target(account, project, logstore)
            else:
                end = int(time.time())
                result = sls_discovery.sls.query_logs(
                    legacy_ak, legacy_sk, project, logstore, "*",
                    start=end - 900, end=end, limit=1, reverse=True)
                checked = {"account_id": 0, "project": project, "logstore": logstore,
                           "ok": True, "query_status": "ok",
                           "data_status": "available" if result.get("logs") else "no_recent_data"}
            verified.append(checked)
        except Exception as exc:
            raise HTTPException(502, f"SLS 目标 {project}/{logstore} 真实查询失败，未写入任何配置："
                                     f"{str(exc)[:180]}") from exc

    inspection = []
    for p, l, chain in slb:
        name = "外层SLB" if chain == "outer" else "内层SLB"
        inspection.append([p, l, name, "status >= 500", "5xx状态码"])
    if app_log:
        inspection.append([app_log[0], app_log[1], "应用日志", "LogLevel: ERROR", "ERROR日志"])

    values = {
        "SLS_ACCOUNT_ID": str(account.id if account is not None else 0),
        "SLS_SLB_TARGETS": json.dumps(slb, ensure_ascii=False),
        "SLS_APP_LOG": json.dumps(app_log, ensure_ascii=False) if app_log else "",
        "SLS_CDN_TARGET": json.dumps(cdn, ensure_ascii=False) if cdn else "",
        "SLS_INSPECTION_TARGETS": json.dumps(inspection, ensure_ascii=False),
    }

    # 持久化失败不能显示“已生效”；先原子落盘，成功后再热更新内存。
    from app.services.env_store import set_keys
    previous = {key: getattr(st, key, "") for key in values}
    if not set_keys(values):
        raise HTTPException(500, "SLS 配置持久化失败，未应用任何目标")
    for k, v in values.items():
        setattr(st, k, int(v) if k == "SLS_ACCOUNT_ID" else v)

    # 采纳后的消费必须复用同账号、endpoint 和新字段映射；失败同时回退文件与内存。
    consumed = []
    try:
        for checked in verified:
            project, logstore = checked["project"], checked["logstore"]
            confirmation = confirmations[(project, logstore)]
            if account is not None:
                use = sls_discovery.consume_target(account, project, logstore, confirmation)
            else:
                use = sls_discovery.consume_query(
                    legacy_ak, legacy_sk, project, logstore, confirmation)
            consumed.append({**checked, **use, "data_status": checked["data_status"],
                             "field_mapping": confirmation["field_mapping"]})
    except Exception as exc:
        for key, value in previous.items():
            setattr(st, key, value)
        db.rollback()
        restored = set_keys({key: str(value) for key, value in previous.items()})
        if not restored:
            raise HTTPException(500, "SLS 消费验证失败，且旧配置持久化回退失败；内存已恢复，请检查 .env") from exc
        raise HTTPException(502, f"SLS 采纳后消费验证失败，已恢复旧配置：{str(exc)[:180]}") from exc

    if account is not None:
        onboarding.record_sls_targets(db, account, verified=consumed)
        semantic = all(x["semantic_supported"] for x in consumed)
        onboarding.record_capability(
            db, account, "logs", scope={"source_id": sls_discovery.account_sls_endpoint(account) or "sls"},
            execution="succeeded",
            data_state=("available" if any(x["data_status"] == "available" for x in consumed)
                        else "no_data"),
            coverage={"discovered": len(consumed), "checked": len(consumed), "failed": 0,
                      "total_known": True, "truncated": False},
            checked_at=datetime.now(),
            evidence_refs=[x["evidence_ref"] for x in consumed],
            reason_code="logs_consumption_ready" if semantic else "semantic_unsupported",
            retryable=False, next_action="query_logs" if semantic else "raw_query_only")
    audit_svc.record(
        db, action="bootstrap.apply_config", user_id=user.id, username=user.username,
        resource_type="config", resource_id="SLS",
        detail={"account_id": account.id if account is not None else 0,
                "slb": slb, "app_log": app_log, "cdn": cdn,
                "query_verified": len(consumed), "consumption_verified": len(consumed)},
        client_ip=get_client_ip(request),
    )
    db.commit()
    no_recent = sum(x["data_status"] == "no_recent_data" for x in consumed)
    return {
        "ok": True, "applied": values, "persisted": True, "verified": consumed,
        "message": f"{len(consumed)} 个 SLS 目标采纳后消费查询成功并已持久化"
                   + (f"；其中 {no_recent} 个近 15 分钟无数据" if no_recent else ""),
    }


@router.get("/agent-command")
def agent_command(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """非云机器接入：生成 Agent 安装命令 + 已接入主机统计。"""
    import socket

    from app.api.v1.integrations import _base_url_of
    from app.core.config import settings as st
    from app.models.asset import Asset

    # 服务地址按请求头动态推导，确保域名/443 访问时生成正确命令；
    # PUBLIC_BASE_URL 配置优先，无 Host 头才兜底本机名，不写死 http://{host}:8810。
    base = _base_url_of(request) or f"http://{socket.gethostname()}:8810"
    token = st.AGENT_TOKEN or ""
    n_hosts = db.query(Asset).filter(Asset.product == "host").count()
    return {
        "command": (f"python3 qianshu-agent.py --server {base} --token {token}"
                    if token else "未配置 AGENT_TOKEN——在 .env 配置（密钥类仅存 .env）后重启生成命令"),
        "token_configured": bool(token),
        "hosts_online": n_hosts,
        "hint": "Agent 单文件零依赖、只上报不执行；文件在产品包 agent/qianshu-agent.py",
    }


@router.get("/monitor-sources")
def monitor_sources(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """已有监控接入：数据源注册表探测结果（Prometheus/VM/Loki 等）。"""
    from app.services.datasource import probe_all

    return probe_all(db)


@router.get("/agent-script")
def agent_script(user: User = Depends(get_current_user)):
    """下载 qianshu-agent.py（产品包内单文件 Agent，只读返回文本）。

    向导"生成 Agent 安装命令"旁的下载按钮用：浏览器直接拿脚本文件，
    不用再去服务器上找文件。Content-Disposition attachment = 浏览器落盘保存。
    """
    import os

    from fastapi.responses import Response

    path = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "..", "agent", "qianshu-agent.py"))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"产品包内未找到 agent 脚本：{path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return Response(
        content=text,
        media_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="qianshu-agent.py"'},
    )
