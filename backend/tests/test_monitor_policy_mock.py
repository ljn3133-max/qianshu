#!/usr/bin/env python3
"""按数据源分级监控策略回归测试。

覆盖：模块开关 × 账号去向 AND 矩阵、模块出口白名单、全局模块不受账号影响、
未知模块不拦、逃生门、重复提醒继承/覆盖、高/中/低采集到期判断、游标回写、
异动类型归属。全部使用一次性 PostgreSQL 库，不碰真实数据。

运行：cd /opt/qianshu/backend && python3 tests/test_monitor_policy_mock.py
"""
import os
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

from app.core.config import settings  # noqa: E402
from app.core.db import Base  # noqa: E402
import app.models  # noqa: E402,F401  注册全部模型
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.cost_anomaly import CostAnomaly  # noqa: E402
from app.models.monitor_policy import MonitorModuleSetting  # noqa: E402
from app.services import monitor_policy as mp  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="qs-policy-"), "t.db")
    engine = create_engine(test_database_url("monpolicy"))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def add_account(db, name, priority="high", **kw):
    acc = CloudAccount(name=name, vendor="aliyun", ak_cipher="x", secret_cipher="y",
                       region="cn-hangzhou", extra_json="{}", enabled=True,
                       status="ok", priority=priority, **kw)
    db.add(acc)
    db.commit()
    return acc.id


