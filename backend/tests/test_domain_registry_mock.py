#!/usr/bin/env python3
"""域名登记表冒烟：一次性 PostgreSQL 全链路验证。

运行：cd /opt/qianshu/backend && python3 tests/test_domain_registry_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不用真实主密钥、不调任何云 API、不起后台任务
（TestClient 不进 context manager = 不触发 startup 定时任务）。

验证项：
  A 幂等迁移：create_all + run_migrations 两遍，domain_registry 就绪且无重复；
  B 六源回填：link/cdn/profile/synth/diagnosis/biz(+注册/证书) 归集，
    域名归一（去端口/scheme/大小写），非域名（IP:port、default）不进表（不造数据）；
  C 回填幂等：两遍 upsert 结果一致（第二遍 created=0）；
  D 证书事实：synth 实测优先、CAS 通配符兜底、序列号只填真实证据；
  E 登记字段：派生种子/归属登记优先/人工固定不被采集覆盖/清空回自动；
  F API 链路：清单/详情聚合/PUT 精确改+审计/停用代替删除（DELETE 不存在）；
  G 详情证据：六处证据逐项在场。
"""
import json
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-dom-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("domains")
os.environ["MASTER_KEY"] = "cd" * 32          # 64 hex 测试主密钥
for _k in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
           "TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402,F401  导入全量模型（与 main.startup 的 create_all 同口径）
from app.core.migrate import run_migrations  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.business import BizIdentifier, Business  # noqa: E402
from app.models.cdn_domain_daily import CdnDomainDaily  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.diagnosis import DiagnosisSession  # noqa: E402
from app.models.domain_registry import DomainRegistry  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.models.object_profile import ObjectProfile  # noqa: E402
from app.models.ownership import OwnershipRule  # noqa: E402
from app.models.synth import SynthCheck, SynthResult  # noqa: E402
from app.services import domain_registry as dr  # noqa: E402

PASS, FAIL = [], []
NOW = datetime.now()


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def seed(db: Session) -> None:
    """最小六源环境（形态故意脏：端口/scheme/大小写/IP/伪 host，照真实库的坑）。"""
    db.add(CloudAccount(id=1, name="prod-acc", vendor="aliyun",
                        ak_cipher="", secret_cipher="", enabled=True, status="ok"))
    # ① 链路：带端口/大小写 + IP:port + 伪 host（后两者不进登记表）
    db.add(LinkEdge(chain="outer", host="Www.Demo.com:80", upstream_addr="10.0.0.1",
                    path_pattern="/", req_count=100, cnt_4xx=1, status="confirmed"))
    db.add(LinkEdge(chain="inner", host="www.demo.com", upstream_addr="10.0.0.2",
                    path_pattern="/api", rule_name="api-rule", rule_source="cloud",
                    req_count=50, status="draft"))
    db.add(LinkEdge(chain="outer", host="121.199.1.2:80", upstream_addr="10.0.0.9",
                    req_count=5, status="confirmed"))
    db.add(LinkEdge(chain="outer", host="default", upstream_addr="10.0.0.8",
                    req_count=5, status="confirmed"))
    # ② CDN 流量（账号事实 account_id=1）
    db.add(CdnDomainDaily(stat_date=date.today(), domain="Cdn.Demo.com", product="cdn",
                          traffic_bytes=3 * 1024 ** 3, account_id=1))
    # ③ 对象画像（域名类，带端口）
    db.add(ObjectProfile(object_type="domain", object_key="api.demo.com:8090", label="API"))
    # ④ 拨测（URL 形态）+ 证书实测文本
    db.add(SynthCheck(id=1, name="shop", check_type="http",
                      target="http://Shop.demo.com/login"))
    db.add(SynthResult(check_id=1, checked_at=NOW, ok=True, latency_ms=10, status_code=200,
                       detail="HTTP 200｜证书 45 天后到期"))
    # ⑤ 诊断引用（URL 形态）
    db.add(DiagnosisSession(question="shop.demo.com 打不开", host="http://shop.demo.com/",
                            status="ok", level="warning"))
    # ⑥ 业务线登记（带端口/大小写）
    b1 = Business(name="商城")
    b2 = Business(name="官网")
    db.add_all([b1, b2])
    db.flush()
    db.add(BizIdentifier(business_id=b1.id, ident_type="host", ident_value="Shop.demo.com:443"))
    db.add(BizIdentifier(business_id=b2.id, ident_type="host", ident_value="www.demo.com"))
    # ⑦ 注册/人工登记 + 证书订单（collector/domain.py 落点；新旧两张同名通配符证）
    # Manual 行 account_id 用列默认 1：这是列默认值不是归属证据，派生必须留空（不猜）
    db.add(Asset(product="domain", instance_id="manual.demo.com", name="manual.demo.com",
                 status="Manual", expire_time=NOW + timedelta(days=200)))
    db.add(Asset(product="ssl_cert", instance_id="cas-old", name="*.demo.com", status="EXPIRED",
                 expire_time=NOW - timedelta(days=10),
                 extra=json.dumps({"root_brand": "GeoTrust", "order_id": 1})))
    db.add(Asset(product="ssl_cert", instance_id="cas-new", name="*.demo.com", status="ISSUED",
                 expire_time=NOW + timedelta(days=100),
                 extra=json.dumps({"root_brand": "GeoTrust", "order_id": 2})))
    # 精确域名证书（无任何其他来源）：到期项点域名要能进详情（来源⑧）
    db.add(Asset(product="ssl_cert", instance_id="cas-solo", name="solo.demo.com", status="ISSUED",
                 expire_time=NOW + timedelta(days=200),
                 extra=json.dumps({"root_brand": "DigiCert", "serial": "0ABC1234"})))
    db.commit()


