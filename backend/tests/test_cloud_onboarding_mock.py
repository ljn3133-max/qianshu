#!/usr/bin/env python3
"""云账号接入状态机 mock 回归（不访问任何真实云 API）。

锁定 probing → syncing → seeding → ok/error：探活成功不是接入成功，
部分产品/区域失败不推进成功游标，播种失败也不能显示正常。
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-onboarding-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("onboarding")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (pathlib.Path(__file__).resolve().parent.parent / "app" / "models").glob("*.py"):
    if _p.stem != "__init__":
        importlib.import_module(f"app.models.{_p.stem}")

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.collector import base as collector_base  # noqa: E402
from app.collector import es as es_collector  # noqa: E402
from app.services import onboarding  # noqa: E402
from app.services.cloud import sync_job  # noqa: E402
from app.api.v1 import clouds as cloud_api  # noqa: E402
from app.api.v1 import bootstrap as bootstrap_api  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  <-- {detail}"))


class FakeProvider:
    def __init__(self, *, probe_ok=True, rows=None, stages=None):
        self.probe_ok = probe_ok
        self.rows = [{"product": "cvm", "ok": True, "total": 0}] if rows is None else rows
        self.stages = stages if stages is not None else []

    def test_account(self, acc, ak, sk):
        self.stages.append(acc.status)
        return {"ok": self.probe_ok, "message": "mock"}

    def sync(self, db, acc, products=None):
        self.stages.append(acc.status)
        return [dict(r) for r in self.rows]


def account(db, name, vendor="tencent", status="probing"):
    row = CloudAccount(name=name, vendor=vendor, enabled=True, status=status)
    db.add(row)
    db.commit()
    return row


def run_bootstrap(db, name, provider, seed=None):
    row = account(db, name)
    calls = []

    def fake_seed(session, acc):
        calls.append(acc.status)
        if seed:
            return seed(session, acc)
        return []

    with patch.object(sync_job, "get_provider", return_value=provider), \
            patch.object(sync_job, "seed_rules_for_account", side_effect=fake_seed):
        sync_job.bootstrap_account(row.id)
    db.expire_all()
    return db.get(CloudAccount, row.id), calls


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        old = datetime(2020, 1, 1)
        cases = [
            ("完整成功", [{"product": "cvm", "ok": True, "total": 0}], True),
            ("产品失败", [{"product": "cvm", "ok": False, "error": "mock"}], False),
            ("区域部分失败", [{"product": "cvm", "total": 1, "failed_regions": ["ap-test: mock"]}], False),
            ("地域暂不可达但其余成功", [{"product": "es", "total": 1,
                                      "unavailable_regions": ["cn-test: mock"],
                                      "note": "1 个地域暂时不可达"}], True),
            ("静默无结果", [], False),
        ]
        for idx, (label, rows, success) in enumerate(cases):
            acc = account(db, f"sync-{idx}")
            acc.last_sync_at = old
            db.commit()
            with patch.object(sync_job, "get_provider", return_value=FakeProvider(rows=rows)):
                out = sync_job.sync_accounts(db, account_ids=[acc.id], force=True)
            db.refresh(acc)
            check(f"{label}：failed_accounts 口径",
                  (acc.id not in out["summary"]["failed_accounts"]) is success,
                  out["summary"])
            check(f"{label}：成功游标只在完整成功时推进",
                  (acc.last_sync_at != old) is success, acc.last_sync_at)

        # ES 地域级连接失败：重试一次后按“地域不可达”跳过，不应让整个产品失败；
        # 但清扫必须跳过，避免把该地域存量资产误标 Deleted。
        calls = []

        class FakeEsClient:
            def __init__(self, region_id):
                self.region_id = region_id

            def list_instance(self, request):
                calls.append(self.region_id)
                if self.region_id == "cn-bad":
                    raise RuntimeError(
                        "('Connection aborted.', RemoteDisconnected("
                        "'Remote end closed connection without response'))")
                inst = SimpleNamespace(to_map=lambda: {
                    "instanceId": "es-mock-1", "description": "mock es",
                    "esVersion": "7.10", "nodeAmount": 1, "nodeSpec": {},
                    "port": 9200, "paymentType": "PostPaid", "status": "active",
                    "zoneId": "cn-ok-a", "endTime": None,
                })
                return SimpleNamespace(body=SimpleNamespace(result=[inst]))

        with patch.object(es_collector, "list_regions", return_value=["cn-ok", "cn-bad"]), \
                patch.object(es_collector, "_dns_ok", return_value=True), \
                patch.object(es_collector, "_client",
                             side_effect=lambda ak, sk, r: FakeEsClient(r)), \
                patch.object(es_collector, "resolve_aliyun_credential",
                             return_value=("ak", "sk", "mock")), \
                patch.object(es_collector.time, "sleep"), \
                patch.object(es_collector, "mark_missing_deleted", return_value=0) as sweep:
            es_out = es_collector.sync_es(db)
        check("ES 单地域断连不判产品失败",
              es_out["failed_regions"] == [] and len(es_out["unavailable_regions"]) == 1,
              es_out)
        check("ES 断连地域重试一次", calls.count("cn-bad") == 2, calls)
        check("ES 存在不可达地域时跳过缺失清扫",
              es_out["sweep_skipped"] is True and not sweep.called, es_out)

        with patch.object(es_collector, "list_regions", return_value=["cn-bad"]), \
                patch.object(es_collector, "_dns_ok", return_value=True), \
                patch.object(es_collector, "_client",
                             side_effect=lambda ak, sk, r: FakeEsClient(r)), \
                patch.object(es_collector, "resolve_aliyun_credential",
                             return_value=("ak", "sk", "mock")), \
                patch.object(es_collector.time, "sleep"), \
                patch.object(es_collector, "mark_missing_deleted", return_value=0) as sweep2:
            es_all_bad = es_collector.sync_es(db)
        check("ES 全部地域断连仍判产品失败",
              bool(es_all_bad["failed_regions"])
              and not es_all_bad["unavailable_regions"]
              and not sweep2.called, es_all_bad)

        # 客户现场的真实形态：错误在“拉地域清单”这一步抛出（RetryError 的
        # str 正好是 {'message': "('Connection aborted.', RemoteDisconnected(...))"}），
        # 还没进入 region 循环。必须降级为本轮跳过，不能判产品失败、不能清扫。
        with patch.object(es_collector, "list_regions",
                          side_effect=RuntimeError(
                              "{'message': \"('Connection aborted.', "
                              "RemoteDisconnected('Remote end closed connection "
                              "without response'))\"}")), \
                patch.object(es_collector, "resolve_aliyun_credential",
                             return_value=("ak", "sk", "mock")), \
                patch.object(es_collector, "mark_missing_deleted", return_value=0) as sweep3:
            es_no_regions = es_collector.sync_es(db)
        check("ES 地域清单断连不判产品失败且标记本轮跳过",
              es_no_regions["failed_regions"] == []
              and es_no_regions["unavailable_regions"] == ["region-list"]
              and es_no_regions["skipped"] is True,
              es_no_regions)
        check("ES 地域清单断连跳过清扫并给说明",
              es_no_regions["sweep_skipped"] is True
              and "不可达" in es_no_regions["note"]
              and not sweep3.called, es_no_regions)

        # 共用的 region 列表：连接级错误重试一次，权限类错误不重试。
        class FakeRegionsClient:
            calls = 0

            def __init__(self, config):
                pass

            def describe_regions(self, request):
                FakeRegionsClient.calls += 1
                if FakeRegionsClient.calls == 1:
                    raise RuntimeError("('Connection aborted.', RemoteDisconnected())")
                region = SimpleNamespace(region_id="cn-mock")
                return SimpleNamespace(
                    body=SimpleNamespace(regions=SimpleNamespace(region=[region])))

        with patch("alibabacloud_ecs20140526.client.Client", FakeRegionsClient), \
                patch.object(collector_base.time, "sleep"):
            regions = collector_base.list_regions("ak", "sk")
        check("region 清单连接级错误重试后成功",
              regions == ["cn-mock"] and FakeRegionsClient.calls == 2,
              (regions, FakeRegionsClient.calls))

        FakeRegionsClient.calls = 0

        class ForbiddenRegionsClient(FakeRegionsClient):
            def describe_regions(self, request):
                FakeRegionsClient.calls += 1
                raise RuntimeError("Forbidden.RAM")

        with patch("alibabacloud_ecs20140526.client.Client", ForbiddenRegionsClient), \
                patch.object(collector_base.time, "sleep"):
            try:
                collector_base.list_regions("ak", "sk")
            except RuntimeError as exc:
                forbidden_raised = "Forbidden" in str(exc)
            else:
                forbidden_raised = False
        check("region 清单权限错误不重试",
              forbidden_raised and FakeRegionsClient.calls == 1,
              FakeRegionsClient.calls)

        stages = []
        ok_provider = FakeProvider(stages=stages)
        row, seed_stages = run_bootstrap(db, "bootstrap-ok", ok_provider)
        check("成功路径按 probing/syncing/seeding 推进",
              stages == ["probing", "syncing"] and seed_stages == ["seeding"],
              (stages, seed_stages))
        check("成功路径最终 ok", row.status == "ok", row.status)
        check("成功路径有同步成功时间", row.last_sync_at is not None)

        def seed_record(session, acc):
            # 模拟真实播种的证据落库：即使资产不完整，规则结果也必须可查。
            onboarding.record_seed_result(session, acc)
            session.commit()
            return []

        bad_provider = FakeProvider(rows=[{"product": "cvm", "ok": False, "error": "mock"}])
        row, seed_stages = run_bootstrap(
            db, "bootstrap-sync-error", bad_provider, seed=seed_record)
        check("同步失败最终 error", row.status == "error", row.status)
        check("同步失败仍进入播种并留下规则证据", seed_stages == ["error"], seed_stages)
        bad_run = onboarding.get_run(db, row.id)
        check("同步失败不再缺默认规则处理结果",
              any(x.get("key") == "alert_rules" for x in onboarding.serialize(bad_run)["items"]),
              onboarding.serialize(bad_run))
        check("同步失败不伪造同步成功时间", row.last_sync_at is None)

        # 阿里云部分产品失败：SLS 发现仍应启动，不能永远停在“尚未执行”。
        aliyun = account(db, "bootstrap-aliyun-partial", vendor="aliyun")
        sls_calls = []
        with patch.object(sync_job, "get_provider", return_value=FakeProvider(rows=[
                {"product": "ecs", "ok": True, "total": 1},
                {"product": "es", "ok": False, "error": "mock es unreachable"}])), \
                patch.object(sync_job, "seed_rules_for_account", side_effect=seed_record), \
                patch("app.services.sls_discovery.discover",
                      side_effect=lambda *a, **k: sls_calls.append(a)), \
                patch("app.services.onboarding_tasks.prepare_account", return_value={}):
            sync_job.bootstrap_account(aliyun.id)
        check("部分产品失败仍启动 SLS 发现", len(sls_calls) == 1, sls_calls)

        def seed_error(session, acc):
            raise RuntimeError("mock seed failure")

        row, _ = run_bootstrap(db, "bootstrap-seed-error", FakeProvider(), seed=seed_error)
        check("播种失败最终 error", row.status == "error", row.status)
        check("播种失败保留真实同步成功时间", row.last_sync_at is not None)

        stages = []
        row, seed_stages = run_bootstrap(
            db, "bootstrap-probe-error", FakeProvider(probe_ok=False, stages=stages))
        check("探活失败最终 error", row.status == "error", row.status)
        check("探活失败不同步不播种", stages == ["probing"] and not seed_stages,
              (stages, seed_stages))

        # KVM 没有资产采集能力，探活成功即为合法成功终态。
        kvm = account(db, "bootstrap-kvm", vendor="kvm")
        with patch.object(sync_job, "get_provider", return_value=FakeProvider()):
            sync_job.bootstrap_account(kvm.id)
        db.expire_all()
        kvm = db.get(CloudAccount, kvm.id)
        check("仅探活厂商探活成功后 ok", kvm.status == "ok", kvm.status)
        check("仅探活厂商不伪造资产同步时间", kvm.last_sync_at is None)

        # 定时兜底：syncing 可按本轮结果继续；probing 未探活，不能被资产任务直接置 ok。
        syncing = account(db, "auto-syncing", status="syncing")
        probing = account(db, "auto-probing", status="probing")
        partial_auto = account(db, "auto-partial", status="syncing")
        seeded = []

        def auto_seed(session, acc):
            seeded.append((acc.name, acc.status))
            return []

        class PerAccountProvider(FakeProvider):
            def sync(self, db, acc, products=None):
                if acc.name == "auto-partial":
                    return [{"product": "ecs", "ok": True, "total": 1},
                            {"product": "es", "ok": False, "error": "mock es unreachable"}]
                return [{"product": "cvm", "ok": True, "total": 0}]

        with patch.object(sync_job, "get_provider", return_value=PerAccountProvider()), \
                patch.object(sync_job, "seed_rules_for_account", side_effect=auto_seed):
            sync_job.auto_sync_all()
        db.expire_all()
        check("定时兜底完成 syncing 的播种并收口",
              db.get(CloudAccount, syncing.id).status == "ok")
        check("定时兜底不把 probing 提前置 ok",
              db.get(CloudAccount, probing.id).status == "probing")
        check("定时兜底只在 seeding 阶段为接入账号播种",
              ("auto-syncing", "seeding") in seeded
              and all(name != "auto-probing" for name, _stage in seeded), seeded)
        check("定时兜底对部分产品失败账号仍补播种并保留 error",
              ("auto-partial", "error") in seeded
              and db.get(CloudAccount, partial_auto.id).status == "error", seeded)

        # 共享 Onboarding Run：终态必须同时具备凭证、资产和规则证据。
        ready = account(db, "run-ready")
        with patch.object(sync_job, "get_provider", return_value=FakeProvider()):
            sync_job.bootstrap_account(ready.id)   # 使用真实空资产播种分支，记录合法 skipped
        db.expire_all()
        ready = db.get(CloudAccount, ready.id)
        ready_run = onboarding.get_run(db, ready.id)
        check("Run 完整证据后 ready", ready.status == "ok" and ready_run.status == "ready",
              onboarding.serialize(ready_run, ready))
        check("Run 凭证/资产/规则证据齐全",
              {x["key"] for x in onboarding.serialize(ready_run)["items"]}
              == {"credential", "asset:cvm", "alert_rules"})

        partial = account(db, "run-partial")
        with patch.object(sync_job, "get_provider", return_value=FakeProvider(rows=[
                {"product": "cvm", "ok": True, "total": 1},
                {"product": "cdb", "ok": False, "error": "mock cdb denied"}])):
            sync_job.bootstrap_account(partial.id)
        db.expire_all()
        partial_run = onboarding.get_run(db, partial.id)
        check("Run 产品级失败为 attention 而非 ready",
              partial_run.status == "attention"
              and [x["key"] for x in onboarding.serialize(partial_run)["issues"]
                   if x["status"] == "failed"] == ["asset:cdb"],
              onboarding.serialize(partial_run))

        probe_bad = account(db, "run-probe-bad")
        with patch.object(sync_job, "get_provider", return_value=FakeProvider(probe_ok=False)):
            sync_job.bootstrap_account(probe_bad.id)
        db.expire_all()
        check("Run 凭证失败为 failed", onboarding.get_run(db, probe_bad.id).status == "failed")

        manual = account(db, "run-manual-only", status="untested")
        onboarding.record_probe(db, manual, ok=True, message="mock")
        manual.status = "ok"
        onboarding.set_account_stage(db, manual, "ok")
        db.commit()
        check("仅手动探活成功不能冒充 Run ready",
              onboarding.get_run(db, manual.id).status == "attention")
        manual_view = onboarding.serialize(onboarding.get_run(db, manual.id), manual)
        check("手动探活后的缺失资产/规则证据可见",
              {x["key"] for x in manual_view["issues"]} == {"asset:*", "alert_rules"}
              and all(x["status"] == "pending" for x in manual_view["issues"]), manual_view)

        # 升级前账号由定时同步补建 Run：缺凭证探测也必须给出具体原因，而非空问题栏。
        legacy = account(db, "run-legacy-no-probe", status="ok")
        onboarding.record_sync_results(db, legacy, [{
            "account_id": legacy.id, "product": "cvm", "ok": True, "total": 1}])
        legacy.alert_seeded_at = datetime.now()
        onboarding.record_seed_result(db, legacy)
        db.commit()
        legacy_view = onboarding.serialize(onboarding.get_run(db, legacy.id), legacy)
        check("存量账号缺凭证证据时显示待处理原因",
              legacy_view["status"] == "attention"
              and [x["key"] for x in legacy_view["issues"]] == ["credential"], legacy_view)

        # 空结果失败后重接：复用同一 Run、递增 attempts，并清除旧 asset:* 占位失败。
        recovery = account(db, "run-recovery")
        with patch.object(sync_job, "get_provider", return_value=FakeProvider(rows=[])):
            sync_job.bootstrap_account(recovery.id)
        db.expire_all()
        recovery = db.get(CloudAccount, recovery.id)
        old_run = onboarding.get_run(db, recovery.id)
        old_id = old_run.id
        check("Run 空结果保留失败证据", old_run.status == "attention"
              and any(x["key"] == "asset:*" for x in onboarding.serialize(old_run)["issues"]))
        recovery.status = "probing"
        onboarding.reset_run(db, recovery)
        db.commit()
        with patch.object(sync_job, "get_provider", return_value=FakeProvider()):
            sync_job.bootstrap_account(recovery.id)
        db.expire_all()
        recovered = onboarding.get_run(db, recovery.id)
        check("Run 重接复用并递增 attempts", recovered.id == old_id and recovered.attempts == 2,
              (old_id, recovered.id, recovered.attempts))
        check("Run 恢复后 ready 且旧占位失败被清除", recovered.status == "ready"
              and not any(x["key"] == "asset:*" for x in onboarding.serialize(recovered)["items"]))

        req = SimpleNamespace(headers={}, client=SimpleNamespace(host="mock"))
        user = SimpleNamespace(id=1, username="mock-admin")
        disabled_body = cloud_api.AccountIn(
            name="run-disabled", vendor="tencent", ak="AKTEST1234567890",
            sk="SKTESTSECRETDATA9999", enabled=False, ack="我已知晓")
        disabled_out = cloud_api.create_account(disabled_body, req, db, user)
        check("禁用账号创建后为 waiting，不启动后台接入",
              disabled_out["onboarding_status"] == "waiting", disabled_out)

        listed = cloud_api.list_onboarding_runs(db, _user=user)
        detail = cloud_api.get_account_onboarding(ready.id, req, db, _user=user)
        listed_ready = next(x for x in listed["items"] if x["account_id"] == ready.id)
        check("Run 列表与账号详情跨入口一致",
              listed_ready["id"] == detail["id"] == ready_run.id
              and listed_ready["status"] == detail["status"] == "ready")
        managed = cloud_api.list_accounts(db, _user=user)
        managed_partial = next(x for x in managed["items"] if x["id"] == partial.id)
        check("云账号管理列表与 Run 终态一致",
              managed_partial["onboarding_run_id"] == partial_run.id
              and managed_partial["onboarding_status"] == "attention", managed_partial)

        placeholder_body = cloud_api.AccountIn(
            name="run-placeholder", vendor="aliyun", ak="agent-pending-ak123",
            sk="agent-pending-sk123", ack="我已知晓")
        try:
            cloud_api.create_account(placeholder_body, req, db, user)
            placeholder_rejected = False
        except Exception as exc:  # FastAPI HTTPException，无需真实云调用
            placeholder_rejected = getattr(exc, "status_code", None) == 400
        check("明确占位凭证在创建前被拒绝", placeholder_rejected
              and db.query(CloudAccount).filter(CloudAccount.name == "run-placeholder").first() is None)

        # 补播种是另一个完成入口：证据齐后必须立即重派生，不等下一次状态写入。
        onboarding.record_sync_results(db, manual, [{
            "account_id": manual.id, "product": "cvm", "ok": True, "total": 0}])
        manual.alert_seeded_at = datetime.now()
        onboarding.record_seed_result(db, manual)
        db.commit()
        check("存量账号补播种后立即派生 ready", onboarding.get_run(db, manual.id).status == "ready")

        # bootstrap 摘要必须按 Run 计数，不能把 status=ok 但缺证据的账号算 ready。
        enabled = db.query(CloudAccount).filter(CloudAccount.enabled == True).all()  # noqa: E712
        enabled_runs = [onboarding.get_run(db, a.id) for a in enabled]
        expected_ready = sum(r is not None and r.status == "ready" for r in enabled_runs)
        expected_running = sum(r is not None and r.status == "running" for r in enabled_runs)
        summary = bootstrap_api.bootstrap_status(db)
        check("bootstrap 摘要与 Run ready/running 计数一致",
              summary["accounts_ready"] == expected_ready
              and summary["accounts_running"] == expected_running
              and summary["onboarding_ready"] == (expected_ready == len(enabled) and bool(enabled)),
              summary)

        class _BusyAccountLock:
            def acquire(self, blocking=False):
                return False

            def release(self):
                pass

        locked = account(db, "run-lock-timeout")
        with patch.object(sync_job, "account_lock", return_value=_BusyAccountLock()):
            sync_job.bootstrap_account(locked.id)
        db.expire_all()
        locked = db.get(CloudAccount, locked.id)
        locked_run = onboarding.get_run(db, locked.id)
        runtime = next((x for x in onboarding.serialize(locked_run)["items"]
                        if x.get("key") == "runtime"), {})
        check("同账号任务占用时排队等待，不误判失败",
              locked.status == "probing" and locked_run.status == "running"
              and runtime.get("status") == "pending",
              onboarding.serialize(locked_run))
        # O1：旧 Run 的能力证据必须保持未验证，不能从历史 items 自动补成功。
        capability_account = account(db, "capability-evidence", vendor="aliyun", status="ok")
        capability_run = onboarding.ensure_run(db, capability_account)
        db.commit()
        legacy_caps = onboarding.serialize(capability_run, capability_account)["capabilities"]
        check("O1 旧 Run 五能力均为未验证",
              {x["capability"] for x in legacy_caps} == {"assets", "metrics", "logs", "cost", "changes"}
              and all(x["execution"] == "waiting" and x["data_state"] == "unknown"
                      and x["reason_code"] == "not_verified" and not x["coverage"]["total_known"]
                      for x in legacy_caps), legacy_caps)

        onboarding.record_capability(
            db, capability_account, "cost", execution="succeeded", data_state="no_data",
            coverage={"discovered": 0, "checked": 1, "failed": 0, "unknown": 0,
                      "total_known": True, "truncated": False},
            checked_at=datetime.now(), evidence_refs=["cost:bill:mock"], reason_code="empty_bill")
        onboarding.record_capability(
            db, capability_account, "metrics", scope={"region": "cn-mock"},
            execution="succeeded", data_state="available",
            coverage={"discovered": 3, "checked": 2, "failed": 1, "unknown": None,
                      "total_known": False, "truncated": True},
            checked_at=datetime.now(), evidence_refs=["metrics:readback:mock"])
        db.commit()
        capability_view = onboarding.serialize(capability_run, capability_account)
        caps_by_name = {x["capability"]: x for x in capability_view["capabilities"]}
        check("O1 执行成功与数据可用分离",
              caps_by_name["cost"]["data_state"] == "no_data"
              and caps_by_name["metrics"]["data_state"] == "available"
              and caps_by_name["metrics"]["scope"]["region"] == "cn-mock", capability_view)
        check("O1 分母未知与截断显式保留",
              caps_by_name["metrics"]["coverage"]["total_known"] is False
              and caps_by_name["metrics"]["coverage"]["truncated"] is True)
        detail_caps = cloud_api.get_account_onboarding(
            capability_account.id, req, db, _user=user)["capabilities"]
        listed_caps = next(x for x in cloud_api.list_onboarding_runs(db, _user=user)["items"]
                           if x["account_id"] == capability_account.id)["capabilities"]
        check("O1 相同 Run 在列表与详情返回相同能力证据", detail_caps == listed_caps)
        with patch.object(sync_job, "get_provider", side_effect=AssertionError("状态读取触发云调用")):
            cloud_api.get_account_onboarding(capability_account.id, req, db, _user=user)
            capability_status = bootstrap_api.bootstrap_status(db)
        check("O1 状态读取不触发云调用", True)
        check("O1 bootstrap 只汇总同一份能力证据",
              capability_status["capability_summary"]["cost"]["no_data"] >= 1
              and capability_status["capability_summary"]["metrics"]["available"] >= 1,
              capability_status["capability_summary"])

        # 按账号×能力×范围重试：成功项不重跑，同范围并发复用，旧 revision 不得覆盖。
        from app.core.security import encrypt_secret
        retry_account = account(db, "capability-retry", vendor="aliyun", status="ok")
        retry_account.ak_cipher = encrypt_secret("AKTESTRETRY123456")
        retry_account.secret_cipher = encrypt_secret("SKTESTRETRY123456")
        retry_run = onboarding.ensure_run(db, retry_account)
        onboarding.record_capability(db, retry_account, "cost", execution="failed",
                                     data_state="unknown", reason_code="denied", retryable=True)
        onboarding.record_capability(db, retry_account, "metrics", execution="succeeded",
                                     data_state="available", reason_code="")
        other_account = account(db, "capability-other", vendor="aliyun", status="ok")
        onboarding.ensure_run(db, other_account)
        db.commit()
        started_threads = []

        class _FakeThread:
            def __init__(self, *, target, args=(), daemon=None, name=None):
                self.target, self.args = target, args
                started_threads.append(self)

            def start(self):
                return None

        retry_body = cloud_api.RetryRequest(scopes=[
            cloud_api.RetryScope(capability="cost", region="cn-mock"),
            cloud_api.RetryScope(capability="metrics"),
        ])
        fake_tasks = SimpleNamespace(retry_scopes=lambda *args: None)
        with patch.dict(sys.modules, {"app.services.onboarding_tasks": fake_tasks}), \
                patch.object(cloud_api.threading, "Thread", _FakeThread):
            retry_out = cloud_api.retry_onboarding_capabilities(
                retry_account.id, retry_body, req, db, user)
            first_thread_count = len(started_threads)
            retry_again = cloud_api.retry_onboarding_capabilities(
                retry_account.id, cloud_api.RetryRequest(scopes=[
                    cloud_api.RetryScope(capability="cost", region="cn-mock")]), req, db, user)
        check("O1 重试只启动失败范围且复用同一 Run",
              retry_out["run_id"] == retry_run.id and len(retry_out["started"]) == 1
              and retry_out["started"][0]["scope"]["region"] == "cn-mock"
              and len(retry_out["unchanged"]) == 1 and first_thread_count == 1, retry_out)
        check("O1 同范围并发重试返回当前任务不新建线程",
              len(retry_again["reused"]) == 1 and not retry_again["started"]
              and len(started_threads) == first_thread_count, retry_again)
        check("O1 重试不改变其他账号能力状态",
              all(x["execution"] == "waiting" for x in onboarding.serialize(
                  onboarding.get_run(db, other_account.id), other_account)["capabilities"]))
        stale = onboarding.record_capability(
            db, retry_account, "cost", scope={"region": "cn-mock"}, expected_revision=0,
            execution="succeeded", data_state="available", revision=1)
        current_cost = next(x for x in onboarding.serialize(retry_run, retry_account)["capabilities"]
                            if x["capability"] == "cost" and x["scope"]["region"] == "cn-mock")
        check("O1 晚到旧 revision 被丢弃", stale is None and current_cost["execution"] == "running")
        fresh = onboarding.record_capability(
            db, retry_account, "cost", scope={"region": "cn-mock"}, expected_revision=1,
            execution="succeeded", data_state="available", revision=1,
            coverage={"total_known": True, "discovered": 1, "checked": 1, "failed": 0, "unknown": 0})
        check("O1 当前 revision 可以落证据", fresh is not None and fresh["data_state"] == "available")

        # 服务端权限必须经真实 ASGI 依赖链验证，而不是手工调用绕过 Depends。
        import asyncio
        import httpx
        from app.core.db import get_db
        from app.core.deps import get_current_user
        from app.main import app
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=9, username="mock-viewer", role="viewer", is_active=True)

        async def _viewer_retry():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                return await client.post(
                    f"/api/clouds/accounts/{retry_account.id}/onboarding/retry",
                    json={"scopes": [{"capability": "cost"}]})
        denied = asyncio.run(_viewer_retry())
        check("O1 viewer 不能启动写入型重试", denied.status_code == 403, denied.text)
        app.dependency_overrides.clear()

        disabled = account(db, "capability-disabled", vendor="aliyun", status="ok")
        disabled.enabled = False
        db.commit()
        try:
            cloud_api.retry_onboarding_capabilities(
                disabled.id, cloud_api.RetryRequest(scopes=[cloud_api.RetryScope(capability="cost")]),
                req, db, user)
            disabled_rejected = False
        except Exception as exc:
            disabled_rejected = getattr(exc, "status_code", None) == 409
        check("O1 停用账号拒绝执行", disabled_rejected)

        bad_secret = account(db, "capability-bad-secret", vendor="aliyun", status="ok")
        bad_secret.ak_cipher = "not-decryptable"
        bad_secret.secret_cipher = "not-decryptable"
        db.commit()
        try:
            cloud_api.retry_onboarding_capabilities(
                bad_secret.id, cloud_api.RetryRequest(scopes=[cloud_api.RetryScope(capability="cost")]),
                req, db, user)
            secret_rejected = False
        except Exception as exc:
            secret_rejected = getattr(exc, "status_code", None) == 400
        check("O1 凭证解密失败拒绝执行", secret_rejected)
    finally:
        db.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
