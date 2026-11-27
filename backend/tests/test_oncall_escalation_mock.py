#!/usr/bin/env python3
"""值班升级发送结果回归测试。

全部通知出口与凭证解析均 mock，数据库使用一次性 PostgreSQL；绝不触达真实通知渠道。
运行：cd /opt/qianshu/backend && python3 -m unittest tests.test_oncall_escalation_mock -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api.v1.oncall import list_escalations  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.oncall import AlertEscalation, OncallContact  # noqa: E402
from app.services import notify as nt  # noqa: E402
from app.services import oncall as oc  # noqa: E402


class OncallEscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qs-oncall-")
        self.engine = create_engine(test_database_url("oncall"))
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

        # 防线 1：测试模式；防线 2：统一通知、短信、语音全部替换成 Mock。
        self.patches = [
            patch.object(oc.settings, "ONCALL_ENABLED", True),
            patch.object(oc.settings, "NOTIFY_TEST_MODE", True),
            patch.object(oc.settings, "ALERT_ESCALATE_MINUTES", 15),
            patch("app.services.monitor_policy.can_push", return_value=True),
            patch("app.collector.credential.resolve_aliyun_credential",
                  return_value=("mock-ak", "mock-sk", "mock")),
        ]
        for p in self.patches:
            p.start()
        self.notify_all = patch.object(
            nt, "notify_all_channels",
            return_value={"sent": ["inapp"], "errors": [], "blocked": []}).start()
        self.send_sms = patch.object(nt, "send_sms").start()
        self.send_voice = patch.object(nt, "send_voice_call").start()

    def tearDown(self):
        self.send_voice.stop()
        self.send_sms.stop()
        self.notify_all.stop()
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def add_case(self, channel="sms"):
        alert = AlertRecord(
            account_id=1, module="alert", rule_id=1, rule_name="CPU", source="metric",
            target="i-mock", target_name="mock-host", level="critical",
            title="CPU high", content="mock alert", status="firing",
            fired_at=datetime.now() - timedelta(hours=1),
        )
        contact = OncallContact(
            name="Mock Contact", channel=channel,
            contact="13800000000" if channel in ("sms", "phone") else "",
            level=1, enabled=True,
        )
        self.db.add_all([alert, contact])
        self.db.commit()
        return alert

    def test_sms_failure_is_visible_retried_then_success_stops_retry(self):
        self.add_case("sms")
        self.send_sms.side_effect = [
            {"ok": False, "detail": "isv.BUSINESS_LIMIT_CONTROL"},
            {"ok": True, "detail": "Code=OK"},
        ]

        first = oc.escalate_due(self.db)
        self.send_sms.assert_called_once()
        self.notify_all.assert_called_once()
        self.assertEqual(first["escalated"], [])
        self.assertEqual(len(first["failed"]), 1)
        self.assertIn("isv.BUSINESS_LIMIT_CONTROL", first["failed"][0]["detail"])
        api_first = list_escalations(db=self.db, user=None)
        self.assertEqual(len(api_first), 1)
        self.assertFalse(api_first[0]["ok"])
        self.assertIn("isv.BUSINESS_LIMIT_CONTROL", api_first[0]["detail"])

        second = oc.escalate_due(self.db)
        self.assertEqual(self.send_sms.call_count, 2, "失败级别必须在下一轮重试")
        self.assertEqual(len(second["escalated"]), 1)
        self.assertEqual(second["failed"], [])

        third = oc.escalate_due(self.db)
        self.assertEqual(self.send_sms.call_count, 2, "成功后不得重复发送")
        self.assertEqual(self.notify_all.call_count, 2)
        self.assertEqual(third, {"escalated": [], "failed": []})
        rows = self.db.query(AlertEscalation).order_by(AlertEscalation.id).all()
        self.assertEqual(len(rows), 2, "一次失败尝试 + 一次成功完成")
        api_rows = list_escalations(db=self.db, user=None)
        self.assertEqual([r["ok"] for r in api_rows], [True, False])

    def test_voice_exception_is_recorded_and_retried(self):
        self.add_case("phone")
        self.send_voice.side_effect = [
            RuntimeError("voice gateway down"),
            {"ok": True, "detail": "Code=OK"},
        ]

        first = oc.escalate_due(self.db)
        self.send_voice.assert_called_once()
        self.assertEqual(first["escalated"], [])
        self.assertIn("voice gateway down", first["failed"][0]["detail"])
        row = list_escalations(db=self.db, user=None)[0]
        self.assertFalse(row["ok"])
        self.assertIn("voice gateway down", row["detail"])

        second = oc.escalate_due(self.db)
        self.assertEqual(self.send_voice.call_count, 2)
        self.assertEqual(len(second["escalated"]), 1)
        oc.escalate_due(self.db)
        self.assertEqual(self.send_voice.call_count, 2)

    def test_inapp_success_with_group_error_completes_without_retry(self):
        self.add_case("wecom")
        self.notify_all.return_value = {
            "sent": ["inapp"], "errors": ["wecom: timeout"], "blocked": [],
        }

        first = oc.escalate_due(self.db)
        self.assertEqual(len(first["escalated"]), 1)
        self.assertEqual(first["failed"], [])
        row = list_escalations(db=self.db, user=None)[0]
        self.assertTrue(row["ok"])
        self.assertIn("wecom: timeout", row["detail"])

        oc.escalate_due(self.db)
        self.notify_all.assert_called_once()
        self.send_sms.assert_not_called()
        self.send_voice.assert_not_called()

    def test_group_success_with_inapp_error_completes_without_retry(self):
        self.add_case("wecom")
        self.notify_all.return_value = {
            "sent": ["wecom"], "errors": ["inapp: database locked"], "blocked": [],
        }

        first = oc.escalate_due(self.db)
        self.assertEqual(len(first["escalated"]), 1)
        row = list_escalations(db=self.db, user=None)[0]
        self.assertTrue(row["ok"])
        self.assertIn("inapp: database locked", row["detail"])

        oc.escalate_due(self.db)
        self.notify_all.assert_called_once()

    def test_all_fallback_channels_failed_is_retried(self):
        self.add_case("wecom")
        self.notify_all.return_value = {
            "sent": [], "errors": ["inapp: unavailable", "wecom: timeout"], "blocked": [],
        }

        first = oc.escalate_due(self.db)
        self.assertEqual(first["escalated"], [])
        self.assertIn("wecom: timeout", first["failed"][0]["detail"])
        self.assertFalse(list_escalations(db=self.db, user=None)[0]["ok"])

        self.notify_all.return_value = {"sent": ["inapp"], "errors": [], "blocked": []}
        second = oc.escalate_due(self.db)
        self.assertEqual(len(second["escalated"]), 1)
        self.assertEqual(self.notify_all.call_count, 2)
        oc.escalate_due(self.db)
        self.assertEqual(self.notify_all.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