def main():
    db = make_db()

    # ---- 种子 ----
    n = mp.ensure_defaults(db)
    check("种子补齐 12 行", n == 12, str(n))
    check("重复补齐零新增", mp.ensure_defaults(db) == 0)
    check("模块清单 12 个", len(mp.MODULES) == 12, str(len(mp.MODULES)))
    rows = {r.module for r in db.query(MonitorModuleSetting).all()}
    check("种子模块名与注册表一致", rows == set(mp.MODULES), str(rows - set(mp.MODULES)))

    # ---- 默认全开 ----
    check("默认允许总览", mp.can_overview(db, "alert", 0))
    check("默认允许站内信", mp.can_inapp(db, "alert", 0))
    check("默认允许外推", mp.can_push(db, "alert", 0))

    # ---- 模块出口白名单 ----
    check("到期提醒不支持站内信", not mp.can_inapp(db, "expiry", 0))
    check("到期提醒不支持外推", not mp.can_push(db, "expiry", 0))
    check("到期提醒支持总览", mp.can_overview(db, "expiry", 0))
    check("巡检日报不支持总览", not mp.can_overview(db, "inspection", 0))
    check("对象画像只支持站内信",
          mp.can_inapp(db, "object_profile", 0) and
          not mp.can_overview(db, "object_profile", 0) and
          not mp.can_push(db, "object_profile", 0))

    # ---- 模块关闭：三个去向全关 ----
    row = db.get(MonitorModuleSetting, "alert")
    row.enabled = False
    db.commit()
    check("模块关闭=三去向全关",
          not mp.can_overview(db, "alert", 0) and
          not mp.can_inapp(db, "alert", 0) and
          not mp.can_push(db, "alert", 0))
    row.enabled = True
    row.inapp = False
    db.commit()
    check("只关站内信不影响总览/外推",
          mp.can_overview(db, "alert", 0) and not mp.can_inapp(db, "alert", 0) and
          mp.can_push(db, "alert", 0))
    row.inapp = True
    db.commit()

    # ---- 账号级 AND：vCenter 只关自己 ----
    vid = add_account(db, "vcenter-test", priority="low", notify_overview=False,
                      notify_inapp=False, notify_push=False)
    aliyun_id = add_account(db, "生产阿里云", priority="high")
    check("账号关全部去向 → 该账号三条全拦",
          not any([mp.can_overview(db, "alert", vid),
                   mp.can_inapp(db, "alert", vid),
                   mp.can_push(db, "alert", vid)]))
    check("同模块其他账号不受影响",
          mp.can_overview(db, "alert", aliyun_id) and
          mp.can_inapp(db, "alert", aliyun_id))
    vid_acc = db.get(CloudAccount, vid)
    vid_acc.notify_overview = True
    db.commit()
    check("账号单开总览只放行总览",
          mp.can_overview(db, "alert", vid) and not mp.can_inapp(db, "alert", vid))

    # ---- 全局模块不受账号去向影响 ----
    check("全局模块无视账号开关", mp.can_inapp(db, "digest", vid))
    check("账号级模块对不存在的账号只看模块", mp.can_inapp(db, "alert", 99999))

    # ---- 未登记模块不拦（旧行为）----
    check("未知模块保持放行", mp.can_inapp(db, "no_such_module", 0))

    # ---- 逃生门 ----
    old = settings.MONITOR_POLICY_ENABLED
    settings.MONITOR_POLICY_ENABLED = False
    try:
        row2 = db.get(MonitorModuleSetting, "alert")
        row2.enabled = False
        db.commit()
        check("逃生门：模块关闭也放行",
              mp.can_overview(db, "alert", vid) and mp.can_inapp(db, "alert", vid))
        vid_acc.last_metrics_at = datetime.now()
        db.commit()
        check("逃生门：节奏回到固定间隔", mp.collect_due(db, "metrics", vid))
    finally:
        settings.MONITOR_POLICY_ENABLED = old
        row2 = db.get(MonitorModuleSetting, "alert")
        row2.enabled = True
        db.commit()

    # ---- 重复提醒继承/覆盖 ----
    mid = add_account(db, "中优先级", priority="medium")
    check("高优先级默认不重申", mp.repeat_days(db, aliyun_id) is None)
    check("中优先级默认 7 天", mp.repeat_days(db, mid) == 7)
    check("低优先级默认 30 天", mp.repeat_days(db, vid) == 30)
    acc = db.get(CloudAccount, vid)
    acc.repeat_days = 0
    db.commit()
    check("覆盖 0=永不重申", mp.repeat_days(db, vid) is None)
    acc.repeat_days = 15
    db.commit()
    check("覆盖 15 天", mp.repeat_days(db, vid) == 15)
    acc.repeat_days = -3
    db.commit()
    check("负值按永不处理", mp.repeat_days(db, vid) is None)
    acc.repeat_days = None
    db.commit()
    check("清空覆盖→回到优先级默认", mp.repeat_days(db, vid) == 30)
    check("无账号上下文按高优先级", mp.repeat_days(db, 0) is None)

    # ---- 采集节奏 ----
    now = datetime(2026, 11, 12, 3, 0, 30)
    hacc = db.get(CloudAccount, aliyun_id)
    hacc.last_metrics_at = now            # 高优先级：刚采过也照跑（现状一致）
    db.commit()
    check("高优先级不二次节流", mp.collect_due(db, "metrics", aliyun_id, now=now))

    macc = db.get(CloudAccount, mid)
    macc.last_metrics_at = now - timedelta(seconds=299)
    macc.last_host_check_at = now - timedelta(minutes=29)
    macc.last_cost_at = now - timedelta(hours=1)
    macc.last_sync_at = now - timedelta(hours=11)
    db.commit()
    check("中优先级 299s 未到期", not mp.collect_due(db, "metrics", mid, now=now))
    macc.last_metrics_at = now - timedelta(seconds=300)
    db.commit()
    check("中优先级 300s 到期", mp.collect_due(db, "metrics", mid, now=now))
    check("中优先级主机 29 分钟未到期", not mp.collect_due(db, "host", mid, now=now))
    check("中优先级成本 1 小时未到期", not mp.collect_due(db, "cost", mid, now=now))
    check("中优先级资产 11 小时未到期", not mp.collect_due(db, "assets", mid, now=now))

    lacc = db.get(CloudAccount, vid)
    lacc.last_metrics_at = datetime(2026, 11, 11, 2, 30)   # 昨天窗口内
    lacc.last_host_check_at = datetime(2026, 11, 12, 3, 20)
    db.commit()
    check("低优先级 01:00 不采（等窗口）",
          not mp.collect_due(db, "metrics", vid, now=datetime(2026, 11, 12, 1, 0)))
    check("低优先级 02:10 窗口内到期",
          mp.collect_due(db, "metrics", vid, now=datetime(2026, 11, 12, 2, 10)))
    lacc.last_metrics_at = datetime(2026, 11, 12, 2, 8)
    db.commit()
    check("低优先级窗口内 5 分钟内不重复",
          not mp.collect_due(db, "metrics", vid, now=datetime(2026, 11, 12, 2, 10)))
    check("低优先级窗口内满 5 分钟再采",
          mp.collect_due(db, "metrics", vid, now=datetime(2026, 11, 12, 2, 13)))
    lacc.last_metrics_at = datetime(2026, 11, 11, 2, 30)
    db.commit()
    check("低优先级窗口后今天没采过要补",
          mp.collect_due(db, "metrics", vid, now=datetime(2026, 11, 12, 5, 0)))
    check("低优先级主机今天已跑不再跑",
          not mp.collect_due(db, "host", vid, now=datetime(2026, 11, 12, 5, 0)))
    lacc.last_host_check_at = datetime(2026, 11, 11, 3, 25)
    db.commit()
    check("低优先级主机未到 03:20 要等到当天窗口",
          not mp.collect_due(db, "host", vid, now=datetime(2026, 11, 12, 3, 0)))
    check("低优先级主机过了 03:20 才跑",
          mp.collect_due(db, "host", vid, now=datetime(2026, 11, 12, 3, 21)))
    lacc.last_host_check_at = datetime(2026, 11, 9, 3, 25)
    db.commit()
    check("低优先级停机多日立即补跑（不等窗口）",
          mp.collect_due(db, "host", vid, now=datetime(2026, 11, 12, 1, 0)))
    check("手动触发永远到期",
          mp.collect_due(db, "host", vid, now=datetime(2026, 11, 12, 1, 0),
                         force=True) and
          mp.collect_due(db, "metrics", mid, now=now, force=True))
    check("无账号上下文立即到期", mp.collect_due(db, "metrics", 0, now=now))

    # ---- 游标回写 ----
    lacc.last_sync_at = None
    db.commit()
    mp.mark_collected(db, "assets", vid, now=now)
    db.commit()
    check("资产游标回写 last_sync_at", db.get(CloudAccount, vid).last_sync_at == now)
    mp.mark_collected(db, "metrics", 0, now=now)      # 无账号：静默不报错
    check("无账号回写不报错", True)

    # ---- 异动类型归属 ----
    def fake(t):
        return CostAnomaly(type=t)

    check("traffic_spike 归 cdn", mp.module_of_anomaly(fake("traffic_spike")) == "cdn")
    check("host_down 归 host", mp.module_of_anomaly(fake("host_down")) == "host")
    check("profile_drift 归 profile",
          mp.module_of_anomaly(fake("profile_drift")) == "profile")
    check("未知类型归 cost", mp.module_of_anomaly(fake("whatever")) == "cost")
    check("cost_spike 归 cost", mp.module_of_anomaly(fake("cost_spike")) == "cost")

    # ---- payload ----
    payload = mp.modules_payload(db)
    check("payload 12 行且带出口列表",
          len(payload) == 12 and all("destinations" in x for x in payload))
    acc_payload = mp.account_payload(db.get(CloudAccount, vid), db)
    check("账号 payload 带生效节奏与重申天数",
          acc_payload["priority"] == "low" and acc_payload["repeat_effective"] == 30 and
          bool(acc_payload["cadence"]))
    check("优先级默认值三档", set(mp.priority_defaults()) == {"high", "medium", "low"})

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("失败：" + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
