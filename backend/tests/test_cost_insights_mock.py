#!/usr/bin/env python3
"""COST-INSIGHTS C1 隔离内容级回归；只读一次性 PostgreSQL，不访问真实云。"""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-cost-insights-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("insights")
os.environ["MASTER_KEY"] = "ce" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.cost_anomaly import CostAnomaly  # noqa: E402
from app.models.cost_daily import CostDaily  # noqa: E402
from app.models.onboarding_run import OnboardingRun  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import onboarding  # noqa: E402
from app.services.cost_insights import get_cost_insights  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


Base.metadata.create_all(engine)
db = SessionLocal()
TODAY = date.today()
DAY = TODAY - timedelta(days=1)
START = DAY.isoformat()


def account(aid, name=None, vendor="aliyun"):
    row = CloudAccount(id=aid, name=name or f"账号{aid}", vendor=vendor, enabled=True, status="ok")
    db.add(row)
    db.commit()
    return row


def bill(aid, day, amount, *, source="bill", sub="PayAsYouGo", product="ecs",
         name="云服务器", instance="i-1", instance_name="服务器一", item="cpu", usage=1.0):
    row = CostDaily(
        account_id=aid, stat_date=day, source=source, subscription_type=sub,
        product_code=product, product_name=name,
        instance_id=instance if aid == 1 or instance != "i-1" else f"i-{aid}",
        instance_name=instance_name, billing_item=item, pretax_amount=amount,
        pretax_gross_amount=amount, payment_amount=0, usage=usage,
        created_at=datetime(2026, 11, 18, 1, 2, 3),
    )
    db.add(row)
    db.commit()
    return row


def one(result, aid):
    return next(x for x in result["accounts"] if x["account"]["id"] == aid)


def daily_success(acc, day, *, amortized_complete=True):
    run = db.query(OnboardingRun).filter(OnboardingRun.account_id == acc.id).first()
    if run is None:
        run = OnboardingRun(account_id=acc.id, status="attention", items_json="[]")
        db.add(run)
        db.commit()
    onboarding.record_capability(
        db, acc, "cost", scope={"bill_date": day.isoformat()},
        execution="succeeded", data_state="no_data",
        coverage={"discovered": 1, "checked": 1, "failed": 0, "unknown": 0,
                  "total_known": True, "truncated": not amortized_complete},
        checked_at=datetime(2026, 11, 18, 2, 3, 4),
        observed_until=datetime.combine(day, datetime.max.time()),
        evidence_refs=[f"cost_daily:{acc.id}:{day.isoformat()}"],
    )
    db.commit()


print("== C1a 成本、覆盖、排行与账号隔离 ==")
a1, a2 = account(1, "生产"), account(2, "测试")
r100 = bill(1, DAY, 100)
bill(1, DAY, 20, source="amortized", sub="Subscription", item="amortized")
bill(1, DAY, 500, sub="Subscription", instance="", item="order")
bill(1, DAY, -50, sub="Subscription", instance="", item="refund")
bill(2, DAY, 999)
bill(999, DAY, 777)
bill(0, DAY, 888)
result = get_cost_insights(db, account_id=1, start=DAY, end=DAY)
view = one(result, 1)
check("A1 日成本=按量100+摊销20=120，不含订单500", view["totals"]["total"] == 120, str(view["totals"]))
check("A2 订单与退款另列且退款保持负值", view["totals"]["order"] == 500 and view["totals"]["refund"] == -50)
check("A3 payment_amount=0 不改变账单金额", view["products"][0]["total"] == 120)
check("A4 产品占比可回溯原始明细", view["products"][0]["share_pct"] == 100
      and view["products"][0]["object"]["id"] == "ecs"
      and view["instances"][0]["object"]["id"] == "i-1"
      and f"cost_daily:1:{DAY.isoformat()}" in view["products"][0]["evidence_refs"])
check("A5 完整单日带账单日和采集时间", view["coverage"]["complete"] is True
      and view["coverage"]["observed_start"] == START and view["coverage"]["collected_at"])
check("A6 账号B不计入账号A", view["totals"]["payg"] == 100 and one(get_cost_insights(db, 2, DAY, DAY), 2)["totals"]["payg"] == 999)
all_view = get_cost_insights(db, 0, DAY, DAY)
check("A7 全部账号分账号呈现", {x["account"]["id"] for x in all_view["accounts"]} == {1, 2})
check("A8 未归属数据单列且不计入任何账号", {x["account"]["id"] for x in all_view["unattributed"]} == {0, 999}
      and sum(x["totals"]["payg"] or 0 for x in all_view["accounts"]) == 1099)

