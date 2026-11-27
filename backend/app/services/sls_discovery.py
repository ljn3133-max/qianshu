"""账号级 SLS 完整目录、确定性语义推荐与真实查询验证。

只把字段名、样本数量和时间等派生特征写入共享 Onboarding Run；原始日志仅在
单次函数调用内使用，不落库、不进入审计，也不调用任何模型。所有云调用都显式
使用所属 CloudAccount 的凭证，避免多账号下回落到另一个账号。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app.collector import sls
from app.core.db import SessionLocal
from app.core.security import decrypt_secret
from app.models.cloud_account import CloudAccount
from app.models.onboarding_run import OnboardingRun
from app.services import onboarding

logger = logging.getLogger("qianshu.sls.discovery")

SAMPLE_LIMIT = 5
SAMPLE_WINDOW_S = 24 * 3600
_ACTIVE: set[int] = set()
_ACTIVE_LOCK = threading.Lock()
_ADOPTABLE = {"slb", "cdn", "application_json", "application_text"}


def _name_hint(project: str, logstore: str) -> str:
    name = f"{project}/{logstore}".lower()
    if re.search(r"(^|[-_/])(slb|clb|alb|nlb)([-_/]|$)", name):
        return "slb"
    if "cdn" in name:
        return "cdn"
    if re.search(r"(^|[-_/])(app|application|service|业务|应用)([-_/]|$)", name):
        return "application"
    return ""


def _latest_at(logs: list[dict]) -> int | None:
    values = []
    for row in logs:
        try:
            value = int(row.get("__time__") or 0)
            if value > 0:
                values.append(value)
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


def classify_sample(project: str, logstore: str, logs: list[dict]) -> dict:
    """按字段集合和结构分类；名称只增强已有证据，不能独立决定角色。"""
    field_set = {str(key) for row in logs for key in row}
    fields = sorted(field_set - {"__time__"})
    custom = field_set - {"__time__", "__source__", "__topic__"}
    custom = {x for x in custom if not x.startswith("__tag__:")}
    lower = {x.lower() for x in custom}
    hint = _name_hint(project, logstore)
    latest = _latest_at(logs)
    base = {
        "project": project, "logstore": logstore,
        "field_count": len(custom), "fields": sorted(custom)[:128],
        "fields_truncated": len(custom) > 128,
        "sample_count": len(logs), "latest_at": latest,
        "data_status": "available" if logs else "no_recent_data",
        "query_status": "ok", "error": "", "name_hint": hint,
        "adoption_scope": "primary_only", "adoptable": False,
    }
    if not logs:
        return dict(base, role="unknown", confidence=0.0,
                    evidence=[{"code": "no_recent_data",
                               "detail": "查询成功，近 24 小时无日志，未读取样本"}])

    slb_markers = {"http_host", "host", "upstream_addr", "upstream_status",
                   "request_time", "upstream_response_time", "status", "slbid", "vip_addr"}
    cdn_markers = {"domain", "request_uri", "uri", "http_code", "return_code",
                   "hit", "cache_hit", "hit_info", "response_size", "referer", "user_agent"}
    slb_hits = sorted(lower & slb_markers)
    cdn_hits = sorted(lower & cdn_markers)
    # 真实云样本：WAF 与 Nginx Ingress 也有 upstream_*，不能据此冒充 SLB。
    # 只有带 CLB 实例标识的访问日志才进入现有 SLB 链路消费面；其他角色保持只识别、不可采纳。
    if ({"matched_host", "request_traceid", "real_client_ip"} <= lower
            and "upstream_addr" in lower):
        return dict(base, role="waf", confidence=0.98, adoption_scope="unsupported",
                    evidence=[{"code": "waf_field_fingerprint",
                               "detail": "命中 WAF 专属字段 matched_host、request_traceid、real_client_ip；不是 CLB 访问日志"}])
    if ({"proxy_upstream_name", "_namespace_"} <= lower and "upstream_addr" in lower):
        return dict(base, role="ingress", confidence=0.97, adoption_scope="unsupported",
                    evidence=[{"code": "ingress_field_fingerprint",
                               "detail": "命中 Nginx Ingress 的 Kubernetes 与 proxy_upstream_name 字段；不是 CLB 访问日志"}])
    if ("slbid" in lower and "upstream_addr" in lower
            and len(slb_hits) >= 4 and ({"http_host", "host"} & lower)):
        confidence = 0.96 if hint == "slb" else 0.94
        return dict(base, role="slb", confidence=confidence, adoptable=True,
                    adoption_scope="multiple",
                    evidence=[{"code": "slb_field_fingerprint",
                               "detail": "命中含 CLB 实例标识的访问日志字段：" + "、".join(slb_hits)}])
    if ("domain" in lower and len(cdn_hits) >= 4
            and ({"request_uri", "uri"} & lower)
            and ({"http_code", "return_code"} & lower)
            and ({"hit", "cache_hit", "hit_info"} & lower)):
        confidence = 0.95 if hint == "cdn" else 0.93
        return dict(base, role="cdn", confidence=confidence, adoptable=True,
                    evidence=[{"code": "cdn_field_fingerprint",
                               "detail": "命中 CDN 域名、URI、状态码与缓存字段：" + "、".join(cdn_hits)}])
    # SLS 内部告警中心也有 level/msg；先识别平台系统日志，避免自动选成业务主日志。
    # 同一 Logstore 可能写入异构事件，最近几条不一定包含完整告警字段；保留目录的精确
    # 名称只用于标记不可采纳的系统日志，绝不能用于绑定业务应用目标。
    internal_alert_fields = {"scheduleid", "fingerprint", "drill_down_url"} <= lower
    internal_alert_store = (project.startswith("sls-alert-")
                            and logstore == "internal-alert-center-log" and "alert" in lower)
    if internal_alert_fields or internal_alert_store:
        return dict(base, role="system", confidence=0.99 if internal_alert_store else 0.97,
                    adoption_scope="unsupported",
                    evidence=[{"code": "cloud_internal_alert_store" if internal_alert_store else "cloud_internal_alert_fields",
                               "detail": "命中 SLS 内部告警中心证据，不作为业务应用主日志"}])

    level_keys = lower & {"level", "loglevel", "log_level", "severity"}
    message_keys = lower & {"message", "msg", "content"}
    if level_keys and message_keys and len(custom) >= 3:
        confidence = 0.88 if hint == "application" else 0.86
        return dict(base, role="application_json", confidence=confidence, adoptable=True,
                    evidence=[{"code": "structured_application_fields",
                               "detail": f"结构化日志包含级别字段（{'、'.join(sorted(level_keys))}）、"
                                         f"消息字段（{'、'.join(sorted(message_keys))}）及 {len(custom)} 个字段"}])
    if len(custom) <= 2 and message_keys:
        confidence = 0.62 if hint == "application" else 0.56
        return dict(base, role="application_text", confidence=confidence, adoptable=True,
                    evidence=[{"code": "single_message_field",
                               "detail": "样本只有消息字段，符合非结构化文本日志；建议人工确认"}])
    return dict(base, role="unknown", confidence=0.0,
                evidence=[{"code": "insufficient_fingerprint",
                           "detail": "已有样本字段不足以确定日志用途，保持未识别"}])


def _safe_error(exc: Exception) -> str:
    return str(exc)[:300]


def _sample(ak: str, sk: str, project: str, logstore: str,
            endpoint: str, start: int, end: int) -> dict:
    try:
        result = sls.query_logs(ak, sk, project, logstore, "*", start=start, end=end,
                                limit=SAMPLE_LIMIT, reverse=True, endpoint=endpoint,
                                apply_field_mapping=False)
        # result["logs"] 只在本函数和 classify_sample 的当前调用栈中使用，绝不写入返回结果。
        return classify_sample(project, logstore, result.get("logs") or [])
    except Exception as exc:
        error_code = "index_missing" if "IndexConfigNotExist" in str(exc) else "query_failed"
        detail = ("Logstore 未配置索引，无法查询样本；这不是权限不足或无数据，未自动修改云侧配置"
                  if error_code == "index_missing" else "日志样本查询失败，未参与角色判断")
        return {
            "project": project, "logstore": logstore, "role": "unknown",
            "confidence": 0.0, "adoptable": False, "adoption_scope": "primary_only",
            "query_status": "failed", "data_status": "unknown", "error": _safe_error(exc),
            "error_code": error_code,
            "sample_count": 0, "latest_at": None, "field_count": 0, "fields": [],
            "fields_truncated": False,
            "name_hint": _name_hint(project, logstore),
            "evidence": [{"code": error_code, "detail": detail}],
        }


def _summary(projects: list[dict], logstores: list[dict], project_listing: dict) -> dict:
    project_failures = sum(x["status"] == "failed" for x in projects)
    query_failures = sum(x["query_status"] == "failed" for x in logstores)
    no_recent = sum(x["data_status"] == "no_recent_data" for x in logstores)
    role_counts = {role: sum(x["role"] == role for x in logstores)
                   for role in ("slb", "cdn", "application_json", "application_text",
                                "waf", "ingress", "system", "unknown")}
    complete = bool(project_listing.get("complete")) and not project_failures and not query_failures
    return {
        "projects": len(projects), "project_failures": project_failures,
        "project_listing_complete": bool(project_listing.get("complete")),
        "project_listing_error": project_listing.get("error") or "",
        "logstores": len(logstores), "sampled": sum(x["query_status"] == "ok" for x in logstores),
        "query_failures": query_failures, "no_recent_data": no_recent,
        "role_counts": role_counts,
        "recommended": sum(x["adoptable"] for x in logstores),
        "complete": complete,
    }


def confirmation_plan(logstores: list[dict]) -> dict:
    """把完整目录压缩成集中确认计划；完整目录仍保留在 catalog，不丢失败项。"""
    def targets(role_names: set[str]) -> list[dict]:
        return [{"project": x["project"], "logstore": x["logstore"],
                 "role": x["role"], "confidence": x["confidence"]}
                for x in logstores if x["adoptable"] and x["role"] in role_names]

    slb = targets({"slb"})
    cdn = targets({"cdn"})
    apps = targets({"application_json", "application_text"})
    app_preselected = []
    if (len(apps) == 1 and apps[0]["role"] == "application_json" and apps[0]["confidence"] >= 0.8):
        candidate = next(x for x in logstores
                         if x["project"] == apps[0]["project"] and x["logstore"] == apps[0]["logstore"])
        if application_field_mapping(candidate)["semantic_supported"]:
            app_preselected = apps
    groups = [
        {"role": "slb", "mode": "choose_many", "targets": slb,
         "preselected": [], "chain_confirmation_required": bool(slb)},
        {"role": "cdn", "mode": "choose_one", "targets": cdn,
         "preselected": cdn if len(cdn) == 1 else []},
        {"role": "application", "mode": "choose_one", "targets": apps,
         "preselected": app_preselected},
    ]
    deferred = sum(not x["adoptable"] for x in logstores)
    return {"required": any(x["targets"] for x in groups), "groups": groups,
            "deferred_count": deferred,
            "failed_count": sum(x["query_status"] == "failed" for x in logstores),
            "policy": "unique_supported_preselect_only"}


def application_field_mapping(candidate: dict) -> dict:
    fields = candidate.get("fields") or []
    by_lower: dict[str, list[str]] = {}
    for field in fields:
        by_lower.setdefault(field.lower(), []).append(field)

    def unique(names: set[str]) -> tuple[str, bool]:
        matches = [field for name in names for field in by_lower.get(name, [])]
        return (matches[0], False) if len(matches) == 1 else ("", len(matches) > 1)

    level, level_ambiguous = unique({"level", "loglevel", "log_level", "severity"})
    message, message_ambiguous = unique({"message", "msg", "content"})
    app, app_ambiguous = unique({"appname", "app", "service", "servicename"})
    safe = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
    if any(field and not safe.fullmatch(field) for field in (level, message, app)):
        reason = "unsafe_field_name"
    elif candidate.get("fields_truncated"):
        reason = "field_inventory_truncated"
    elif level_ambiguous or message_ambiguous or app_ambiguous:
        reason = "field_mapping_ambiguous"
    elif not level:
        reason = "level_field_missing"
    else:
        reason = ""
    mapping = {"level_field": level, "message_field": message, "app_field": app}
    return {"role": candidate["role"], "field_mapping": mapping,
            "semantic_supported": bool(level) and not reason,
            "semantic_reason": reason}


def confirmation_for_target(db, account: CloudAccount, project: str, logstore: str,
                            expected_roles: set[str]) -> dict:
    run = onboarding.get_run(db, account.id)
    catalog = get_discovery(run)
    candidate = next((x for x in catalog.get("logstores") or []
                      if x["project"] == project and x["logstore"] == logstore), None)
    if candidate is None or not candidate.get("adoptable") or candidate.get("query_status") != "ok":
        raise ValueError("目标不在该账号已发现且可采纳的候选中，请重新发现后再确认")
    if candidate.get("role") not in expected_roles:
        raise ValueError(f"目标角色 {candidate.get('role') or 'unknown'} 与所选用途不符，未自动绑定")
    if candidate["role"] in ("application_json", "application_text"):
        return application_field_mapping(candidate)
    return {"role": candidate["role"], "field_mapping": {},
            "semantic_supported": True, "semantic_reason": ""}


def account_sls_endpoint(account: CloudAccount) -> str:
    """同一账号的目录、样本和采纳验证必须访问同一 SLS endpoint。"""
    try:
        extra = json.loads(account.extra_json or "{}")
    except (TypeError, ValueError):
        extra = {}
    return str(extra.get("sls_endpoint") or extra.get("endpoint") or "")


def _record_discovery_capability(db, account: CloudAccount, result: dict) -> None:
    coverage = result.get("coverage") or {}
    stores = result.get("logstores") or []
    status = result.get("status") or "failed"
    latest = max((int(x.get("latest_at") or 0) for x in stores), default=0)
    if status == "failed":
        execution, data_state = "failed", "unknown"
        reason, action = "logs_discovery_incomplete", "retry_logs_discovery"
    elif status == "skipped":
        execution, data_state = "skipped", "no_data"
        reason, action = "no_logstores", "continue_onboarding"
    else:
        # 完整目录只是等待集中确认，不能冒充已确认日志的一次分析。
        execution = "waiting"
        data_state = "available" if any(x.get("data_status") == "available" for x in stores) else "no_data"
        reason, action = "confirmation_required", "confirm_logs"
    onboarding.record_capability(
        db, account, "logs", scope={"source_id": account_sls_endpoint(account) or "sls"},
        execution=execution, data_state=data_state,
        coverage={"discovered": coverage.get("logstores", 0),
                  "checked": coverage.get("sampled", 0),
                  "failed": (coverage.get("project_failures", 0)
                             + coverage.get("query_failures", 0)),
                  "total_known": bool(coverage.get("complete")), "truncated": False},
        checked_at=datetime.now(), observed_until=datetime.fromtimestamp(latest) if latest else None,
        evidence_refs=[f"sls_catalog:{account.id}:{int(time.time())}"],
        reason_code=reason, retryable=status == "failed", next_action=action)


def discover(account_id: int, background: bool = True) -> dict | None:
    """启动一次账号级发现；background=False 供新账号后台编排和隔离测试复用。"""
    db = SessionLocal()
    try:
        account = db.get(CloudAccount, account_id)
        if account is None or not account.enabled or account.vendor != "aliyun":
            raise ValueError("只有启用的阿里云账号支持 SLS 发现")
        run = onboarding.ensure_run(db, account)
        with _ACTIVE_LOCK:
            if account_id in _ACTIVE:
                return get_discovery(run)
            _ACTIVE.add(account_id)
        onboarding.record_sls_started(db, account)
        onboarding.record_capability(
            db, account, "logs", scope={"source_id": account_sls_endpoint(account) or "sls"},
            execution="running", data_state="unknown", checked_at=None,
            reason_code="logs_discovery_running", retryable=False, next_action="wait")
        db.commit()
    finally:
        db.close()

    if background:
        thread = threading.Thread(target=_run_claimed, args=(account_id,), daemon=True,
                                  name=f"sls-discovery-{account_id}")
        thread.start()
        db = SessionLocal()
        try:
            return get_discovery(onboarding.get_run(db, account_id))
        finally:
            db.close()
    return _run_claimed(account_id)


def _run_claimed(account_id: int) -> dict:
    db = SessionLocal()
    try:
        account = db.get(CloudAccount, account_id)
        if account is None:
            raise ValueError("云账号不存在")
        ak = decrypt_secret(account.ak_cipher)
        sk = decrypt_secret(account.secret_cipher)
        endpoint = account_sls_endpoint(account)
        end = int(time.time())
        listing = sls.list_projects_detailed(ak, sk, endpoint=endpoint)
        project_rows: list[dict] = []
        stores: list[tuple[str, str]] = []
        for project in listing["projects"]:
            try:
                names = sls.list_logstores(ak, sk, project, endpoint=endpoint)
                project_rows.append({"project": project, "status": "ready",
                                     "logstore_count": len(names), "error": ""})
                stores.extend((project, name) for name in names)
            except Exception as exc:
                project_rows.append({"project": project, "status": "failed",
                                     "logstore_count": 0, "error": _safe_error(exc)})
        # SDK 的 list_logstores 不接收分页参数，一次返回该项目完整清单；这里不截断。
        with ThreadPoolExecutor(max_workers=8) as pool:
            logstores = list(pool.map(
                lambda item: _sample(ak, sk, item[0], item[1], endpoint, end - SAMPLE_WINDOW_S, end),
                stores))
        logstores.sort(key=lambda x: (x["project"], x["logstore"]))
        coverage = _summary(project_rows, logstores, listing)
        status = ("failed" if not coverage["complete"] else
                  ("skipped" if not logstores else "ready"))
        result = {
            "account_id": account_id, "status": status,
            "sample_window_seconds": SAMPLE_WINDOW_S, "sample_limit": SAMPLE_LIMIT,
            "privacy": "derived_fields_only",
            "coverage": coverage, "projects": project_rows, "logstores": logstores,
            "confirmation": confirmation_plan(logstores),
        }
        onboarding.record_sls_discovery(db, account, result)
        _record_discovery_capability(db, account, result)
        db.commit()
        return result
    except Exception as exc:
        logger.exception("账号 %s SLS 发现失败", account_id)
        db.rollback()
        account = db.get(CloudAccount, account_id)
        if account is not None:
            result = {"account_id": account_id, "status": "failed",
                      "coverage": {"projects": 0, "logstores": 0, "complete": False},
                      "projects": [], "logstores": [], "error": _safe_error(exc),
                      "privacy": "derived_fields_only"}
            onboarding.record_sls_discovery(db, account, result)
            _record_discovery_capability(db, account, result)
            db.commit()
            return result
        raise
    finally:
        db.close()
        with _ACTIVE_LOCK:
            _ACTIVE.discard(account_id)


def reap_interrupted() -> int:
    """启动时收割进程中断的发现项；只修正本地状态，不自动发起云请求。"""
    db = SessionLocal()
    try:
        recovered = 0
        for run in db.query(OnboardingRun).all():
            item = next((x for x in onboarding._items(run)
                         if x.get("key") == "sls_discovery"), {})
            if item.get("status") != "running":
                continue
            account = db.get(CloudAccount, run.account_id)
            if account is None:
                continue
            onboarding.record_sls_discovery(db, account, {
                "account_id": account.id, "status": "failed",
                "coverage": {"projects": 0, "logstores": 0, "complete": False},
                "projects": [], "logstores": [],
                "error": "本机服务重启，SLS 发现被中断；请重新发现",
                "privacy": "derived_fields_only",
            })
            _record_discovery_capability(db, account, {"status": "failed", "coverage": {}, "logstores": []})
            recovered += 1
        if recovered:
            db.commit()
        return recovered
    finally:
        db.close()


def get_discovery(run) -> dict:
    if run is None:
        return {"status": "pending", "coverage": {}, "projects": [], "logstores": []}
    for item in onboarding._items(run):
        if item.get("key") == "sls_discovery":
            catalog = item.get("catalog") or {}
            return {**catalog, "status": item.get("status") or catalog.get("status") or "pending",
                    "detail": item.get("detail") or "",
                    "updated_at": run.updated_at.isoformat(timespec="seconds") if run.updated_at else None}
    return {"status": "pending", "coverage": {}, "projects": [], "logstores": []}


def verify_target(account: CloudAccount, project: str, logstore: str) -> dict:
    """采纳前用同一账号执行真实只读查询；无近期数据与查询失败分别返回。"""
    if account.vendor != "aliyun" or not account.enabled:
        raise ValueError("目标必须属于启用的阿里云账号")
    ak = decrypt_secret(account.ak_cipher)
    sk = decrypt_secret(account.secret_cipher)
    end = int(time.time())
    result = sls.query_logs(ak, sk, project, logstore, "*",
                            start=end - 900, end=end, limit=1, reverse=True,
                            endpoint=account_sls_endpoint(account), apply_field_mapping=False)
    return {"account_id": account.id, "project": project, "logstore": logstore,
            "ok": True, "query_status": "ok",
            "data_status": "available" if result.get("logs") else "no_recent_data"}


def consume_query(ak: str, sk: str, project: str, logstore: str,
                  confirmation: dict, *, endpoint: str = "") -> dict:
    """配置生效后走真实消费查询；只返回计数与时间证据，不返回或落库原始日志。"""
    role = confirmation.get("role") or ""
    semantic = bool(confirmation.get("semantic_supported", True))
    if role in ("application_json", "application_text") and semantic:
        query = "LogLevel: ERROR | select count(*) as cnt"
    elif role == "slb":
        query = "status >= 500 | select count(*) as cnt"
    else:
        query = "*"
    end = int(time.time())
    result = sls.query_logs(ak, sk, project, logstore, query,
                            start=end - 900, end=end, limit=1, reverse=True,
                            endpoint=endpoint)
    matched = 0
    if result.get("logs") and "|" in query:
        try:
            matched = int((result["logs"][0] or {}).get("cnt") or 0)
        except (TypeError, ValueError):
            matched = 0
    elif result.get("logs"):
        matched = len(result["logs"])
    return {"project": project, "logstore": logstore, "ok": True,
            "query_status": "ok", "consumption_verified": True,
            "semantic_supported": semantic, "matched_count": matched,
            "evidence_ref": f"sls_consumption:{project}/{logstore}:{end - 900}:{end}"}


def consume_target(account: CloudAccount, project: str, logstore: str,
                   confirmation: dict) -> dict:
    ak = decrypt_secret(account.ak_cipher)
    sk = decrypt_secret(account.secret_cipher)
    return {"account_id": account.id,
            **consume_query(ak, sk, project, logstore, confirmation,
                            endpoint=account_sls_endpoint(account))}
