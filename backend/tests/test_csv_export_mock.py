#!/usr/bin/env python3
"""CSV 导出 mock 全链路（0.3.1）。

隔离：一次性 PostgreSQL + 假 MASTER_KEY + ASGI 直连；不碰生产库、不启动后台任务。

验证项：
  A 三个导出端点 200 + BOM + 表头 + 正确行数（绕开列表 page_size 上限）；
  B 导出与列表共用筛选口径：product/status/source/默认排除 Deleted 一致；
  C X-Total-Rows / X-Exported-Rows / Content-Disposition（中文 filename*）；
  D 2 万行截断与 # truncated 注释、公式注入防护；
  E 权限：无登录 401，viewer 可导出；
  F 审计：cost.daily.export / assets.export / alerts.records.export 落库且行数正确。

运行：cd /opt/qianshu/backend && python3 tests/test_csv_export_mock.py（退出码 0=全过）
"""
from __future__ import annotations

import asyncio
import codecs
import csv
import io
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-csv-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("csv")
os.environ["MASTER_KEY"] = "ab" * 32

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import app.core.deps as deps  # noqa: E402
import app.main  # noqa: E402,F401  导入全量模型（与 startup create_all 同口径）
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.cost_daily import CostDaily  # noqa: E402
from app.models.ownership import OwnershipRule  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _client(role="admin"):
    import app.main as mainmod

    fake = types.SimpleNamespace(id=7, username="csvsmoke", role=role, is_active=True)
    mainmod.app.dependency_overrides[deps.get_current_user] = lambda: fake

    def _req(method, url, **kw):
        async def _go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mainmod.app),
                                         base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(_go())

    return _req, mainmod.app


