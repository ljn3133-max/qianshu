"""云账号 Onboarding Run：跨向导、智能体与后台任务的唯一接入状态。

只记录本地任务与只读采集证据，不执行云调用。云调用仍由 sync_job/Provider 负责；
本模块把其产品级结果转成可恢复、可审计的就绪状态，避免账号存在即成功。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.cloud_account import CloudAccount
from app.models.onboarding_run import OnboardingRun
from app.schemas.onboarding import CapabilityResult, CapabilityScope, RetryScope, ScopeSelector

_CAPABILITY_STORE = "_capabilities"
_CAPABILITY_NAMES = ("assets", "metrics", "logs", "cost", "changes")
_STAGE_BY_ACCOUNT_STATUS = {
    "untested": "credential",
    "probing": "credential",
    "syncing": "assets",
    "seeding": "alert_rules",
    "ok": "complete",
    "error": "complete",
    "unsupported": "complete",
}
_TERMINAL = {"ready", "attention", "failed"}


def _items(run: OnboardingRun) -> list[dict]:
    try:
        value = json.loads(run.items_json or "[]")
    except (TypeError, ValueError):
        value = []
    return value if isinstance(value, list) else []


def _lock_run(db: Session, account: CloudAccount) -> OnboardingRun:
    """锁定本账号 Run 行并刷新为最新提交状态。

    证据存在 items_json 整列；成本/指标/变更/日志由独立线程并发回写。
    不取行锁时是“读旧整列 -> 改一项 -> 覆盖整列”，先提交的能力证据会被后写者抹掉
    （表现为资产已同步、其它能力却一直“等待执行”）。只锁单账号、不跨账号取锁，
    调用方应在执行云调用前尽早 commit 释放，避免同账号能力互相阻塞。
    """
    run = ensure_run(db, account)
    # SessionLocal 关闭了 autoflush；populate_existing 会丢弃未 flush 的本事务改动，
    # 必须先 flush（写入仍未提交，且顺序与行锁一致），否则同事务前面写的
    # items_json（资产条目/能力证据）会被刷新回旧值。
    db.flush()
    locked = (db.query(OnboardingRun)
              .filter(OnboardingRun.id == run.id)
              .populate_existing()
              .with_for_update()
              .first())
    return locked if locked is not None else run


def _upsert(run: OnboardingRun, key: str, **values) -> None:
    items = _items(run)
    for item in items:
        if item.get("key") == key:
            item.update(values)
            break
    else:
        items.append({"key": key, **values})
    run.items_json = json.dumps(items, ensure_ascii=False)
    run.updated_at = datetime.now()


def _capability_scope(account: CloudAccount, scope: dict | None = None) -> dict:
    selector = ScopeSelector.model_validate(scope or {}).model_dump()
    return CapabilityScope(
        account_id=account.id, vendor=account.vendor, **selector,
    ).model_dump()


def _capability_identity(capability: str, scope: dict) -> tuple:
    return (capability, scope.get("region", ""), scope.get("source_id", ""),
            tuple(scope.get("resource_ids") or []), scope.get("bill_date", ""))


def _default_capability(run: OnboardingRun, account: CloudAccount | None,
                        capability: str, scope: dict | None = None) -> dict:
    account_id = account.id if account is not None else run.account_id
    vendor = account.vendor if account is not None else ""
    selector = ScopeSelector.model_validate(scope or {}).model_dump()
    return CapabilityResult(
        capability=capability,
        scope={"account_id": account_id, "vendor": vendor, **selector},
    ).model_dump(mode="json")


def _stored_capabilities(run: OnboardingRun) -> list[dict]:
    store = next((x for x in _items(run) if x.get("key") == _CAPABILITY_STORE), {})
    raw = store.get("results") or []
    if not isinstance(raw, list):
        return []
    results = []
    for value in raw:
        try:
            result = CapabilityResult.model_validate(value).model_dump(mode="json")
        except (TypeError, ValueError):
            # 损坏或旧格式证据保持未验证，绝不能因字段存在就补为成功。
            continue
        if result["scope"]["account_id"] != run.account_id:
            continue
        results.append(result)
    return results


def _write_capabilities(run: OnboardingRun, results: list[dict]) -> None:
    _upsert(run, _CAPABILITY_STORE, kind="capability_store", results=results)


def capability_results(run: OnboardingRun, account: CloudAccount | None = None) -> list[dict]:
    stored = _stored_capabilities(run)
    grouped = {name: [] for name in _CAPABILITY_NAMES}
    for result in stored:
        grouped[result["capability"]].append(result)
    results = []
    for name in _CAPABILITY_NAMES:
        values = grouped[name] or [_default_capability(run, account, name)]
        results.extend(sorted(values, key=lambda x: _capability_identity(name, x["scope"])))
    return results


def record_capability(db: Session, account: CloudAccount, capability: str, *,
                      scope: dict | None = None, expected_revision: int | None = None,
                      **values) -> dict | None:
    """写入一项能力证据；expected_revision 不匹配时丢弃晚到执行结果。"""
    if capability not in _CAPABILITY_NAMES:
        raise ValueError(f"未知接入能力: {capability}")
    run = _lock_run(db, account)
    scope_value = _capability_scope(account, scope)
    identity = _capability_identity(capability, scope_value)
    results = _stored_capabilities(run)
    index = next((i for i, item in enumerate(results)
                  if _capability_identity(item["capability"], item["scope"]) == identity), None)
    current = results[index] if index is not None else None
    if expected_revision is not None:
        if current is None and expected_revision != 0:
            return None
        if current is not None and current["revision"] != expected_revision:
            return None
    base = current or _default_capability(run, account, capability, scope)
    base["scope"] = scope_value
    if "coverage" in values:
        base["coverage"] = {**(base.get("coverage") or {}), **(values.pop("coverage") or {})}
    base.update(values)
    result = CapabilityResult.model_validate(base).model_dump(mode="json")
    if index is None:
        results.append(result)
    else:
        results[index] = result
    _write_capabilities(run, results)
    return result


def prepare_retries(db: Session, account: CloudAccount, scopes: list[dict],
                    *, automatic: bool = False) -> dict:
    """只启动失败/未完成范围；运行中复用，成功项保留，不重置整个 Run。"""
    run = _lock_run(db, account)
    current = capability_results(run, account)
    started, reused, unchanged = [], [], []
    for raw in scopes:
        selector = RetryScope.model_validate(raw)
        scope = selector.model_dump(exclude={"capability"})
        identity = _capability_identity(selector.capability, _capability_scope(account, scope))
        existing = next((item for item in current
                         if _capability_identity(item["capability"], item["scope"]) == identity), None)
        if existing is None:
            existing = _default_capability(run, account, selector.capability, scope)
        if existing["execution"] == "running":
            reused.append(existing)
            continue
        if automatic and existing["execution"] == "failed" and not existing["retryable"]:
            unchanged.append(existing)
            continue
        if existing["execution"] in ("succeeded", "skipped") and existing["data_state"] in ("available", "no_data"):
            unchanged.append(existing)
            continue
        revision = existing["revision"] + 1
        result = record_capability(
            db, account, selector.capability, scope=scope,
            expected_revision=existing["revision"], revision=revision,
            execution="running", data_state=existing["data_state"],
            checked_at=None, reason_code="retry_running", retryable=False, next_action="")
        if result is None:
            current_run = db.get(OnboardingRun, run.id)
            reused.extend(x for x in capability_results(current_run, account)
                          if _capability_identity(x["capability"], x["scope"]) == identity)
            continue
        started.append({"capability": selector.capability, "scope": scope, "revision": revision})
    if started:
        run.status = "running"
        run.stage = started[0]["capability"]
        run.finished_at = None
        run.updated_at = datetime.now()
    return {"started": started, "reused": reused, "unchanged": unchanged}


def ensure_run(db: Session, account: CloudAccount) -> OnboardingRun:
    run = (db.query(OnboardingRun)
           .filter(OnboardingRun.account_id == account.id).first())
    if run is None:
        run = OnboardingRun(account_id=account.id, status="running",
                            stage=_STAGE_BY_ACCOUNT_STATUS.get(account.status, "credential"))
        db.add(run)
        db.flush()
        _derive_terminal(run, account.status, account.vendor)
    return run


def reset_run(db: Session, account: CloudAccount) -> OnboardingRun:
    """重新接入复用原 Run；保留历史尝试次数，清除上一次结果，不并发另建任务。"""
    run = _lock_run(db, account)
    run.status = "running"
    run.stage = "credential"
    run.items_json = "[]"
    run.attempts = (run.attempts or 0) + 1
    run.finished_at = None
    run.updated_at = datetime.now()
    return run


def _derive_terminal(run: OnboardingRun, account_status: str, vendor: str = "") -> None:
    items = _items(run)
    credential = next((x for x in items if x.get("key") == "credential"), {})
    if account_status == "unsupported":
        run.status = "attention"
    elif account_status == "error":
        run.status = "failed" if credential.get("status") == "failed" else "attention"
    elif account_status == "ok":
        assets = [x for x in items if x.get("kind") == "assets"]
        rules = next((x for x in items if x.get("key") == "alert_rules"), {})
        discovery = next((x for x in items if x.get("key") == "sls_discovery"), {})
        targets = next((x for x in items if x.get("key") == "sls_targets"), {})
        complete = (
            credential.get("status") == "ready"
            and bool(assets)
            and all(x.get("status") in ("ready", "skipped") for x in assets)
            and rules.get("status") in ("ready", "skipped")
        )
        if vendor == "aliyun":
            # 目录完整不等于日志已配置；有 Logstore 时必须经过集中确认或明确跳过。
            complete = complete and discovery.get("status") in ("ready", "skipped")
            has_logstores = bool((discovery.get("catalog") or {}).get("coverage", {}).get("logstores"))
            if has_logstores:
                complete = complete and targets.get("status") in ("ready", "skipped")
        # 手动探活成功只证明凭证可用；缺资产/规则证据时仍是 attention，不能冒充接入完成。
        run.status = "ready" if complete else "attention"
    elif account_status == "untested":
        run.status = "waiting"
    else:
        run.status = "running"
    run.finished_at = datetime.now() if run.status in _TERMINAL else None


def set_account_stage(db: Session, account: CloudAccount, status: str) -> None:
    """复用 cloud_accounts 的现有生命周期，派生共享 Run，不新增第二套阶段字段。"""
    run = _lock_run(db, account)
    run.stage = _STAGE_BY_ACCOUNT_STATUS.get(status, "credential")
    run.updated_at = datetime.now()
    _derive_terminal(run, status, account.vendor)


def record_probe(db: Session, account: CloudAccount, *, ok: bool,
                 message: str = "", supported: bool = True) -> None:
    run = _lock_run(db, account)
    _upsert(run, "credential", kind="credential", label="凭证与连通性",
            status="ready" if ok else ("skipped" if not supported else "failed"),
            detail=str(message or "")[:300])


def record_sync_results(db: Session, account: CloudAccount, rows: list[dict]) -> None:
    """逐产品保留结果；区域部分失败、异常和静默空结果都不能汇总为账号成功。"""
    run = _lock_run(db, account)
    own = [r for r in rows if r.get("account_id") == account.id]
    if not own:
        _upsert(run, "asset:*", kind="assets", label="资产同步", status="failed",
                detail="本轮未返回任何产品结果，尚未验证资产同步完成")
        return
    # 有真实产品结果后移除上一轮的“空结果”占位失败，避免恢复后仍被旧证据拖成 attention。
    items = [x for x in _items(run) if x.get("key") != "asset:*"]
    run.items_json = json.dumps(items, ensure_ascii=False)
    for row in own:
        product = str(row.get("product") or "*")
        failed_regions = row.get("failed_regions") or []
        failed = row.get("ok") is False or bool(failed_regions)
        skipped = bool(row.get("skipped")) and not failed
        detail = row.get("error") or row.get("note") or ""
        if failed_regions and not detail:
            detail = "；".join(str(x) for x in failed_regions)
        _upsert(run, f"asset:{product}", kind="assets",
                label=f"资产同步 · {product}",
                status="failed" if failed else ("skipped" if skipped else "ready"),
                detail=str(detail)[:300], total=int(row.get("total") or 0),
                created=int(row.get("created") or 0), updated=int(row.get("updated") or 0),
                failed_regions=[str(x)[:200] for x in failed_regions])


def record_seed_result(db: Session, account: CloudAccount, *, error: str = "") -> None:
    run = _lock_run(db, account)
    if error:
        status, detail = "failed", str(error)[:300]
    elif account.alert_seeded_at:
        status, detail = "ready", "默认规则已播种或此前已存在"
    else:
        status, detail = "skipped", "当前没有适用资产，未播种；后续同步可补播"
    _upsert(run, "alert_rules", kind="alert_rules", label="默认告警规则",
            status=status, detail=detail)
    # 存量账号手动同步/定时补播种不会经过 bootstrap 的最后 _set_status，这里也必须重派生。
    _derive_terminal(run, account.status, account.vendor)


def record_runtime_error(db: Session, account: CloudAccount, message: str) -> None:
    """调度/锁等待失败不是凭证无效；单独留证据，避免任务永久停留在 running。"""
    run = _lock_run(db, account)
    _upsert(run, "runtime", kind="runtime", label="接入任务调度",
            status="failed", detail=str(message)[:300])
    _derive_terminal(run, account.status, account.vendor)


def record_runtime_waiting(db: Session, account: CloudAccount, message: str) -> None:
    """同账号已有任务在跑：排队是正常等待，不能写成失败，也不能把账号置 error。"""
    run = _lock_run(db, account)
    _upsert(run, "runtime", kind="runtime", label="接入任务调度",
            status="pending", detail=str(message)[:300])
    run.status = "running"
    run.finished_at = None
    run.updated_at = datetime.now()


def record_sls_started(db: Session, account: CloudAccount) -> None:
    run = _lock_run(db, account)
    _upsert(run, "sls_discovery", kind="sls_discovery", label="SLS 完整发现与语义推荐",
            status="running", detail="正在枚举完整目录并提取字段指纹")
    run.stage = "logs"
    run.status = "running"
    run.finished_at = None
    run.updated_at = datetime.now()


def record_sls_discovery(db: Session, account: CloudAccount, result: dict) -> None:
    run = _lock_run(db, account)
    coverage = result.get("coverage") or {}
    status = result.get("status") or "failed"
    detail = result.get("error") or coverage.get("project_listing_error") or ""
    if status == "failed":
        if not detail:
            detail = (f"目录或样本查询存在失败：Project {coverage.get('project_failures', 0)} 个，"
                      f"Logstore {coverage.get('query_failures', 0)} 个")
    elif status == "skipped":
        detail = "该账号没有 Project 或 Logstore，无需配置 SLS 目标"
    else:
        detail = (f"已完整检查 {coverage.get('logstores', 0)} 个 Logstore；"
                  f"无近期数据 {coverage.get('no_recent_data', 0)} 个")
    catalog = {key: value for key, value in result.items() if key != "status"}
    _upsert(run, "sls_discovery", kind="sls_discovery", label="SLS 完整发现与语义推荐",
            status=status, detail=str(detail)[:300], catalog=catalog)
    if coverage.get("logstores"):
        old = next((x for x in _items(run) if x.get("key") == "sls_targets"), {})
        _upsert(run, "sls_targets", kind="sls_targets", label="SLS 目标确认与查询验证",
                status=old.get("status") if old.get("status") in ("ready", "skipped") else "pending",
                detail=old.get("detail") or "请审阅推荐并验证启用，或明确暂不配置")
    elif status == "skipped":
        _upsert(run, "sls_targets", kind="sls_targets", label="SLS 目标确认与查询验证",
                status="skipped", detail="没有可配置的 Logstore")
    else:
        # 枚举失败不代表空目录：目标是否可配置仍未知，不能写成用户已明确跳过。
        _upsert(run, "sls_targets", kind="sls_targets", label="SLS 目标确认与查询验证",
                status="pending", detail="SLS 发现未完成，尚不能判断是否有可配置日志；请重新发现")
    run.stage = "complete"
    _derive_terminal(run, account.status, account.vendor)


def record_sls_targets(db: Session, account: CloudAccount, *, verified: list[dict] | None = None,
                       skipped: bool = False) -> None:
    run = _lock_run(db, account)
    checked = verified or []
    no_recent = sum(x.get("data_status") == "no_recent_data" for x in checked)
    _upsert(run, "sls_targets", kind="sls_targets", label="SLS 目标确认与查询验证",
            status="skipped" if skipped else "ready",
            detail=("用户明确暂不配置 SLS 目标" if skipped else
                    f"{len(checked)} 个目标真实查询成功；其中 {no_recent} 个近 15 分钟无数据"),
            verified=checked)
    _derive_terminal(run, account.status, account.vendor)


def get_run(db: Session, account_id: int) -> OnboardingRun | None:
    return (db.query(OnboardingRun)
            .filter(OnboardingRun.account_id == account_id).first())


def serialize(run: OnboardingRun, account: CloudAccount | None = None) -> dict:
    items = [x for x in _items(run) if x.get("key") != _CAPABILITY_STORE]
    discovery = next((x for x in items if x.get("key") == "sls_discovery"), {})
    has_logstores = bool((discovery.get("catalog") or {}).get("coverage", {}).get("logstores"))
    # 升级前账号可能只有定时同步/补播种证据。终态下把缺失的必需证据显式展示，
    # 否则“需要处理”没有问题项，用户无法知道还差凭证、资产、规则还是日志确认。
    if run.status in ("attention", "failed"):
        existing = {x.get("key") for x in items}
        required = [
            ("credential", "credential", "凭证与连通性", "尚无凭证验证结果，请执行连通测试或重新接入"),
            ("asset:*", "assets", "资产同步", "尚无资产同步结果，不能判定接入就绪"),
            ("alert_rules", "alert_rules", "默认告警规则", "尚无默认规则处理结果"),
        ]
        if account is not None and account.vendor == "aliyun":
            required.append(("sls_discovery", "sls_discovery", "SLS 完整发现与语义推荐",
                             "尚未执行完整 SLS 发现，不能判定日志数据就绪"))
            if has_logstores:
                required.append(("sls_targets", "sls_targets", "SLS 目标确认与查询验证",
                                 "请审阅日志推荐并验证启用，或明确暂不配置"))
        for key, kind, label, detail in required:
            present = (any(x.get("kind") == "assets" for x in items)
                       if kind == "assets" else key in existing)
            if not present:
                items.append({"key": key, "kind": kind, "label": label,
                              "status": "pending", "detail": detail})
    counts = {key: sum(1 for x in items if x.get("status") == key)
              for key in ("ready", "running", "pending", "failed", "skipped")}
    # 大目录只走账号级专用接口；Run 列表和详情只带轻量覆盖摘要，避免每次轮询传输数万字段。
    items = [{key: value for key, value in item.items() if key != "catalog"} for item in items]
    out = {
        "id": run.id, "account_id": run.account_id, "status": run.status,
        "stage": run.stage, "attempts": run.attempts, "items": items,
        "counts": counts, "capabilities": capability_results(run, account),
        "issues": [x for x in items if x.get("status") == "failed"
                   or (run.status == "attention" and x.get("status") == "pending")],
        "created_at": run.created_at.isoformat(timespec="seconds") if run.created_at else None,
        "updated_at": run.updated_at.isoformat(timespec="seconds") if run.updated_at else None,
        "finished_at": run.finished_at.isoformat(timespec="seconds") if run.finished_at else None,
    }
    if account is not None:
        out["account"] = {"id": account.id, "name": account.name,
                          "vendor": account.vendor, "status": account.status,
                          "enabled": account.enabled}
    return out