# ================= A. 幂等迁移 =================
def test_migration() -> None:
    print("A. 幂等迁移")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    Base.metadata.create_all(bind=engine)   # 第二遍：新表 create_all 幂等
    run_migrations(engine)
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    check("A1 domain_registry 表就绪", insp.has_table("domain_registry"))
    with SessionLocal() as db:
        check("A2 两遍迁移后无重复行", db.query(DomainRegistry).count() == 0)


# ================= B/C. 六源回填 + 幂等 =================
def test_backfill() -> dict:
    print("B/C. 六源回填 + 幂等")
    with SessionLocal() as db:
        seed(db)
        st = dr.backfill(db)
        check("B1 回填统计 domains=6", st["domains"] == 6, str(st))
        rows = {r.domain: r for r in db.query(DomainRegistry).all()}
        expect = {"www.demo.com", "cdn.demo.com", "api.demo.com", "shop.demo.com",
                  "manual.demo.com", "solo.demo.com"}
        check("B2 六域名归一收口（去端口/scheme/大小写）", set(rows) == expect, str(set(rows)))
        check("B2b 证书精确域名入登记（来源⑧，通配符不算）",
              rows["solo.demo.com"].sources == "cert" and "*.demo.com" not in rows)
        check("B3 IP:port 不进登记表（不造数据）", "121.199.1.2" not in rows)
        check("B4 伪 host default 不进登记表", "default" not in rows)
        check("B5 来源标记：链路+业务线（含证书匹配）",
              {"biz", "link", "cert"} == set(rows["www.demo.com"].sources.split(",")),
              rows["www.demo.com"].sources)
        check("B6 来源标记：CDN", {"cdn", "cert"} <= set(rows["cdn.demo.com"].sources.split(",")),
              rows["cdn.demo.com"].sources)
        check("B7 来源标记：画像", "profile" in rows["api.demo.com"].sources)
        check("B8 来源标记：拨测+诊断", {"synth", "diagnosis"} <=
              set(rows["shop.demo.com"].sources.split(",")), rows["shop.demo.com"].sources)
        check("B9 来源标记：注册", "registrar" in rows["manual.demo.com"].sources)

        snap = {d: (r.sources, r.cert_not_after, r.cert_source, r.business, r.vendor,
                    r.account_name, r.status) for d, r in rows.items()}
        st2 = dr.backfill(db)
        rows2 = {r.domain: r for r in db.query(DomainRegistry).all()}
        snap2 = {d: (r.sources, r.cert_not_after, r.cert_source, r.business, r.vendor,
                     r.account_name, r.status) for d, r in rows2.items()}
        check("C1 幂等：第二遍 created=0", st2["created"] == 0 and st2["updated"] == 6, str(st2))
        check("C2 幂等：两遍字段一致", snap == snap2)
    return {}


