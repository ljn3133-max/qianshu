#!/usr/bin/env python3
"""采集节奏接线回归测试。

验证「按数据源优先级跳过未到期账号」真的接进了共享入口，而不只是策略函数
自己有单测：

- 云资产同步：low 当天已同步 → 定时跳过、手动 force 照常同步；medium 12 小时；
- 告警评估：medium 账号 5 分钟内已评估 → 后台轮跳过且不推进游标；
  到期/force 时评估并推进游标。

全部一次性 PostgreSQL + 假 provider，不访问云 API。

运行：cd /opt/qianshu/backend && python3 tests/test_cadence_wiring_mock.py
"""
import importlib
import os
import pkgutil
import sys
import tempfile
from datetime import datetime, timedelta

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
from app.models.alert_rule import AlertRule  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.services import monitor_policy as mp  # noqa: E402
from app.services import alert_eval  # noqa: E402
from app.services.cloud import sync_job  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="qs-cadence-"), "t.db")
    engine = create_engine(test_database_url("cadence"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    mp.ensure_defaults(db)
    return db


def account(db, name, priority, **kw):
    acc = CloudAccount(name=name, vendor="aliyun", ak_cipher="x", secret_cipher="y",
                       region="cn-hangzhou", extra_json="{}", enabled=True,
                       status="ok", priority=priority, **kw)
    db.add(acc)
    db.commit()
    return acc


class FakeProvider:
    def __init__(self):
        self.calls = []

    def sync(self, db, acc, products=None):
        self.calls.append(acc.id)
        return [{"product": "ecs", "ok": True, "total": 1, "created": 1,
                 "updated": 0, "missing_marked": 0, "failed_regions": []}]


def main():
    db = make_db()
    real_provider = sync_job.get_provider
    fake = FakeProvider()
    sync_job.get_provider = lambda vendor: fake
    try:
        now = datetime.now()
        low = account(db, "vcenter", "low", last_sync_at=now)
        med_due = account(db, "staging", "medium", last_sync_at=now - timedelta(hours=13))
        med_fresh = account(db, "dev", "medium", last_sync_at=now - timedelta(hours=11))

        # ---- 1) 定时同步按节奏跳过 ----
        out = sync_job.sync_accounts(db)
        check("定时同步只跑到期账号", fake.calls == [med_due.id], str(fake.calls))
        check("跳过数如实上报", out["summary"]["skipped_accounts"] == 2, str(out["summary"]))

        # ---- 2) 手动同步 force 不受限 ----
        fake.calls.clear()
        sync_job.sync_accounts(db, account_ids=[low.id], force=True)
        check("手动/接入同步 force 照常执行", fake.calls == [low.id], str(fake.calls))
    finally:
        sync_job.get_provider = real_provider

    # ---- 3) 告警评估按节奏跳过（medium 5 分钟） ----
    m = account(db, "staging-eval", "medium")
    db.add(Asset(account_id=m.id, product="ecs", instance_id="i-1",
                 name="机器1", status="Running"))
    rule = AlertRule(name="CPU 高", source="metric", product="ecs", metric="cpu",
                     comparator=">", threshold=80, level="warning", enabled=True,
                     channel_ids="[]", silences="[]")
    db.add(rule)
    db.commit()

    real_eval = alert_eval._eval_metric_rule
    alert_eval._eval_metric_rule = lambda r: {"i-1": (False, 10.0, "")}
    try:
        m.last_alert_eval_at = datetime.now()
        db.commit()
        cursor = m.last_alert_eval_at
        alert_eval.run_evaluation(db)
        db.refresh(m)
        check("未到期账号不评估也不推进游标",
              m.last_alert_eval_at == cursor, str(m.last_alert_eval_at))

        m.last_alert_eval_at = datetime.now() - timedelta(seconds=301)
        db.commit()
        alert_eval.run_evaluation(db)
        db.refresh(m)
        check("到期账号评估并推进游标", m.last_alert_eval_at > cursor,
              str(m.last_alert_eval_at))
        check("未越限不产生告警记录", db.query(AlertRecord).count() == 0)

        m.last_alert_eval_at = datetime.now()
        db.commit()
        alert_eval.run_evaluation(db, force=True)
        db.refresh(m)
        check("手动评估 force 绕过节奏", m.last_alert_eval_at > cursor,
              str(m.last_alert_eval_at))
    finally:
        alert_eval._eval_metric_rule = real_eval

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("失败：" + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