def _rows(resp):
    text = resp.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    req, appmod = _client("admin")
    try:
        db.add(CloudAccount(id=1, name="主账号", vendor="aliyun", enabled=True))
        # 人工归属登记：导出与列表都必须按同口径回流覆盖展示账号/业务
        db.add(OwnershipRule(key_type="instance", key_value="i-1", match_type="exact",
                             account_name="业务归属A", business="业务线A"))
        # 成本是 T+1 口径：默认区间结束于昨天，数据也放昨天
        today = date.today() - timedelta(days=1)
        # 成本：120 ecs + 5 rds，验证 product 过滤与 page_size 上限
        db.add_all([
            CostDaily(account_id=1, stat_date=today, source="bill",
                      product_code="ecs" if i < 120 else "rds",
                      product_name="ECS", instance_id=f"i-{i}",
                      instance_name=f"vm-{i}", billing_item="cpu", pretax_amount=i + 0.5,
                      usage=i, region="cn-hangzhou")
            for i in range(125)
        ])
        # 资产：200 在册 ecs + 5 Deleted；带 IP extra
        db.add_all([
            Asset(account_id=1, product="ecs", instance_id=f"i-{i}", name=f"vm-{i}",
                  region="cn-hangzhou", status="Deleted" if i >= 200 else "Running",
                  instance_type="ecs.g6", extra='{"ip_internal": "10.0.0.%d"}' % i)
            for i in range(205)
        ])
        # 告警：100 firing（60 metric + 40 sls）+ 5 recovered（sls）
        # + 2 recovered（synth/builtin，覆盖非规则来源的中文化）
        recs = []
        for i in range(105):
            if i < 60:
                src, st = "metric", "firing"
            elif i < 100:
                src, st = "sls", "firing"
            else:
                src, st = "sls", "recovered"
            recs.append(AlertRecord(
                rule_id=0, rule_name=f"规则{i}", source=src, target=f"i-{i}",
                target_name=f"vm-{i}", level="warning", title=f"告警标题{i}",
                status=st, fired_at=datetime.now(), last_eval_at=datetime.now(),
            ))
        for j, src in enumerate(("synth", "builtin")):
            recs.append(AlertRecord(
                rule_id=0, rule_name=f"非规则-{src}", source=src, target=f"x-{j}",
                target_name=f"x-{j}", level="critical", title=f"非规则告警-{src}",
                status="recovered", fired_at=datetime.now(), last_eval_at=datetime.now(),
            ))
        db.add_all(recs)
        db.commit()

        # ---- A/B/C 成本 ----
        lst = req("GET", "/api/cost/daily", params={"product": "ecs", "page_size": 100})
        check("成本列表 page_size 上限 total=120", lst.json()["total"] == 120, lst.json().get("total"))
        r = req("GET", "/api/cost/daily/export", params={"product": "ecs"})
        check("成本导出 200", r.status_code == 200, r.text[:200])
        check("成本导出 BOM", r.content.startswith(codecs.BOM_UTF8))
        check("成本导出总行数头=120", r.headers.get("x-total-rows") == "120",
              r.headers.get("x-total-rows"))
        check("成本导出导出行数头=120", r.headers.get("x-exported-rows") == "120")
        rows = _rows(r)
        check("成本导出 1 表头 + 120 数据", len(rows) == 121, len(rows))
        check("成本表头含金额与厂商",
              "金额（元）" in rows[0] and "厂商" in rows[0], rows[0])
        check("成本 product 过滤生效", all(x[3] == "ecs" for x in rows[1:]))
        check("成本账号/厂商归属", rows[1][1] == "主账号" and rows[1][2] == "阿里云", rows[1][:3])
        own_row = next(x for x in rows[1:] if x[5] == "i-1")
        check("成本导出人工归属回流", own_row[1] == "业务归属A", own_row[:3])
        disp = r.headers.get("content-disposition", "")
        check("中文文件名 RFC5987", "filename*=UTF-8''" in disp and "filename=" in disp, disp)

        # ---- A/B 资产 ----
        lst = req("GET", "/api/assets", params={"product": "ecs", "page_size": 200})
        check("资产列表默认排除 Deleted total=200", lst.json()["total"] == 200,
              lst.json().get("total"))
        r = req("GET", "/api/assets/export", params={"product": "ecs"})
        check("资产导出 200", r.status_code == 200, r.text[:200])
        check("资产导出总行数头=200", r.headers.get("x-total-rows") == "200")
        rows = _rows(r)
        check("资产导出 1 表头 + 200 数据", len(rows) == 201, len(rows))
        check("资产表头含 IP/业务归属", "内网 IP" in rows[0] and "业务归属" in rows[0])
        check("资产默认无 Deleted", all(x[6] != "Deleted" for x in rows[1:]))
        check("资产 IP 从 extra 带出", any(x[8].startswith("10.0.0.") for x in rows[1:]))
        own_row = next(x for x in rows[1:] if x[3] == "i-1")
        check("资产导出人工归属回流",
              own_row[2] == "业务归属A" and own_row[11] == "业务线A", own_row[:12])
        # 厂商过滤与列表同口径
        r_ali = req("GET", "/api/assets/export", params={"product": "ecs", "vendor": "aliyun"})
        r_tc = req("GET", "/api/assets/export", params={"product": "ecs", "vendor": "tencent"})
        check("资产厂商过滤 aliyun=200", r_ali.headers.get("x-total-rows") == "200")
        check("资产厂商过滤 tencent=0", r_tc.headers.get("x-total-rows") == "0")

        # ---- A/B 告警 ----
        lst = req("GET", "/api/alerts/records", params={"status": "firing", "page_size": 100})
        check("告警列表 firing total=100", lst.json()["total"] == 100, lst.json().get("total"))
        lst_src = req("GET", "/api/alerts/records", params={"source": "metric"})
        check("列表 source 过滤 total=60", lst_src.json()["total"] == 60,
              lst_src.json().get("total"))
        r = req("GET", "/api/alerts/records/export", params={"status": "firing"})
        check("告警导出 200", r.status_code == 200, r.text[:200])
        check("告警导出总行数头=100", r.headers.get("x-total-rows") == "100")
        rows = _rows(r)
        check("告警导出 1 表头 + 100 数据", len(rows) == 101, len(rows))
        check("告警表头含确认状态", "确认状态" in rows[0], rows[0])
        check("告警状态中文化", all(x[8] == "触发中" for x in rows[1:]))
        check("告警来源中文化", any(x[7] == "指标阈值" for x in rows[1:])
              and any(x[7] == "SLS 错误" for x in rows[1:]))
        r_rec = req("GET", "/api/alerts/records/export", params={"status": "recovered"})
        labels = {x[7] for x in _rows(r_rec)[1:]}
        check("告警来源覆盖拨测/主机看护", {"拨测", "主机看护"} <= labels, labels)
        r_src = req("GET", "/api/alerts/records/export", params={"source": "sls"})
        check("告警导出 source 过滤=45",
              r_src.headers.get("x-total-rows") == "45", r_src.headers.get("x-total-rows"))
        check("告警导出顺序 ID 倒序",
              int(_rows(r)[1][0]) > int(_rows(r)[-1][0]))

        # ---- D 截断 + 公式注入（单元级，避免造 2 万行） ----
        from app.core.csvx import csv_response

        resp = csv_response([("a", "列")], [{"a": i} for i in range(5)],
                            filename="导出.csv", ascii_filename="x.csv",
                            total_rows=5, max_rows=2)
        body = ""
        async def _drain():
            nonlocal body
            async for chunk in resp.body_iterator:
                body += chunk
        asyncio.run(_drain())
        check("截断头 X-Total-Rows=5", resp.headers["x-total-rows"] == "5")
        check("截断头 X-Exported-Rows=2", resp.headers["x-exported-rows"] == "2")
        check("截断注释行", "# truncated at 2 rows" in body, body)
        check("截断后只输出 2 行数据", body.count("\r\n") == 3, body)
        inj = csv_response([("a", "列")],
                           [{"a": "=cmd|'/c calc'"}, {"a": -5}, {"a": "+1"}, {"a": "@x"}],
                           filename="i.csv", ascii_filename="i.csv", total_rows=4)
        ib = ""
        async def _drain2():
            nonlocal ib
            async for chunk in inj.body_iterator:
                ib += chunk
        asyncio.run(_drain2())
        check("公式注入加前缀", "'=cmd" in ib and "'+1" in ib and "'@x" in ib, ib)
        check("负数保持数字", "\r\n-5\r\n" in ib, ib)

        # ---- E 权限 ----
        appmod.dependency_overrides.pop(deps.get_current_user, None)
        r = req("GET", "/api/cost/daily/export")
        check("无登录导出 401", r.status_code == 401, r.status_code)
        req_v, _ = _client("viewer")
        r = req_v("GET", "/api/alerts/records/export")
        check("viewer 可导出", r.status_code == 200, r.status_code)
        req, appmod = _client("admin")   # 恢复 admin 覆盖

        # ---- F 审计 ----
        logs = {a: db.query(AuditLog).filter(AuditLog.action == a).all()
                for a in ("cost.daily.export", "assets.export", "alerts.records.export")}
        check("三类导出均落审计", all(logs.values()),
              {k: len(v) for k, v in logs.items()})
        import json
        cost_detail = json.loads(logs["cost.daily.export"][0].detail)
        check("成本审计含行数与筛选", cost_detail.get("rows") == 120
              and cost_detail.get("product") == "ecs", cost_detail)
        asset_detail = json.loads(logs["assets.export"][0].detail)
        check("资产审计含行数", asset_detail.get("rows") == 200, asset_detail)
        alert_details = [json.loads(x.detail) for x in logs["alerts.records.export"]]
        check("告警审计含来源筛选", any(d.get("source") == "sls" and d.get("rows") == 45
                                         for d in alert_details), alert_details)

    finally:
        db.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