# ================= D. 证书事实 =================
def test_cert() -> None:
    print("D. 证书事实（synth 实测优先 / CAS 通配符 / 序列号只填真实证据）")
    with SessionLocal() as db:
        rows = {r.domain: r for r in db.query(DomainRegistry).all()}
        shop, www = rows["shop.demo.com"], rows["www.demo.com"]
        check("D1 synth 实测解出证书到期（checked_at+45d）",
              shop.cert_source == "tls_probe" and shop.cert_not_after
              and abs((shop.cert_not_after.date() - (NOW.date() + timedelta(days=45))).days) <= 1,
              f"{shop.cert_source} {shop.cert_not_after}")
        check("D2 CAS 通配符 *.demo.com 覆盖 www.demo.com",
              www.cert_source == "cas_order" and www.cert_not_after
              and abs((www.cert_not_after.date() - (NOW + timedelta(days=100)).date()).days) <= 1,
              f"{www.cert_source} {www.cert_not_after}")
        check("D3 同域名多张证取最新一张（旧证 10 天前那张不生效）",
              abs((www.cert_not_after.date() - (NOW + timedelta(days=100)).date()).days) <= 1)
        check("D4 synth 证据里无序列号 → 序列号留空（不造数据）", shop.cert_serial == "")
        check("D4b 证据里有序列号 → 落表", rows["solo.demo.com"].cert_serial == "0ABC1234",
              rows["solo.demo.com"].cert_serial)
        check("D5 证书匹配来源标记", "cert" in rows["manual.demo.com"].sources
              and "cert" in shop.sources)
        check("D6 覆盖函数：一级通配/不多级/不盖裸域",
              dr.cert_covers("*.demo.com", "a.demo.com")
              and not dr.cert_covers("*.demo.com", "demo.com")
              and not dr.cert_covers("*.demo.com", "a.b.demo.com"))


# ================= E. 登记字段语义 =================
def test_fields() -> None:
    print("E. 登记字段（派生/归属登记优先/人工不被采集覆盖/清空回自动）")
    with SessionLocal() as db:
        rows = {r.domain: r for r in db.query(DomainRegistry).all()}
        check("E1 派生：业务线登记种子（带端口归一命中）",
              rows["www.demo.com"].business == "官网", rows["www.demo.com"].business)
        check("E2 派生：业务线登记（shop）", rows["shop.demo.com"].business == "商城")
        check("E3 派生：采集账号事实（CDN account→云账号台账）",
              rows["cdn.demo.com"].vendor == "aliyun"
              and rows["cdn.demo.com"].account_name == "prod-acc",
              f"{rows['cdn.demo.com'].vendor}/{rows['cdn.demo.com'].account_name}")
        check("E4 无登记无账号事实 → 留空（不猜；Manual 行的 account_id=1 列默认值不算事实）",
              rows["manual.demo.com"].business == ""
              and rows["manual.demo.com"].account_name == ""
              and rows["manual.demo.com"].vendor == "")

        # 归属登记（ownership_rules）→ 派生值升级（登记优先于业务线登记）
        db.add(OwnershipRule(key_type="domain", key_value="www.demo.com", match_type="exact",
                             vendor="onprem", account_name="IDC 机房", business="官网业务"))
        db.commit()
        dr.backfill(db)
        row = db.get(DomainRegistry, "www.demo.com")
        check("E5 归属登记优先（业务/厂商/账号）",
              row.business == "官网业务" and row.vendor == "onprem"
              and row.account_name == "IDC 机房",
              f"{row.business}/{row.vendor}/{row.account_name}")

        # 人工固定 → 采集不覆盖
        row.manual_fields = "business"
        row.business = "人工固定"
        db.commit()
        dr.backfill(db)
        row = db.get(DomainRegistry, "www.demo.com")
        check("E6 人工字段不被采集覆盖", row.business == "人工固定", row.business)
        check("E7 未固定字段仍随登记刷新", row.vendor == "onprem")

        # 清空人工值 → 回到自动派生
        row.manual_fields = ""
        db.commit()
        dr.backfill(db)
        row = db.get(DomainRegistry, "www.demo.com")
        check("E8 清空人工值回到派生（归属登记）", row.business == "官网业务", row.business)


