#!/usr/bin/env python3
"""可解释业务归属草稿隔离回归。

运行：cd /opt/qianshu/backend && python3 tests/test_biz_draft_mock.py
只使用一次性 PostgreSQL 与 ASGI/API mock，不启动服务、不访问云、不读写本机业务库。
覆盖证据过滤、冲突/未决、草稿确认门、确认幂等、人工变更与旧库迁移。
"""
import asyncio
import json
import os
import sys
import tempfile
import types

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-biz-draft-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("biz-draft")
os.environ["MASTER_KEY"] = "cd" * 32
for _key in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
             "TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
    os.environ.pop(_key, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_business_status, run_migrations  # noqa: E402
from app.main import app  # noqa: E402,F401  导入全量模型；ASGI 直连不触发 lifespan
from app.models.asset import Asset  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.business import BizIdentifier, Business  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.services.biz_map import generate_biz_draft, plan_biz_draft, resolve_biz  # noqa: E402
from app.services.domain_registry import biz_domain_map  # noqa: E402
from app.services.ownership import business_from_hints, business_hint_map  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _edge(host: str, *, status="confirmed", source="log", req=1, path="",
          ecs="", app="", upstream="10.0.0.1:80") -> LinkEdge:
    return LinkEdge(chain="outer", host=host, upstream_addr=upstream, path_pattern=path,
                    rule_source=source, req_count=req, status=status,
                    ecs_name=ecs, app_name=app)


def _asset(product: str, iid: str, name: str, tags, status="Running") -> Asset:
    raw = json.dumps(tags, ensure_ascii=False) if not isinstance(tags, str) else tags
    return Asset(product=product, instance_id=iid, name=name, status=status, tags=raw)


def seed(db) -> None:
    db.add_all([
        _edge("orders.demo.com", req=120, ecs="orders-ecs", app="orders-app"),
        _edge("orders.demo.com", req=30, path="/api", ecs="orders-ecs", app="orders-app"),
        _edge("red.demo.com", req=20, ecs="shared-ecs", app="shared-app"),
        _edge("blue.demo.com", req=10, ecs="shared-ecs", app="shared-app"),
        _edge("draft.demo.com", status="draft", req=99),
        _edge("ignored.demo.com", status="ignored", req=99),
        _edge("cloud.demo.com", source="cloud", req=99),
        _edge("zero.demo.com", req=0),
        _edge("www.demo.com"), _edge("api.demo.com"), _edge("127.0.0.1:80"),
        _edge("*.wild.demo.com"), _edge("localhost"),
    ])
    db.add_all([
        _asset("ecs", "i-crm", "CRM-Node", [{"Key": "Business", "Value": "CRM"}]),
        _asset("rds", "i-billing", "billing-rds", {"业务线": "Billing"}),
        _asset("ecs", "i-conflict", "conflict-node",
               [{"key": "business", "value": "A"}, {"key": "biz", "value": "B"}]),
        _asset("ecs", "i-unknown", "unknown-node", {"business": "unknown"}),
        _asset("ecs", "i-deleted", "deleted-node", {"business": "Gone"}, status="Deleted"),
        _asset("ecs", "i-red", "red-node", {"business": "red"}),
        _asset("ecs", "i-blue", "blue-node", {"business": "blue"}),
        # 不同产品可以出现相同实例 ID；共享键不得被自动挂到任一候选。
        _asset("ecs", "i-shared", "shared-red", {"business": "red"}),
        _asset("slb", "i-shared", "shared-blue", {"business": "blue"}),
    ])
    db.commit()


def _requester():
    import app.core.deps as deps

    fake = types.SimpleNamespace(id=7, username="biz-smoke", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: fake

    def req(method, url, **kw):
        async def go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver") as client:
                return await client.request(method, url, **kw)
        return asyncio.run(go())

    return req, fake


def test_evidence_plan() -> None:
    print("A. 证据过滤、冲突和未决（planner 只读）")
    with SessionLocal() as db:
        seed(db)
        before = (db.query(Business).count(), db.query(BizIdentifier).count(),
                  db.query(LinkEdge).count(), db.query(Asset).count())
        plan = plan_biz_draft(db)
        again = plan_biz_draft(db)
        after = (db.query(Business).count(), db.query(BizIdentifier).count(),
                 db.query(LinkEdge).count(), db.query(Asset).count())
        candidates = {c["name"]: c for c in plan["candidates"]}
        check("A1 planner 不落业务或标识、不改证据", before == after == (0, 0, 13, 9), str((before, after)))
        check("A2 重跑结果确定", plan == again)
        check("A3 只采纳已确认日志流量边与精确业务标签",
              set(candidates) == {"orders", "red", "blue", "CRM", "Billing"}, str(list(candidates)))
        check("A4 同候选多条流量证据求和", candidates["orders"]["request_count"] == 150)
        check("A5 通用域名/IP/泛域名保持未决",
              {x["host"] for x in plan["unresolved_hosts"]}
              == {"www.demo.com", "api.demo.com", "127.0.0.1", "*.wild.demo.com", "localhost"},
              str(plan["unresolved_hosts"]))
        check("A6 非确认/忽略/云配置/零流量边不产生候选或未决噪声",
              not ({"draft.demo.com", "ignored.demo.com", "cloud.demo.com", "zero.demo.com"}
                   & ({*candidates} | {x["host"] for x in plan["unresolved_hosts"]})))
        conflicts = {(x["kind"], x["ident_type"], x["value"]): x for x in plan["conflicts"]}
        check("A7 冲突标签不自动绑定", ("asset_tag", "instance", "i-conflict") in conflicts
              and not ({"A", "B"} & set(candidates)), str(plan["conflicts"]))
        check("A8 共享 ECS/app/instance 均留冲突且每条只报一次", len(plan["conflicts"]) == 4
              and {("shared_identifier", "ecs_name", "shared-ecs"),
                   ("shared_identifier", "app_name", "shared-app"),
                   ("shared_identifier", "instance", "i-shared")} <= set(conflicts),
              str(plan["conflicts"]))
        all_idents = {(i["ident_type"], i["ident_value"]) for c in plan["candidates"]
                      for i in c["identifiers"]}
        check("A9 冲突标识不进入任何候选",
              not ({("ecs_name", "shared-ecs"), ("app_name", "shared-app"),
                    ("instance", "i-shared"), ("instance", "i-conflict")} & all_idents), str(all_idents))
        check("A10 未知/删除标签不成为事实", not ({"unknown", "Gone"} & set(candidates)))
        check("A11 返回实际证据量",
              plan["evidence"] == {"confirmed_log_edges": 9, "tagged_assets": 6}, str(plan["evidence"]))


def test_confirmation_gate() -> None:
    print("B. 草稿持久化不生效；逐条确认后才生效")
    with SessionLocal() as db:
        result = generate_biz_draft(db, created_by="biz-smoke", created_by_id=7)
        db.commit()
        rows = {b.name: b for b in db.query(Business).all()}
        check("B1 自动生成全部 draft", result["created"] == 5 and len(rows) == 5
              and {b.status for b in rows.values()} == {"draft"}, str(result))
        check("B2 草稿不经告警/巡检/成本公共解析生效", resolve_biz(db, target="orders.demo.com") == [])
        check("B3 草稿不经拓扑/资产 hint map 生效",
              business_from_hints(business_hint_map(db), domain="orders.demo.com") == "")
        check("B4 草稿不经域名登记生效", biz_domain_map(db) == {})
        count = db.query(BizIdentifier).count()
        repeated = generate_biz_draft(db, created_by="biz-smoke", created_by_id=7)
        db.commit()
        check("B5 重复生成不重复落库", repeated["created"] == 0
              and len(repeated["skipped_existing"]) == 5
              and db.query(BizIdentifier).count() == count, str(repeated))

    req, user = _requester()
    try:
        listing = req("GET", "/api/links/businesses").json()["items"]
        orders = next(x for x in listing if x["name"] == "orders")
        crm = next(x for x in listing if x["name"] == "CRM")
        check("B6 管理列表明确返回草稿状态", orders["status"] == "draft")
        mapped = req("GET", f"/api/links/businesses/{orders['id']}/map")
        check("B7 右侧只是带 draft 标记的证据预览", mapped.status_code == 200
              and mapped.json()["business"]["status"] == "draft"
              and any(x["host"] == "orders.demo.com" for x in mapped.json()["domains"]))

        manual = req("POST", "/api/links/businesses", json={"name": "manual-biz", "code": "manual"})
        check("B8 手工新建仍直接 confirmed", manual.status_code == 200
              and manual.json()["status"] == "confirmed", manual.text[:200])
        manual_id = manual.json()["id"]
        added = req("POST", f"/api/links/businesses/{manual_id}/identifiers",
                    json={"ident_type": "host", "ident_value": "manual.demo.com", "note": ""})
        with SessionLocal() as db:
            check("B9 手工业务确认后按既有路径生效", added.status_code == 200
                  and resolve_biz(db, target="manual.demo.com") == ["manual-biz"]
                  and biz_domain_map(db).get("manual.demo.com") == ["manual-biz"])

        user.role = "viewer"
        denied = req("POST", f"/api/links/businesses/{orders['id']}/confirm", json={})
        user.role = "admin"
        check("B10 viewer 不能确认草稿", denied.status_code == 403, denied.text[:200])
        first = req("POST", f"/api/links/businesses/{orders['id']}/confirm", json={})
        second = req("POST", f"/api/links/businesses/{orders['id']}/confirm", json={})
        missing = req("POST", "/api/links/businesses/999999/confirm", json={})
        with SessionLocal() as db:
            hints = business_hint_map(db)
            check("B11 首次确认后三条归属通道同时生效", first.status_code == 200
                  and first.json()["confirmed"] is True
                  and resolve_biz(db, target="orders.demo.com") == ["orders"]
                  and business_from_hints(hints, domain="orders.demo.com") == "orders"
                  and biz_domain_map(db).get("orders.demo.com") == ["orders"])
            check("B12 重复确认幂等且只记一次确认审计", second.status_code == 200
                  and second.json()["confirmed"] is False and missing.status_code == 404
                  and db.query(AuditLog).filter(AuditLog.action == "business.confirm",
                                                AuditLog.resource_id == str(orders["id"])).count() == 1)
            check("B13 确认一个草稿不会放行其他草稿",
                  db.query(Business).filter(Business.name == "red").one().status == "draft"
                  and resolve_biz(db, target="red.demo.com") == []
                  and crm["status"] == "draft")

        from app.services.copilot.change_tools import _h_save_biz_map, _rb_biz_map, _snap_biz_map

        with SessionLocal() as db:
            before = _snap_biz_map(db, {"name": "CRM"})
            out = _h_save_biz_map(db, {
                "name": "CRM", "code": "crm", "description": "已人工核对",
                "identifiers": [{"ident_type": "ip", "ident_value": "10.20.0.8:8080", "note": ""}],
            }, user, "人工确认变更")
            check("B14 已确认智能体变更保存权威映射时解除草稿门",
                  before["status"] == "draft"
                  and out["result"]["business"]["status"] == "confirmed"
                  and db.query(Business).filter(Business.name == "CRM").one().status == "confirmed"
                  and resolve_biz(db, target="10.20.0.8:8080") == ["CRM"], str(out))
            _rb_biz_map(db, {"name": "CRM"}, before, {"name": "CRM"}, user)
            db.commit()
            check("B15 智能体回滚同时恢复草稿状态和原标识",
                  db.query(Business).filter(Business.name == "CRM").one().status == "draft"
                  and resolve_biz(db, target="10.20.0.8:8080") == []
                  and not db.query(BizIdentifier).filter(
                      BizIdentifier.business_id == before["id"],
                      BizIdentifier.ident_value == "10.20.0.8").first())
    finally:
        app.dependency_overrides.clear()


def test_legacy_migration() -> None:
    print("C. 旧业务映射迁移与幂等")
    legacy = create_engine(test_database_url("bizmig"))
    with legacy.begin() as conn:
        conn.execute(text(
            "CREATE TABLE businesses (id INTEGER PRIMARY KEY, name VARCHAR(128),"
            " code VARCHAR(64) DEFAULT '', description VARCHAR(500) DEFAULT '',"
            " created_at TIMESTAMP)"))
        conn.execute(text("INSERT INTO businesses (id, name, description) VALUES"
                          " (1, 'old-human', '人工登记'), (2, 'old-auto', '自动草稿：域名聚类')"))
    migrate_business_status(legacy)
    with legacy.begin() as conn:
        rows = dict(conn.execute(text("SELECT name, status FROM businesses")).fetchall())
        check("C1 旧人工映射默认 confirmed、旧自动草稿降 draft",
              rows == {"old-human": "confirmed", "old-auto": "draft"}, str(rows))
        # 模拟迁移后人工已经确认；重跑启动迁移不得把它再次降级。
        conn.execute(text("UPDATE businesses SET status='confirmed' WHERE name='old-auto'"))
    migrate_business_status(legacy)
    with legacy.connect() as conn:
        rows = dict(conn.execute(text("SELECT name, status FROM businesses")).fetchall())
        check("C2 迁移重复执行不覆盖后续人工确认",
              rows == {"old-human": "confirmed", "old-auto": "confirmed"}, str(rows))
    legacy.dispose()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    test_evidence_plan()
    test_confirmation_gate()
    test_legacy_migration()
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
