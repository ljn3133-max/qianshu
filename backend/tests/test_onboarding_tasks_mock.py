#!/usr/bin/env python3
"""Onboarding 自动准备隔离回归：临时库、假凭证、采集替身，零云写入/通知。"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from unittest import mock
import unittest

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-onboarding-tasks-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("tasks")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
for _path in (BACKEND / "app" / "models").glob("*.py"):
    if _path.stem != "__init__":
        importlib.import_module(f"app.models.{_path.stem}")

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.services import onboarding, onboarding_tasks  # noqa: E402
from app.services import cost_job, change_source  # noqa: E402
from app.collector import metrics_job  # noqa: E402
from app.models.change_event import ChangeEvent  # noqa: E402


def collection_ok(account_id, dates, rows=1, amortized_active=True):
    return {
        "account_id": account_id,
        "lock_waiting": False,
        "days": [{
            "bill_date": day.isoformat(), "ok": True, "rows": rows,
            "amount": 0 if rows == 0 else 12.5, "amortized_active": amortized_active,
            "notes": [] if amortized_active else ["摊销成本未开通"],
            "reason_code": "" if amortized_active else "amortized_unavailable",
            "retryable": False, "error": "",
        } for day in dates],
    }


class OnboardingTaskTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.account = self.new_account("mock-a")
        self.other = self.new_account("mock-b")

    def tearDown(self):
        self.db.close()

    def new_account(self, name):
        row = CloudAccount(
            name=name, vendor="aliyun", enabled=True, status="ok",
            ak_cipher=encrypt_secret("AKTEST1234567890"),
            secret_cipher=encrypt_secret("SKTEST1234567890"),
        )
        self.db.add(row)
        self.db.commit()
        onboarding.ensure_run(self.db, row)
        self.db.commit()
        return row

    def capabilities(self, account_id):
        run = onboarding.get_run(self.db, account_id)
        return onboarding.serialize(run, self.db.get(CloudAccount, account_id))["capabilities"]

    def test_cost_adapter_enters_explicit_account_and_preserves_daily_results(self):
        from app.collector.credential import active_account_id
        days = [date.today() - timedelta(days=offset) for offset in (1, 2, 3)]
        calls = []

        def fake_collect(db, ak, sk, day):
            calls.append((active_account_id(), day))
            if day == days[1]:
                raise cost_job.BSSError("mock 403 permission denied", code="NoPermission")
            return {"rows": 1, "amount": 10, "amortized_active": True, "notes": []}

        with mock.patch.object(cost_job, "_collect_one_day", side_effect=fake_collect), \
                mock.patch.object(cost_job, "detect_cost_for_date", side_effect=AssertionError("禁止异动附带流程")), \
                mock.patch.object(cost_job, "snapshot_and_detect_packs", side_effect=AssertionError("禁止包快照")):
            result = cost_job.collect_account_days(self.db, self.account, days)
        self.assertEqual([item[0] for item in calls], [self.account.id] * 3)
        self.assertIsNone(active_account_id())
        self.assertEqual(len(result["days"]), 3)
        self.assertTrue(result["days"][0]["ok"])
        self.assertFalse(result["days"][1]["ok"])
        self.assertFalse(result["days"][1]["retryable"])
        self.assertTrue(result["days"][2]["ok"])

    def test_first_bill_day_precedes_thirty_day_backfill(self):
        yesterday = date.today() - timedelta(days=1)
        calls = []

        def fake_collect(db, account, dates):
            calls.append(list(dates))
            return collection_ok(account.id, dates)

        with mock.patch.object(cost_job, "collect_account_days", side_effect=fake_collect), \
                mock.patch.object(cost_job, "run_collection", side_effect=AssertionError("禁止整段复用 run_collection")), \
                mock.patch.object(cost_job, "detect_cost_for_date", side_effect=AssertionError("禁止通知")), \
                mock.patch.object(cost_job, "snapshot_and_detect_packs", side_effect=AssertionError("禁止包快照")):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["cost"])
        flattened = [day for call in calls for day in call]
        self.assertEqual(flattened[0], yesterday)
        self.assertEqual(len(flattened), 30)
        self.assertEqual(len(set(flattened)), 30)
        costs = [item for item in self.capabilities(self.account.id) if item["capability"] == "cost"]
        self.assertEqual(len(costs), 31)  # 30 个账单日 + 1 个账号汇总
        summary = next(item for item in costs if not item["scope"]["bill_date"])
        self.assertEqual(summary["execution"], "succeeded")
        self.assertEqual(summary["data_state"], "available")
        self.assertEqual(summary["coverage"]["checked"], 30)
        self.assertTrue(all(item["execution"] == "waiting" for item in self.capabilities(self.other.id)))

    def test_daily_failure_does_not_clear_success_or_retry_403(self):
        yesterday = date.today() - timedelta(days=1)
        denied_day = yesterday - timedelta(days=1)
        calls = []

        def fake_collect(db, account, dates):
            calls.append(list(dates))
            result = collection_ok(account.id, dates)
            for item in result["days"]:
                day = date.fromisoformat(item["bill_date"])
                if day == denied_day:
                    item.update(ok=False, rows=0, reason_code="permission_denied",
                                retryable=False, error="mock 403")
            return result

        with mock.patch.object(cost_job, "collect_account_days", side_effect=fake_collect), \
                mock.patch.object(onboarding_tasks.time, "sleep", return_value=None):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["cost"])
            onboarding_tasks.prepare_account(self.account.id, capabilities=["cost"])
        flattened = [day for call in calls for day in call]
        self.assertNotIn(denied_day, flattened[30:], "明确 403 不得在自动准备中反复重试")
        costs = [item for item in self.capabilities(self.account.id) if item["capability"] == "cost"]
        first = next(item for item in costs if item["scope"]["bill_date"] == yesterday.isoformat())
        denied = next(item for item in costs if item["scope"]["bill_date"] == denied_day.isoformat())
        summary = next(item for item in costs if not item["scope"]["bill_date"])
        self.assertEqual(first["execution"], "succeeded")
        self.assertEqual(first["data_state"], "available")
        self.assertEqual(denied["execution"], "failed")
        self.assertFalse(denied["retryable"])
        self.assertEqual(summary["execution"], "failed")
        self.assertEqual(summary["data_state"], "available")

    def test_transient_lock_waits_then_succeeds_without_stale_success(self):
        yesterday = date.today() - timedelta(days=1)
        attempts = []

        def fake_collect(db, account, dates):
            attempts.append(list(dates))
            if len(attempts) < 3:
                return {"account_id": account.id, "lock_waiting": True, "days": []}
            return collection_ok(account.id, dates, rows=0)

        with mock.patch.object(cost_job, "collect_account_days", side_effect=fake_collect), \
                mock.patch.object(onboarding_tasks.time, "sleep", return_value=None):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["cost"], dates=[yesterday])
        self.assertEqual(len(attempts), 3)
        result = next(item for item in self.capabilities(self.account.id)
                      if item["capability"] == "cost" and item["scope"]["bill_date"] == yesterday.isoformat())
        self.assertEqual(result["execution"], "succeeded")
        self.assertEqual(result["data_state"], "no_data")
        self.assertNotEqual(result["reason_code"], "collection_locked")

    def test_lock_exhaustion_is_waiting_not_borrowed_success(self):
        yesterday = date.today() - timedelta(days=1)
        with mock.patch.object(cost_job, "collect_account_days", return_value={
                "account_id": self.account.id, "lock_waiting": True, "days": []}), \
                mock.patch.object(onboarding_tasks.time, "sleep", return_value=None):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["cost"], dates=[yesterday])
        result = next(item for item in self.capabilities(self.account.id)
                      if item["capability"] == "cost" and item["scope"]["bill_date"] == yesterday.isoformat())
        self.assertEqual(result["execution"], "waiting")
        self.assertEqual(result["data_state"], "unknown")
        self.assertEqual(result["reason_code"], "collection_locked")
        self.assertTrue(result["retryable"])

    def test_disabled_or_bad_credential_never_calls_cloud(self):
        self.account.enabled = False
        self.db.commit()
        with mock.patch.object(cost_job, "collect_account_days") as collect:
            onboarding_tasks.prepare_account(self.account.id, capabilities=["cost"], dates=[date.today() - timedelta(days=1)])
        collect.assert_not_called()
        self.account.enabled = True
        self.account.ak_cipher = "not-decryptable"
        self.db.commit()
        with mock.patch.object(cost_job, "collect_account_days") as collect:
            onboarding_tasks.prepare_account(self.account.id, capabilities=["cost"], dates=[date.today() - timedelta(days=1)])
        collect.assert_not_called()

    def test_bad_credential_finishes_capability_instead_of_leaving_running(self):
        self.account.ak_cipher = "not-decryptable"
        self.db.commit()
        result = onboarding_tasks.prepare_account(
            self.account.id, capabilities=["cost"], dates=[date.today() - timedelta(days=1)])
        self.assertFalse(result["accepted"])
        summary = next(item for item in self.capabilities(self.account.id)
                       if item["capability"] == "cost" and not item["scope"]["bill_date"])
        self.assertEqual(summary["execution"], "failed")
        self.assertEqual(summary["reason_code"], "credential_unavailable")
        self.assertFalse(summary["retryable"])

    def test_reap_marks_only_interrupted_account_without_cloud_calls(self):
        run = onboarding.get_run(self.db, self.account.id)
        other_run = onboarding.get_run(self.db, self.other.id)
        other_status = other_run.status
        onboarding.record_capability(
            self.db, self.account, "cost", scope={"bill_date": "2026-11-01"},
            revision=1, execution="running", data_state="unknown")
        self.db.commit()
        with mock.patch.object(cost_job, "collect_account_days") as collect:
            self.assertEqual(onboarding_tasks.reap_interrupted(), 1)
        collect.assert_not_called()
        self.db.expire_all()
        interrupted = next(item for item in onboarding.capability_results(run, self.account)
                           if item["capability"] == "cost" and item["scope"]["bill_date"])
        self.assertEqual(interrupted["execution"], "failed")
        self.assertEqual(interrupted["reason_code"], "interrupted")
        self.assertTrue(interrupted["retryable"])
        self.assertEqual(other_run.status, other_status)

    def add_metric_assets(self):
        rows = [
            Asset(account_id=self.account.id, product="ecs", instance_id="i-a",
                  region="cn-test", status="Running"),
            Asset(account_id=self.other.id, product="ecs", instance_id="i-b",
                  region="cn-test", status="Running"),
        ]
        self.db.add_all(rows)
        self.db.commit()

    def test_metrics_explicit_account_write_and_exact_readback(self):
        from app.collector.credential import active_account_id
        self.add_metric_assets()
        now_ms = 1_800_000_000_000
        seen_accounts = []
        written = []

        def fake_query(_client, _namespace, _metric, instance_ids, start, end, _period):
            seen_accounts.append(active_account_id())
            return [{"instanceId": iid, "Average": 12.5, "timestamp": now_ms - 60_000}
                    for iid in instance_ids]

        def fake_read(selector, _start, _end):
            # 选择器只能按产品/区域取数，不能把实例 ID 拼进 PromQL：
            # re.escape 会把 i-bp... 的 '-' 写成 '\-'，VM 直接 422。
            self.assertNotIn("instance_id", selector)
            self.assertNotIn("i-a", selector)
            self.assertNotIn("i-b", selector)
            self.assertNotIn("\\", selector)
            name = selector.split("{", 1)[0]
            # export 返回样本自身 ms 时间戳；归属本账号的 i-a 和无关账号的 i-b 都在窗口内，
            # 由 Python 侧按 instance_id 过滤，只有 i-a 计数。
            return [{"metric": {"__name__": name, "instance_id": "i-a",
                                "product": "ecs", "region": "cn-test"},
                     "timestamps": [now_ms - 60_000], "values": [12.5]},
                    {"metric": {"__name__": name, "instance_id": "i-b",
                                "product": "ecs", "region": "cn-test"},
                     "timestamps": [now_ms - 60_000], "values": [12.5]}]

        with mock.patch.object(metrics_job, "_build_client", return_value=object()), \
                mock.patch.object(metrics_job, "_query_batch", side_effect=fake_query), \
                mock.patch.object(metrics_job, "write_prometheus", side_effect=lambda lines: written.extend(lines) or True), \
                mock.patch.object(metrics_job, "export_samples", side_effect=fake_read), \
                mock.patch.object(metrics_job.time, "time", return_value=now_ms / 1000):
            result = metrics_job.collect_account(self.db, self.account)
        self.assertEqual(set(seen_accounts), {self.account.id})
        self.assertIsNone(active_account_id())
        self.assertTrue(written and all("i-b" not in line for line in written))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data_state"], "available")
        self.assertEqual(result["checked"], 1)
        self.assertGreater(result["points_read"], 0)
        self.assertEqual(result["evidence_refs"], [f"metrics:{self.account.id}:i-a"])

    def test_metrics_readback_waits_for_vm_visibility_delay(self):
        """VM import 到可查询有秒级延迟：首轮回读为空时短轮询，不能直接判 no_metric_data。"""
        self.add_metric_assets()
        now_ms = int(time.time() * 1000)
        calls = []

        def fake_read(_selector, _start, _end):
            calls.append(1)
            if len(calls) == 1:
                return []
            return [{"metric": {"instance_id": "i-a", "product": "ecs",
                                "region": "cn-test", "__name__": "aliyun_ecs_cpu_total"},
                     "timestamps": [now_ms], "values": [1]}]

        with mock.patch.object(metrics_job, "_build_client", return_value=object()), \
                mock.patch.object(metrics_job, "_query_batch",
                                  return_value=[{"instanceId": "i-a", "Average": 1,
                                                 "timestamp": now_ms}]), \
                mock.patch.object(metrics_job, "write_prometheus", return_value=True), \
                mock.patch.object(metrics_job, "export_samples", side_effect=fake_read), \
                mock.patch.object(metrics_job.time, "sleep", return_value=None):
            result = metrics_job.collect_account(self.db, self.account)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked"], 1)
        self.assertGreater(result["points_read"], 0)

    def test_metrics_vm_write_failure_cannot_borrow_old_readback(self):
        self.add_metric_assets()
        datapoints = [{"instanceId": "i-a", "Average": 1, "timestamp": 1_800_000_000_000}]
        with mock.patch.object(metrics_job, "_build_client", return_value=object()), \
                mock.patch.object(metrics_job, "_query_batch", return_value=datapoints), \
                mock.patch.object(metrics_job, "write_prometheus", return_value=False), \
                mock.patch.object(metrics_job, "export_samples") as read:
            result = metrics_job.collect_account(self.db, self.account)
        read.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "vm_write_failed")
        self.assertEqual(result["data_state"], "unknown")

    def test_metrics_vm_readback_failure_is_not_available(self):
        self.add_metric_assets()
        datapoints = [{"instanceId": "i-a", "Average": 1, "timestamp": 1_800_000_000_000}]
        with mock.patch.object(metrics_job, "_build_client", return_value=object()), \
                mock.patch.object(metrics_job, "_query_batch", return_value=datapoints), \
                mock.patch.object(metrics_job, "write_prometheus", return_value=True), \
                mock.patch.object(metrics_job, "_READBACK_DEADLINE_S", 0), \
                mock.patch.object(metrics_job, "export_samples", side_effect=RuntimeError("mock VM down")):
            result = metrics_job.collect_account(self.db, self.account)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "vm_readback_failed")
        self.assertEqual(result["data_state"], "unknown")
        self.assertEqual(result["points_read"], 0)

    def test_metrics_no_samples_and_permission_failure_are_distinct(self):
        self.add_metric_assets()
        with mock.patch.object(metrics_job, "_build_client", return_value=object()), \
                mock.patch.object(metrics_job, "_query_batch", return_value=[]), \
                mock.patch.object(metrics_job, "write_prometheus", return_value=True):
            empty = metrics_job.collect_account(self.db, self.account)
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["data_state"], "no_data")
        self.assertEqual(empty["reason_code"], "no_metric_data")

        with mock.patch.object(metrics_job, "_build_client", return_value=object()), \
                mock.patch.object(metrics_job, "_query_batch", side_effect=RuntimeError("mock 403 Forbidden")), \
                mock.patch.object(metrics_job, "write_prometheus", return_value=True):
            denied = metrics_job.collect_account(self.db, self.account)
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason_code"], "permission_denied")
        self.assertFalse(denied["retryable"])

    def test_metrics_transient_retry_and_permanent_failure_preserved(self):
        yesterday = date.today() - timedelta(days=1)
        success = {"account_id": self.account.id, "lock_waiting": False, "ok": True,
                   "data_state": "no_data", "reason_code": "no_supported_assets",
                   "retryable": False, "discovered": 0, "checked": 0,
                   "evidence_refs": [], "observed_until": None, "truncated": False}
        with mock.patch.object(metrics_job, "collect_account", side_effect=[
                {"account_id": self.account.id, "lock_waiting": True},
                {"account_id": self.account.id, "lock_waiting": True}, success]) as collect, \
                mock.patch.object(onboarding_tasks.time, "sleep", return_value=None):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["metrics"])
        self.assertEqual(collect.call_count, 3)
        metric = next(item for item in self.capabilities(self.account.id)
                      if item["capability"] == "metrics")
        self.assertEqual(metric["execution"], "succeeded")
        self.assertEqual(metric["data_state"], "no_data")

        denied = {**success, "account_id": self.other.id, "ok": False,
                  "data_state": "unknown", "reason_code": "permission_denied"}
        with mock.patch.object(metrics_job, "collect_account", return_value=denied) as collect:
            onboarding_tasks.prepare_account(self.other.id, capabilities=["metrics"], dates=[yesterday])
            onboarding_tasks.prepare_account(self.other.id, capabilities=["metrics"], dates=[yesterday])
        self.assertEqual(collect.call_count, 1)
        other_metric = next(item for item in self.capabilities(self.other.id)
                            if item["capability"] == "metrics")
        self.assertEqual(other_metric["execution"], "failed")
        self.assertEqual(other_metric["reason_code"], "permission_denied")

    def test_metrics_resource_scope_limits_collection_not_only_result_card(self):
        self.add_metric_assets()
        self.db.add(Asset(account_id=self.account.id, product="ecs", instance_id="i-c",
                          region="cn-other", status="Running"))
        self.db.commit()
        collected = {"errors": [], "_write_ok": True, "_evidence": [],
                     "_window_start_ms": 0, "_window_end_ms": 1000, "points_written": 0}
        with mock.patch.object(metrics_job, "_run_collection_locked", return_value=collected) as run:
            result = metrics_job.collect_account(
                self.db, self.account, resource_ids=["i-c"], region="cn-other")
        self.assertEqual(run.call_args.kwargs["resource_ids"], ["i-c"])
        self.assertEqual(run.call_args.kwargs["region"], "cn-other")
        self.assertEqual(result["discovered"], 1)

        prepared = onboarding.prepare_retries(self.db, self.account, [{
            "capability": "metrics", "region": "cn-other", "resource_ids": ["i-c"]}])
        self.db.commit()
        run = onboarding.get_run(self.db, self.account.id)
        with mock.patch.object(metrics_job, "collect_account", return_value={
                "account_id": self.account.id, "lock_waiting": False, "ok": True,
                "data_state": "no_data", "reason_code": "no_metric_data", "retryable": False,
                "discovered": 1, "checked": 0, "evidence_refs": [], "observed_until": None,
                "truncated": True}) as collect:
            onboarding_tasks.retry_scopes(self.account.id, run.id, prepared["started"])
        self.assertEqual(collect.call_args.kwargs["resource_ids"], ["i-c"])
        self.assertEqual(collect.call_args.kwargs["region"], "cn-other")

    def test_changes_explicit_account_window_and_scoped_persistence(self):
        from app.collector.credential import active_account_id
        seen = []
        events = [
            change_source.NormalizedChange("event-a", datetime.now() - timedelta(hours=1), resource="i-a"),
            change_source.NormalizedChange("event-c", datetime.now() - timedelta(hours=2), resource="i-c"),
        ]
        source = mock.Mock(key="actiontrail")

        def fetch(**kwargs):
            seen.append((active_account_id(), kwargs["account"].id,
                         kwargs["end"] - kwargs["start"], kwargs["max_pages"]))
            return change_source.ChangeBatch(events=events)

        source.fetch.side_effect = fetch
        with mock.patch.object(change_source, "source_for_account", return_value=source):
            result = change_source.collect_account(self.db, self.account, resource_ids=["i-a"])
        self.assertEqual(seen, [(self.account.id, self.account.id, timedelta(hours=24), 20)])
        self.assertIsNone(active_account_id())
        self.assertTrue(result["ok"])
        self.assertEqual(result["data_state"], "available")
        self.assertEqual(result["fetched"], 1)
        rows = self.db.query(ChangeEvent).filter_by(account_id=self.account.id).all()
        self.assertEqual([row.external_id for row in rows], ["event-a"])
        self.assertTrue(result["evidence_refs"][0].startswith(
            f"change_window:{self.account.id}:actiontrail:"))

    def test_changes_empty_window_is_not_an_event_or_health_claim(self):
        source = mock.Mock(key="actiontrail")
        source.fetch.return_value = change_source.ChangeBatch(events=[])
        with mock.patch.object(change_source, "source_for_account", return_value=source):
            result = change_source.collect_account(self.db, self.account)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data_state"], "no_data")
        self.assertEqual(result["reason_code"], "empty_window")
        self.assertIsNone(result["observed_until"])
        self.assertEqual(result["fetched"], 0)
        self.assertTrue(result["evidence_refs"])

    def test_changes_permission_denied_is_not_retried(self):
        source = mock.Mock(key="actiontrail")
        source.fetch.side_effect = RuntimeError("mock 403 NoPermission")
        with mock.patch.object(change_source, "source_for_account", return_value=source):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["changes"])
            onboarding_tasks.prepare_account(self.account.id, capabilities=["changes"])
        self.assertEqual(source.fetch.call_count, 1)
        capability = next(item for item in self.capabilities(self.account.id)
                          if item["capability"] == "changes")
        self.assertEqual(capability["execution"], "failed")
        self.assertEqual(capability["data_state"], "unknown")
        self.assertEqual(capability["reason_code"], "permission_denied")
        self.assertFalse(capability["retryable"])

    def test_changes_truncation_preserves_events_and_requires_narrower_window(self):
        source = mock.Mock(key="actiontrail")
        source.fetch.return_value = change_source.ChangeBatch(events=[
            change_source.NormalizedChange("event-partial", datetime.now(), resource="i-a")],
            fetched=100, truncated=True)
        with mock.patch.object(change_source, "source_for_account", return_value=source):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["changes"])
            onboarding_tasks.prepare_account(self.account.id, capabilities=["changes"])
        self.assertEqual(source.fetch.call_count, 1)
        capability = next(item for item in self.capabilities(self.account.id)
                          if item["capability"] == "changes")
        self.assertEqual(capability["execution"], "failed")
        self.assertEqual(capability["data_state"], "available")
        self.assertEqual(capability["reason_code"], "window_truncated")
        self.assertEqual(capability["next_action"], "narrow_window")
        self.assertFalse(capability["coverage"]["total_known"])
        self.assertTrue(capability["coverage"]["truncated"])
        self.assertEqual(self.db.query(ChangeEvent).filter_by(account_id=self.account.id).count(), 1)

    def test_changes_transient_failure_retries_three_times_and_source_is_fenced(self):
        source = mock.Mock(key="actiontrail")
        source.fetch.side_effect = [RuntimeError("mock timeout"), RuntimeError("mock timeout"),
                                    change_source.ChangeBatch(events=[])]
        with mock.patch.object(change_source, "source_for_account", return_value=source), \
                mock.patch.object(onboarding_tasks.time, "sleep", return_value=None):
            onboarding_tasks.prepare_account(self.account.id, capabilities=["changes"])
        self.assertEqual(source.fetch.call_count, 3)
        capability = next(item for item in self.capabilities(self.account.id)
                          if item["capability"] == "changes")
        self.assertEqual(capability["execution"], "succeeded")
        self.assertEqual(capability["data_state"], "no_data")

        wrong_source = mock.Mock(key="actiontrail")
        with mock.patch.object(change_source, "source_for_account", return_value=wrong_source):
            result = change_source.collect_account(self.db, self.other, source_id="other-source")
        wrong_source.fetch.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])

    def test_capability_writes_hold_run_row_lock_and_do_not_lose_evidence(self):
        """两个能力并发回写时，后写者必须基于最新整列，不能抹掉先提交的证据。"""
        s1, s2 = SessionLocal(), SessionLocal()
        try:
            acc1 = s1.get(CloudAccount, self.account.id)
            acc2 = s2.get(CloudAccount, self.account.id)
            onboarding._lock_run(s1, acc1)   # 主事务先持锁
            finished = threading.Event()
            errors = []

            def writer():
                try:
                    onboarding.record_capability(
                        s2, acc2, "cost", execution="succeeded",
                        data_state="no_data", reason_code="empty_bill")
                    s2.commit()
                except Exception as exc:  # noqa: BLE001 - 测试线程收集原始异常
                    errors.append(exc)
                finally:
                    finished.set()

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            time.sleep(0.4)
            self.assertFalse(finished.is_set(), "并发行锁未生效：第二个写事务没有等待")
            onboarding.record_capability(
                s1, acc1, "metrics", execution="succeeded",
                data_state="no_data", reason_code="no_metric_data")
            s1.commit()
            thread.join(10)
            self.assertTrue(finished.is_set())
            self.assertFalse(errors)
        finally:
            s1.close()
            s2.close()
        self.db.expire_all()
        caps = {item["capability"]: item for item in self.capabilities(self.account.id)}
        self.assertEqual(caps["metrics"]["execution"], "succeeded")
        self.assertEqual(caps["cost"]["execution"], "succeeded",
                         "后写事务覆盖了先提交的能力证据")

    def test_asset_sync_records_assets_capability(self):
        from app.services.cloud import sync_job

        class FakeProvider:
            def sync(self, _db, _acc, products=None):
                return [{"product": "ecs", "total": 3, "created": 3, "updated": 0}]

        with mock.patch.object(sync_job, "get_provider", return_value=FakeProvider()):
            out = sync_job.sync_accounts(self.db, account_ids=[self.account.id], force=True)
        self.assertEqual(out["summary"]["failed_accounts"], [])
        self.db.expire_all()
        assets = next(item for item in self.capabilities(self.account.id)
                      if item["capability"] == "assets")
        self.assertEqual(assets["execution"], "succeeded")
        self.assertEqual(assets["data_state"], "available")
        self.assertEqual(assets["coverage"]["discovered"], 3)
        self.assertEqual(assets["evidence_refs"], [f"assets:{self.account.id}"])

    def test_asset_sync_skips_account_busy_instead_of_duplicating_cloud_calls(self):
        from app.services.cloud import sync_job

        provider = mock.Mock()
        provider.sync = mock.Mock(return_value=[])
        result = {}
        lock = sync_job.account_lock(self.account.id)
        lock.acquire()
        try:
            def run():
                db = SessionLocal()
                try:
                    with mock.patch.object(sync_job, "get_provider", return_value=provider):
                        result["out"] = sync_job.sync_accounts(
                            db, account_ids=[self.account.id], force=True)
                finally:
                    db.close()

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(10)
        finally:
            lock.release()
        self.assertFalse(thread.is_alive())
        provider.sync.assert_not_called()
        row = result["out"]["results"][0]
        self.assertEqual(row["skipped_reason"], "account_busy")
        self.assertNotIn(self.account.id, result["out"]["summary"]["failed_accounts"])
        self.assertEqual(result["out"]["summary"]["failed_accounts"], [])

    def test_finish_run_promotes_error_account_only_when_evidence_ready(self):
        onboarding.record_probe(self.db, self.account, ok=True, message="ok")
        onboarding.record_sync_results(self.db, self.account, [
            {"account_id": self.account.id, "product": "ecs", "total": 1, "created": 1}])
        onboarding.record_seed_result(self.db, self.account)
        onboarding.record_sls_discovery(self.db, self.account, {
            "status": "skipped", "coverage": {"logstores": 0}})
        self.account.status = "error"
        self.db.commit()
        onboarding_tasks._finish_run(self.db, self.account)
        self.assertEqual(onboarding.get_run(self.db, self.account.id).status, "ready")
        self.assertEqual(self.account.status, "ok")

    def test_finish_run_keeps_error_when_evidence_incomplete(self):
        self.account.status = "error"
        self.db.commit()
        onboarding_tasks._finish_run(self.db, self.account)
        self.assertEqual(onboarding.get_run(self.db, self.account.id).status, "attention")
        self.assertEqual(self.account.status, "error")

    def test_runtime_waiting_is_not_reported_as_failure(self):
        onboarding.record_runtime_waiting(self.db, self.account, "该账号已有任务在跑，已排队")
        self.db.commit()
        run = onboarding.get_run(self.db, self.account.id)
        self.assertEqual(run.status, "running")
        self.assertEqual(self.account.status, "ok")
        runtime = next(item for item in onboarding.serialize(run, self.account)["items"]
                       if item.get("key") == "runtime")
        self.assertEqual(runtime["status"], "pending")


if __name__ == "__main__":
    unittest.main()
