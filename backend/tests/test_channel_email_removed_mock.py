#!/usr/bin/env python3
"""邮件渠道下线后的负向契约。

产品口径：内部运维产品不做多用户管理，收件人绑定用户的设计全部 pass，邮件渠道也不要。
本测试锁定：能力已下线，但历史 email 行保留、可删除、不静默丢数据。

隔离：一次性 PostgreSQL + 假 MASTER_KEY；全程不建真实 SMTP 连接。
运行：cd /opt/qianshu/backend && python3 tests/test_channel_email_removed_mock.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-mailgone-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("mailgone")
os.environ["MASTER_KEY"] = "cd" * 32

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import app.main  # noqa: E402,F401  导入全量模型
from app.api.v1 import alerts as alerts_api  # noqa: E402
from app.api.v1 import integrations as integrations_api  # noqa: E402
from app.api.v1.oncall import ContactIn  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.alert_channel import AlertChannel  # noqa: E402
from app.schemas.alerts import CHANNEL_TYPES, ChannelCreate, ChannelUpdate  # noqa: E402
from app.services.notify import NotifyError, notify_all_channels, send_to_channel  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class _FakeReq:
    def __init__(self):
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.headers = {}


class _FakeUser:
    id = 1
    username = "smoke"


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        check("渠道类型只允许三种", CHANNEL_TYPES == ("wecom_bot", "dingtalk_bot", "webhook"), CHANNEL_TYPES)
        for cls in (ChannelCreate, ChannelUpdate):
            try:
                cls(name="mail", type="email")
                ok = False
            except ValidationError:
                ok = True
            check(f"{cls.__name__} 拒绝 email", ok)
        try:
            ContactIn(name="值班", channel="email")
            ok = False
        except ValidationError:
            ok = True
        check("值班联系人拒绝 email", ok)

        legacy = AlertChannel(name="历史邮件", type="email", enabled=True,
                              webhook_url_cipher="", at_mobiles="[]")
        webhook = AlertChannel(name="群机器人", type="webhook", enabled=False,
                               webhook_url_cipher="", at_mobiles="[]")
        db.add_all([legacy, webhook])
        db.commit()

        rows = alerts_api.list_channels(db, None)
        check("列表隐藏历史 email 行", [r["name"] for r in rows] == ["群机器人"], rows)
        try:
            send_to_channel(legacy, "t", "c")
            ok = False
        except NotifyError as e:
            ok = "已下线" in str(e)
        check("send_to_channel 明确拒绝 email", ok)

        r = notify_all_channels(db, "t", "c")
        check("统一出口不把 email 当发送目标",
              all(not str(x).startswith("历史邮件") for x in r["sent"]), r)

        try:
            integrations_api.integrations_check_channel(legacy.id, _FakeReq(), db, _FakeUser())
            integ_hidden = False
        except HTTPException as e:
            integ_hidden = e.status_code == 404
        check("集成自检也隐藏历史 email 行", integ_hidden)

        alerts_api.delete_channel(legacy.id, _FakeReq(), db, _FakeUser())
        check("历史 email 行仍可删除清理", db.get(AlertChannel, legacy.id) is None)
    finally:
        db.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