# ================= F/G. API 链路 + 详情证据 =================
def test_api() -> None:
    print("F/G. API 链路（ASGI 直连，不起后台任务）")
    import asyncio

    import httpx

    import app.core.deps as deps
    from app.main import app

    fake = types.SimpleNamespace(id=1, username="smoke", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: fake

    def _req(method, url, **kw):
        async def _go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(_go())

    try:
        r = _req("GET", "/api/domains", params={"refresh": 1})
        j = r.json()
        check("F1 清单接口（含证书到期/关联 SLB/CDN/业务）",
              r.status_code == 200 and j["total"] == 6
              and {i["domain"] for i in j["items"]} ==
              {"www.demo.com", "cdn.demo.com", "api.demo.com", "shop.demo.com",
               "manual.demo.com", "solo.demo.com"}, r.text[:300])
        by = {i["domain"]: i for i in j["items"]}
        check("F2 清单带 SLB 聚合（2 条规则边/150 req）",
              by["www.demo.com"]["slb"]["edge_count"] == 2
              and by["www.demo.com"]["slb"]["req_24h"] == 150, str(by["www.demo.com"]["slb"]))
        check("F3 清单带 CDN 流量", (by["cdn.demo.com"]["cdn"] or {}).get("traffic_30d_gb", 0) > 0,
              str(by["cdn.demo.com"]["cdn"]))
        check("F4 清单带业务/归属", by["www.demo.com"]["business"] == "官网业务"
              and by["www.demo.com"]["account_name"] == "IDC 机房")
        days_seq = [i["cert_days_left"] for i in j["items"]]
        check("F5 证书到期升序（无证书沉底）",
              j["items"][0]["domain"] == "shop.demo.com"
              and days_seq == sorted(days_seq),
              str([(i["domain"], i["cert_days_left"]) for i in j["items"]]))
        r = _req("GET", "/api/domains", params={"level": "ok", "refresh": 0})
        check("F6 证书到期分级筛选", r.status_code == 200
              and all(i["cert_level"] == "ok" for i in r.json()["items"]))

        r = _req("GET", "/api/domains/www.demo.com/detail")
        d = r.json()
        check("G1 详情：链路证据（两种 host 形态都归进来）",
              r.status_code == 200 and len(d["slb"]["edges"]) == 2, r.text[:300])
        check("G2 详情：证书证据在场（CAS 订单）",
              d["cert"]["source"] == "cas_order" and len(d["cert"]["evidence"]) >= 1)
        check("G3 详情：业务线登记 + 归属登记命中",
              any(x["business"] == "官网" for x in d["business"]["identifiers"])
              and d["business"]["ownership"] and d["business"]["ownership"]["business"] == "官网业务",
              str(d["business"])[:300])
        r = _req("GET", "/api/domains/shop.demo.com/detail")
        d = r.json()
        check("G4 详情：拨测证据（目标+证书实测文本）",
              len(d["synth"]["checks"]) == 1
              and "证书" in (d["synth"]["checks"][0]["results"][0]["detail"] or ""),
              str(d["synth"])[:300])
        check("G5 详情：诊断引用", len(d["diagnosis"]["sessions"]) == 1, str(d["diagnosis"]))
        r = _req("GET", "/api/domains/api.demo.com/detail")
        d = r.json()
        check("G6 详情：对象画像证据（带端口归一命中）",
              len(d["profile"]["objects"]) == 1 and d["profile"]["objects"][0]["object_key"] ==
              "api.demo.com:8090", str(d["profile"]))
        r = _req("GET", "/api/domains/cdn.demo.com/detail")
        d = r.json()
        check("G7 详情：CDN 证据 + 注册/证书资产",
              len(d["cdn"]["daily"]) == 1 and len(d["assets"]["certs"]) == 2,
              str(d["cdn"])[:200] + str(d["assets"])[:200])
        r = _req("GET", "/api/domains/solo.demo.com/detail")
        d = r.json()
        check("G8 详情：证书精确域名（仅证书来源）可进详情且序列号在场",
              r.status_code == 200 and d["cert"]["serial"] == "0ABC1234"
              and d["cert"]["source"] == "cas_order", r.text[:300])

        r = _req("GET", "/api/domains/not-a-domain/detail")
        check("F7 非法域名 400", r.status_code == 400, r.text[:100])
        r = _req("GET", "/api/domains/nosuch.demo.com/detail")
        check("F8 无证据域名 404（不造数据）", r.status_code == 404, r.text[:100])

        r = _req("PUT", "/api/domains/www.demo.com", json={"note": "核心官网"})
        check("F9 PUT 补备注", r.status_code == 200 and r.json()["note"] == "核心官网",
              r.text[:200])
        r = _req("PUT", "/api/domains/www.demo.com", json={"business": "人工固定"})
        check("F10 PUT 人工固定业务（精确改）",
              r.status_code == 200 and r.json()["business"] == "人工固定"
              and r.json()["manual_fields"] == ["business"], r.text[:200])
        with SessionLocal() as db:
            dr.backfill(db)   # 采集再来一遍
            row = db.get(DomainRegistry, "www.demo.com")
            check("F11 API 人工字段不被采集覆盖", row.business == "人工固定", row.business)
            n_audit = (db.query(AuditLog)
                       .filter(AuditLog.action == "domain.update").count())
            check("F12 审计落库", n_audit == 2, str(n_audit))
        r = _req("PUT", "/api/domains/www.demo.com", json={"business": ""})
        check("F13 清空=取消固定回自动派生",
              r.status_code == 200 and r.json()["business"] == "官网业务"
              and r.json()["manual_fields"] == [], r.text[:200])
        r = _req("PUT", "/api/domains/www.demo.com", json={"status": "disabled"})
        check("F14 停用代替删除", r.status_code == 200 and r.json()["status"] == "disabled")
        r = _req("DELETE", "/api/domains/www.demo.com")
        check("F15 不提供删除（DELETE 不存在）", r.status_code == 405, r.text[:100])
        r = _req("GET", "/api/domains", params={"status": "active", "refresh": 0})
        check("F16 停用项可被筛掉", all(i["domain"] != "www.demo.com"
                                       for i in r.json()["items"]))
        r = _req("POST", "/api/domains/backfill")
        check("F17 手动回填接口（幂等）", r.status_code == 200
              and r.json()["created"] == 0, r.text[:200])
        r = _req("PUT", "/api/domains/nosuch.demo.com", json={"note": "x"})
        check("F18 PUT 未收录域名 404", r.status_code == 404, r.text[:100])

        # 注册行的采集账号事实（云同步行，非 Manual）：account_id→云账号台账
        with SessionLocal() as db:
            db.add(Asset(product="domain", instance_id="synced.demo.com", name="synced.demo.com",
                         status="ADD_NEW", account_id=1))
            db.commit()
            dr.backfill(db)
        r = _req("GET", "/api/domains", params={"refresh": 0})
        by = {i["domain"]: i for i in r.json()["items"]}
        check("F19 云同步注册行带采集账号事实",
              by["synced.demo.com"]["account_name"] == "prod-acc"
              and by["synced.demo.com"]["vendor"] == "aliyun",
              str(by.get("synced.demo.com")))
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    test_migration()
    test_backfill()
    test_cert()
    test_fields()
    test_api()
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