r100.pretax_amount = 130
db.commit()
check("A9 同日修订复算不重复计费", one(get_cost_insights(db, 1, DAY, DAY), 1)["totals"]["total"] == 150)

# 排行可补全展示名称，但只读服务不得修改待持久化的 ORM 账单对象。
a6 = account(6, "名称补全")
unnamed = bill(6, DAY, 8, name="", instance="i-unnamed")
bill(6, DAY, 2, name="补全名称", instance="i-named")
named_view = one(get_cost_insights(db, 6, DAY, DAY), 6)
check("A10 排行补名称不修改原始 ORM 行", named_view["products"][0]["object"]["product_name"] == "补全名称"
      and not db.is_modified(unnamed))

print("== C1a 缺摊销、无数据与真实零费用 ==")
a3 = account(3, "缺摊销")
bill(3, DAY, 40)
partial = one(get_cost_insights(db, 3, DAY, DAY), 3)
check("B1 缺摊销明确部分覆盖", partial["status"] == "partial" and not partial["coverage"]["complete"]
      and partial["reason_code"] == "amortized_incomplete")
check("B2 缺摊销不补零，已知总额仅为已获取部分", partial["totals"]["total"] == 40
      and partial["totals"]["amortized"] is None and not partial["totals"]["complete"])

a4 = account(4, "无账单")
empty = one(get_cost_insights(db, 4, DAY, DAY), 4)
check("B3 无账单不是零费用或健康", empty["status"] == "no_data" and empty["totals"]["total"] is None
      and empty["days"][0]["payg"] is None and empty["recommendations"] == [])

a5 = account(5, "真实零费用")
daily_success(a5, DAY)
zero = one(get_cost_insights(db, 5, DAY, DAY), 5)
check("B4 有逐日成功和完整范围证据才允许真实零费用", zero["status"] == "available"
      and zero["totals"]["total"] == 0 and zero["days"][0]["payg"] == 0 and zero["coverage"]["complete"])

two_days = one(get_cost_insights(db, 1, DAY - timedelta(days=1), DAY), 1)
check("B5 历史不足保留请求区间和缺失日期", two_days["range"]["start"] == (DAY - timedelta(days=1)).isoformat()
      and two_days["coverage"]["missing_bill_dates"] == [(DAY - timedelta(days=1)).isoformat()]
      and not two_days["coverage"]["complete"])

