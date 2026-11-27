#!/usr/bin/env python3
"""账单断供隔离 mock 测试：成本停更守卫 + 就绪度停更暴露。

运行：cd /opt/qianshu/backend && python3 tests/test_dashboard_cost_stale_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实库、
零网络（notify_inapp 只写站内信表，不碰任何机器人渠道）。

背景：真实凭证只配置在手工进程 env 时，切换 systemd 等运行方式后可能丢失；
  账单采集软跳过 0 行会导致 cost_daily 停更，总览「昨日按量」显示 '-'（不是接口坏了，是上游断供）。
  旧就绪度只判"全空"，历史非空但停更仍全绿——本测试锁定这个盲区。

验证项：
  A 就绪度：cost_daily 空 → "成本数据未采集"；停更 ≥2 天 → "成本数据停更 N 天"；
    新鲜（昨 日有行）→ 不误报停更
  B stats 字段：cost_last_date / cost_stale_days 暴露（'-' 的"为什么"可读）
  C 守卫 guard_bill_staleness：无 bill 史不打扰（纯腾讯部署）；停更 <2 天不打扰；
    停更 ≥2 天发站内信（admins 口径）；同日同题幂等只发一封；自行 commit
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-cost-stale-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("stale")
os.environ["MASTER_KEY"] = "cd" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.cost_daily import CostDaily  # noqa: E402
from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import cost_job  # noqa: E402
from app.services import dashboard  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(username="admin1", password_hash="x", role="admin", is_active=True))
db.commit()

today = date.today()


def _bill(d: date, amount: float = 10.0) -> CostDaily:
    return CostDaily(stat_date=d, source="bill", subscription_type="PayAsYouGo",
                     pretax_amount=amount, product_code="ECS", product_name="云服务器",
                     account_id=1)


def _reasons():
    return dashboard.summary(db).get("not_ready_reasons", [])


# ---- A1：全空 → "未采集"（旧逻辑）----
r = _reasons()
check("A1 全空报「成本数据未采集」", any("未采集" in x for x in r), str(r))

# ---- A2/C1：有 30 天前的 bill 史、停更 30 天 → 报「停更」+ 守卫发信 ----
day0 = today - timedelta(days=30)
db.add(_bill(day0))
db.commit()
r = _reasons()
check("A2 停更 ≥2 天报「成本数据停更」", any("停更" in x for x in r), str(r))
check("A2 停更原因带最后账单日", any(day0.isoformat() in x for x in r), str(r))

g = cost_job.guard_bill_staleness(db)
check("C1 停更 30 天 → 守卫发信", g.get("notified", 0) >= 1, str(g))
n_today = db.query(InAppMessage).filter(
    InAppMessage.title.like("账单采集断供%")).count()
check("C1 站内信落库（admins 口径）", n_today == 1, f"count={n_today}")

g2 = cost_job.guard_bill_staleness(db)
n_after = db.query(InAppMessage).filter(
    InAppMessage.title.like("账单采集断供%")).count()
check("C2 同日同题幂等只发一封", g2.get("notified") == 0 and n_after == 1,
      f"2nd={g2}, count={n_after}")

# ---- A3/C3：补到新鲜（昨日有行）→ 不误报、守卫不打扰 ----
db.add(_bill(today - timedelta(days=1), 88.0))
db.add(_bill(today - timedelta(days=2), 80.0))
db.commit()
r = _reasons()
check("A3 新鲜数据不报停更", not any("停更" in x for x in r), str(r))

s = dashboard.summary(db)
check("B1 stats.cost_last_date 暴露", s["stats"].get("cost_last_date") == (today - timedelta(days=1)).isoformat(),
      str(s["stats"].get("cost_last_date")))
check("B2 stats.cost_stale_days=1（<2 不算停更）", s["stats"].get("cost_stale_days") == 1,
      str(s["stats"].get("cost_stale_days")))
check("B3 昨日按量出真实金额", s["stats"].get("payg_yesterday") == 88.0,
      str(s["stats"].get("payg_yesterday")))
check("B4 环比出真实值", s["stats"].get("payg_change_pct") == 10.0,
      str(s["stats"].get("payg_change_pct")))

g3 = cost_job.guard_bill_staleness(db)
check("C3 新鲜数据守卫不打扰", g3.get("notified") == 0 and "新鲜" in g3.get("reason", ""),
      str(g3))

# ---- C4：纯腾讯部署（只有 tc_bill 史）停更也提醒（同属账单断供）----
db.query(CostDaily).delete()
db.add(CostDaily(stat_date=today - timedelta(days=9), source="tc_bill",
                 subscription_type="PayAsYouGo", pretax_amount=5.0,
                 product_code="CDN", product_name="CDN", account_id=2))
db.commit()
r = _reasons()
check("A4 tc_bill 停更同样报「停更」", any("停更" in x for x in r), str(r))
g4 = cost_job.guard_bill_staleness(db)
check("C4 tc_bill 停更守卫发信", g4.get("notified", 0) >= 1, str(g4))

db.close()
print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
