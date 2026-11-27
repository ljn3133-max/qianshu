#!/usr/bin/env python3
"""确认/忽略/重申状态机回归测试（分级提醒）。

覆盖：
- 重申时间按数据源优先级：low 30 天 / medium 7 天 / high 不重申；
  账号 repeat_days 覆盖，0=永不；
- 确认后不再进总览；到期后 reminder_job 重置并只发一条聚合提醒；
- 恢复/级别升级的静默清理由 alert_eval 负责（单测覆盖状态重置函数）；
- 到期忽略 until_at 过期自动失效。

一次性 PostgreSQL + monkeypatch 发送函数，绝不碰真实渠道。

运行：cd /opt/qianshu/backend && python3 tests/test_reminder_state_mock.py
"""
import importlib
import os
import pkgutil
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.core.db import Base  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.cost_anomaly import CostAnomaly  # noqa: E402
from app.models.expiry_ignore import ExpiryIgnore  # noqa: E402
from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import alert_ack  # noqa: E402
from app.services import monitor_policy as mp  # noqa: E402
from app.services import notify as nt  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="qs-remind-"), "t.db")
    engine = create_engine(test_database_url("reminder"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    mp.ensure_defaults(db)
    db.add(User(username="admin", password_hash="x", role="admin", is_active=True))
    db.commit()
    return db


def account(db, name, priority="high", repeat=None):
    acc = CloudAccount(name=name, vendor="aliyun", ak_cipher="x", secret_cipher="y",
                       region="cn-hangzhou", extra_json="{}", enabled=True,
                       status="ok", priority=priority, repeat_days=repeat)
    db.add(acc)
    db.commit()
    return acc.id


def alert(db, account_id, level="warning", title="CPU 高", **kw):
    r = AlertRecord(account_id=account_id, module="alert", rule_id=1,
                    rule_name="CPU", source="metric", target="i-1",
                    target_name="机器1", level=level, title=title, content="c",
                    status="firing", **kw)
    db.add(r)
    db.commit()
    return r


def main():
    db = make_db()
    user = db.query(User).first()
    high = account(db, "prod", "high")
    med = account(db, "staging", "medium")
    low = account(db, "vcenter", "low")
    never = account(db, "archive", "low", repeat=0)
    ninety = account(db, "legacy", "low", repeat=90)

    now = datetime.now()
    # ---- 1) 重申时间按优先级/覆盖计算 ----
    check("high 默认不重申", alert_ack.compute_next_notify(db, high, now) is None)
    d_med = alert_ack.compute_next_notify(db, med, now)
    check("medium 7 天重申", d_med and (d_med - now).days == 7, str(d_med))
    d_low = alert_ack.compute_next_notify(db, low, now)
    check("low 30 天重申", d_low and (d_low - now).days == 30, str(d_low))
    check("repeat_days=0 永不重申",
          alert_ack.compute_next_notify(db, never, now) is None)
    d_90 = alert_ack.compute_next_notify(db, ninety, now)
    check("repeat_days=90 覆盖默认", d_90 and (d_90 - now).days == 90, str(d_90))
    check("无账号上下文不重申",
          alert_ack.compute_next_notify(db, 0, now) is None)

    # ---- 2) 确认后字段与总览过滤 ----
    r = alert(db, low, level="critical")
    payload = alert_ack.apply_ack(db, r, "acknowledge", "已处理", user, now)
    db.commit()
    check("确认后 ack_status", payload["ack_status"] == "acknowledged",
          payload["ack_status"])
    check("确认写 next_notify_at", bool(payload["next_notify_at"]),
          payload["next_notify_at"])
    from app.services.dashboard import summary
    s = summary(db)
    check("已确认告警不进总览",
          not [t for t in s["todos"] if t["cat"] == "告警"],
          str([t["title"] for t in s["todos"]]))

    # ---- 3) 到期重申：重置 + 只发一条聚合消息 ----
    calls = []
    real = nt.notify_all_channels

    def fake_all(db_, title, content, level="critical", at_mobiles=None,
                 module=None, account_id=None):
        calls.append({"module": module, "account_id": account_id,
                      "title": title, "content": content})
        return {"sent": ["inapp"], "errors": [], "blocked": []}

    nt.notify_all_channels = fake_all
    try:
        r2 = alert(db, med, level="warning", title="磁盘高")
        alert_ack.apply_ack(db, r2, "ignore", "", user, now)
        db.commit()
        r2.next_notify_at = now - timedelta(minutes=1)
        db.commit()
        from app.services.reminder_job import run_reminders
        out = run_reminders(db, now)
    finally:
        nt.notify_all_channels = real

    check("到期才重申（1 条告警）", out["alerts"] == 1, str(out))
    check("聚合只发一条", len(calls) == 1, str(len(calls)))
    check("重申消息按模块/账号", calls and calls[0]["module"] == "alert"
          and calls[0]["account_id"] == med, str(calls))
    db.refresh(r2)
    check("重申后回到未处理", r2.ack_status == "none" and r2.next_notify_at is None,
          f"{r2.ack_status}/{r2.next_notify_at}")
    check("重申后计数重新为 1", r2.notify_count == 1, str(r2.notify_count))

    # 已重置的不会再被扫到
    out2 = run_reminders(db, now)
    check("未到期不重复扫", out2["alerts"] == 0, str(out2))

    # ---- 4) repeat_days=0：确认后再久也不重申 ----
    r3 = alert(db, never, title="辅助机")
    alert_ack.apply_ack(db, r3, "ignore", "", user, now)
    db.commit()
    r3.next_notify_at = now - timedelta(days=999)
    db.commit()
    run_reminders(db, now + timedelta(days=1000))
    db.refresh(r3)
    check("永不重申账号保持静默不改状态",
          r3.ack_status == "ignored" and r3.next_notify_at is None,
          f"{r3.ack_status}/{r3.next_notify_at}")

    # ---- 5) 低优先级确认后不再进总览，重申后重新出现 ----
    r4 = alert(db, low, level="critical", title="vCenter 告警")
    alert_ack.apply_ack(db, r4, "acknowledge", "", user, now)
    db.commit()
    s2 = summary(db)
    check("vCenter 确认后不进总览",
          not [t for t in s2["todos"] if "vCenter" in t["title"]],
          str([t["title"] for t in s2["todos"]]))

    nt.notify_all_channels = lambda *a, **k: {"sent": ["inapp"], "errors": [],
                                              "blocked": []}
    try:
        r4.next_notify_at = now - timedelta(minutes=1)
        db.commit()
        run_reminders(db, now)
    finally:
        nt.notify_all_channels = real
    s3 = summary(db)
    check("重申后 vCenter 重新进总览",
          [t for t in s3["todos"] if "vCenter" in t["title"]],
          str([t["title"] for t in s3["todos"]]))

    # ---- 6) 取消静默 ----
    alert_ack.clear_ack(r4)
    db.commit()
    check("取消静默字段清零", r4.ack_status == "none" and r4.next_notify_at is None)

    # ---- 7) 异动重复提醒（按类型归模块、账号分组） ----
    anom = CostAnomaly(detect_date=date.today(), last_seen=date.today(),
                       type="payg_spike", level="warning", target_key="i-9",
                       target_name="机器9", title="按量突增", content="c",
                       value=10, baseline=1, ratio=10, account_id=low,
                       ack_status="ignored", notify_count=1,
                       next_notify_at=now - timedelta(minutes=1))
    db.add(anom)
    db.commit()
    calls.clear()
    nt.notify_all_channels = fake_all
    try:
        out4 = run_reminders(db, now)
    finally:
        nt.notify_all_channels = real
    check("异动到期重申", out4["anomalies"] == 1 and len(calls) == 1, str(out4))
    check("异动重申归成本模块", calls and calls[0]["module"] == "cost", str(calls))
    db.refresh(anom)
    check("异动重申后回到 open",
          anom.ack_status == "open" and anom.notify_count == 1,
          f"{anom.ack_status}/{anom.notify_count}")

    # ---- 8) 到期忽略 until_at ----
    db.add_all([
        ExpiryIgnore(target_key="ssl_cert:old", note="", until_at=now - timedelta(days=1)),
        ExpiryIgnore(target_key="ssl_cert:new", note="", until_at=now + timedelta(days=3)),
        ExpiryIgnore(target_key="domain:forever", note="", until_at=None),
    ])
    db.commit()
    from app.services.expiry import _ignore_set
    ig = _ignore_set(db)
    check("限时忽略过期自动失效", "ssl_cert:old" not in ig, str(ig))
    check("限时忽略未到期仍生效", "ssl_cert:new" in ig, str(ig))
    check("永久忽略保持", "domain:forever" in ig, str(ig))

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("失败：" + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