print("== C1a 只读 API 契约 ==")
db.add_all([
    User(id=101, username="viewer-insights", password_hash="x", role="viewer", is_active=True),
    User(id=102, username="admin-insights", password_hash="x", role="admin", is_active=True),
])
db.commit()
# 本机 TestClient 与 httpx 0.28 不兼容；复用项目既有 ASGI 同步壳。
def _get(url, **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.get(url, **kw)
    return asyncio.run(_go())


client = SimpleNamespace(get=_get)
from app.core.deps import get_current_user
app.dependency_overrides[get_current_user] = lambda: db.get(User, 101)
with patch("app.services.cost_job.run_collection", side_effect=AssertionError("GET 不得触发云采集")):
    resp = client.get(f"/api/cost/insights?account_id=1&start={START}&end={START}")
check("C1 viewer 可读取且响应模型有效", resp.status_code == 200 and resp.json()["accounts"][0]["totals"]["total"] == 150,
      resp.text[:300])
check("C2 GET 不触发云同步", resp.status_code == 200)
check("C3 逆序日期返回400", client.get(f"/api/cost/insights?account_id=1&start={START}&end={DAY - timedelta(days=1)}").status_code == 400)
check("C4 不存在账号返回404，不回退首账号", client.get(f"/api/cost/insights?account_id=404&start={START}&end={START}").status_code == 404)
default_window = client.get("/api/cost/insights?account_id=1")
check("C5 默认最近30天且截至昨日", default_window.status_code == 200
      and default_window.json()["requested"] == {
          "account_id": 1, "start": (DAY - timedelta(days=29)).isoformat(), "end": START,
      })

print("== C1b 冷启动、首次观察、周期异常与按量候选 ==")
# 账号 10：28 个已采集历史日 + 当前突增；只从本账号原始账单重算。
a10 = account(10, "突增账号")
for offset in range(28, 0, -1):
    bill(10, DAY - timedelta(days=offset), 10, instance="i-spike", item="traffic")
bill(10, DAY, 100, instance="i-spike", item="traffic")
# 同窗口另一账号的同产品/计费项巨额数据不得进入账号10基线或当前值。
bill(11, DAY - timedelta(days=28), 10000, instance="i-other", item="traffic")
account(11, "其他账号")
spike = one(get_cost_insights(db, 10, DAY, DAY), 10)
check("D1 有充分同账号基线才确认突增", len(spike["anomalies"]) == 1
      and spike["anomalies"][0]["value"] == 100 and spike["anomalies"][0]["baseline"] == 10,
      str(spike["anomalies"]))
check("D2 异常证据定位账号、对象、日期和原始账单", spike["anomalies"][0]["account_id"] == 10
      and spike["anomalies"][0]["object"]["id"] == "ecs|i-spike|traffic"
      and spike["anomalies"][0]["history_range"] == {
          "from": (DAY - timedelta(days=28)).isoformat(), "to": (DAY - timedelta(days=1)).isoformat(),
      }
      and f"cost_daily:10:{(DAY - timedelta(days=28)).isoformat()}" in spike["anomalies"][0]["evidence_refs"])
check("D3 异常建议无节省金额且不是采购动作", spike["recommendations"][0]["action"] == "investigate_cost_growth"
      and spike["recommendations"][0]["estimated_savings"] is None
      and "pack" not in spike["recommendations"][0]["action"])
check("D4 只读洞察不落旧异常生命周期", db.query(CostAnomaly).count() == 0)
spike_api = client.get(f"/api/cost/insights?account_id=10&start={START}&end={START}")
check("D5 异常及历史证据通过只读响应模型", spike_api.status_code == 200
      and spike_api.json()["accounts"][0]["anomalies"][0]["baseline"] == 10
      and spike_api.json()["accounts"][0]["recommendations"][0]["history_range"]["to"]
      == (DAY - timedelta(days=1)).isoformat(), spike_api.text[:300])

# 账号 12：冷启动当前有费用，只能首次观察，不得宣称异常或刚开通。
a12 = account(12, "冷启动")
bill(12, DAY, 30, instance="i-new", item="new-item")
cold = one(get_cost_insights(db, 12, DAY, DAY), 12)
check("E1 冷启动不制造异常", cold["anomalies"] == [])
check("E2 历史不足降级为首次观察", len(cold["observations"]) == 1
      and cold["observations"][0]["kind"] == "first_observed"
      and not cold["observations"][0]["history_verified"])
check("E3 首次观察只建议核实，不断言刚开通", cold["recommendations"][0]["action"] == "verify_first_observed_cost"
      and cold["recommendations"][0]["estimated_savings"] is None)

# 账号 13：28 天完整历史均没有该键，当前首次出现可证明历史覆盖，但仍不猜开通原因。
a13 = account(13, "历史完整")
for offset in range(28, 0, -1):
    bill(13, DAY - timedelta(days=offset), 5, instance="i-base", item="base")
bill(13, DAY, 30, instance="i-new-13", item="new-item")
first = one(get_cost_insights(db, 13, DAY, DAY), 13)
check("E4 首次观察带已核验历史覆盖", first["observations"][0]["history_verified"] is True
      and first["observations"][0]["history_days"] >= 7
      and first["observations"][0]["history_range"]["from"] == (DAY - timedelta(days=28)).isoformat()
      and len(first["observations"][0]["evidence_refs"]) == 29
      and first["anomalies"] == [])

# 账号 14：历史零基线，当前同一键由零到有只进入首次观察，不强行判 spike。
a14 = account(14, "零基线")
for offset in range(28, 0, -1):
    bill(14, DAY - timedelta(days=offset), 0, instance="i-zero", item="zero")
bill(14, DAY, 30, instance="i-zero", item="zero")
zero_base = one(get_cost_insights(db, 14, DAY, DAY), 14)
check("E5 零基线不强行判异常", zero_base["anomalies"] == []
      and zero_base["observations"][0]["kind"] == "first_observed")

# 账号 15：连续高额但稳定，给评估计费方式候选，不给购买/SKU/节省金额。
a15 = account(15, "稳定高额")
for offset in range(6, -1, -1):
    bill(15, DAY - timedelta(days=offset), 30, instance="i-high", item="high")
high = one(get_cost_insights(db, 15, DAY - timedelta(days=6), DAY), 15)
check("F1 持续高额按量形成有限候选", any(x["action"] == "evaluate_sustained_payg" for x in high["recommendations"]))
check("F2 高额不等于浪费或采购建议", all(x["estimated_savings"] is None for x in high["recommendations"])
      and all(x["action"] != "purchase_resource_pack" for x in high["recommendations"]))
check("F3 建议最多三条且有稳定标识", len(high["recommendations"]) <= 3
      and all(x["id"] and x["evidence_refs"] for x in high["recommendations"]))

app.dependency_overrides.clear()
db.close()
print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
for failure in FAIL:
    print("  FAIL:", failure)
sys.exit(1 if FAIL else 0)
