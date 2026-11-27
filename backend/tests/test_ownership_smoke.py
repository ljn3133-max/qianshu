#!/usr/bin/env python3
"""归属可配隔离冒烟（数据孤岛治理）：一次性 PostgreSQL 全链路验证。

运行：cd /opt/qianshu/backend && python3.9 tests/test_ownership_smoke.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不用真实主密钥、不调任何云 API、不起后台任务
（TestClient 不进 context manager = 不触发 startup 定时任务）。

验证项：
  A 幂等迁移：create_all + run_migrations 两遍，ownership_rules 就绪且无重复；
  B 不造数据：无登记时 EIP/域名/未纳管主机/台账外资产全部保持"未归属"（红线）；
  C 归属登记→回流：ip/domain/instance 三形态登记后拓扑 meta 回填（含虚拟账号聚合）；
  D 字段覆盖：只登记业务 → 厂商/账号保留台账值；绑定云账号 → 与台账同名；
  E 解析优先级：exact > pattern；键优先级 instance > ip > domain > name；长 pattern 优先；
  F 归一：IP:port 去端口 / 域名小写去尾点 / 大小写不敏感；
  G API 链路：TestClient 走真实路由（登记/去重/试算/未归属清单/删除后回流撤销）；
  H 五展示面回流：登记→资产/多云/成本/告警/到期全回流，
    未登记行原状（含告警"account_name 空则补、已有不覆盖"语义、绑定台账同名）；
  I biz_identifiers ip/instance 键：归一入库/去重/非法拒绝/业务挂接一处收口
    （hint map + 告警 biz 标签 + 拓扑回流）/ 迁移幂等（无 DDL，两遍无损）。
"""
import json
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta  # noqa: E402

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-own-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("ownership")
os.environ["MASTER_KEY"] = "cd" * 32          # 64 hex 测试主密钥
for _k in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
           "TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402,F401  导入全量模型（与 main.startup 的 create_all 同口径）
from app.core.migrate import run_migrations  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.business import BizIdentifier  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.cost_daily import CostDaily  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.models.ownership import OwnershipRule  # noqa: E402
from app.models.resource_pack_snapshot import ResourcePackSnapshot  # noqa: E402
from app.services import ownership as own  # noqa: E402
from app.services.biz_map import resolve_biz  # noqa: E402
from app.services.topology import build_arch, build_graph  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def seed(db: Session) -> None:
    """最小环境：1 云账号 + 2 资产 + 2 链路（域名 + 幽灵主机）。"""
    db.add(CloudAccount(id=1, name="prod-acc", vendor="aliyun",
                        ak_cipher="", secret_cipher="", enabled=True, status="ok"))
    db.add(Asset(product="ecs", instance_id="i-aaa", name="web-01", account_id=1,
                 status="Running", extra=json.dumps(
                     {"ip_public": "1.2.3.4", "ip_internal": "192.168.1.10"})))
    db.add(Asset(product="ecs", instance_id="i-bbb", name="db-01", account_id=99,
                 status="Running", extra=json.dumps({"ip_internal": "192.168.1.20"})))
    db.add(LinkEdge(chain="outer", host="www.demo.com", upstream_addr="192.168.1.10",
                    ecs_name="web-01", req_count=100, status="confirmed"))
    # 同 (域名,后端) 按路径拆行（LinkEdge 粒度 chain,host,path_pattern,upstream_addr）：
    # 拓扑 flow 边权重必须求和（移交修复项验证）
    db.add(LinkEdge(chain="outer", host="www.demo.com", upstream_addr="192.168.1.10",
                    path_pattern="/api", ecs_name="web-01", req_count=50, status="confirmed"))
    db.add(LinkEdge(chain="outer", host="ghost.demo.com", upstream_addr="10.0.0.9:8080",
                    ecs_name="", req_count=10, status="confirmed"))
    db.commit()


def node_map(g: dict) -> dict:
    return {n["id"]: n for n in g["nodes"]}


# ================= A. 幂等迁移 =================
def test_migration() -> None:
    print("A. 幂等迁移")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    Base.metadata.create_all(bind=engine)   # 第二遍：新表 create_all 幂等
    run_migrations(engine)                  # 第二遍：迁移幂等
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    check("A1 ownership_rules 表就绪", insp.has_table("ownership_rules"))
    with SessionLocal() as db:
        check("A2 两遍迁移后无重复登记", db.query(OwnershipRule).count() == 0)


# ================= B/C/D. 归属与回流 =================
def test_flow() -> None:
    print("B/C/D. 不造数据 + 登记回流 + 字段覆盖")
    with SessionLocal() as db:
        seed(db)
        g = build_graph(db)
        nm = node_map(g)
        eip, dom, ghost = nm["eip:1.2.3.4"], nm["domain:www.demo.com"], nm["host:10.0.0.9:8080"]
        a_bbb = nm["ecs:i-bbb"]

        # B 红线：无登记=未归属，不造数据
        check("B1 EIP 无登记保持未归属", not eip["meta"].get("account") and not eip["meta"].get("vendor"))
        check("B2 域名无登记保持未归属", not dom["meta"].get("account") and not dom["meta"].get("vendor"))
        check("B3 幽灵主机无登记保持未归属", not ghost["meta"].get("account"))
        check("B4 台账外资产保持未归属（不造标签）", not a_bbb["meta"].get("account")
              and not a_bbb["meta"].get("vendor"))
        check("B5 台账内资产自动归属仍在", nm["ecs:i-aaa"]["meta"].get("account") == "prod-acc")
        un = own.unattributed(db, graph=g)
        un_keys = {(x["key_type"], x["key_value"]) for x in un}
        check("B6 未归属清单含全部 5 个实体",
              {("ip", "1.2.3.4"), ("domain", "www.demo.com"), ("domain", "ghost.demo.com"),
               ("ip", "10.0.0.9"), ("instance", "i-bbb")} <= un_keys, str(un_keys))
        flow = [e for e in g["edges"] if e["type"] == "flow"
                and e["source"] == "domain:www.demo.com" and e["target"] == "ecs:i-aaa"]
        check("B7 flow 边权重按路径拆行求和（100+50）",
              len(flow) == 1 and flow[0]["weight"] == 150.0
              and flow[0]["label"] == "150 req/24h", str(flow))

        # C 登记 → 回流
        db.add(OwnershipRule(key_type="ip", key_value="1.2.3.4", match_type="exact",
                             vendor="aliyun", account_name="入口账号"))
        db.add(OwnershipRule(key_type="domain", key_value="www.demo.com", match_type="exact",
                             business="官网业务"))
        db.add(OwnershipRule(key_type="instance", key_value="i-bbb", match_type="exact",
                             vendor="tencent", account_name="测试账号", business="测试业务"))
        db.add(OwnershipRule(key_type="ip", key_value="10.0.*", match_type="pattern",
                             vendor="onprem", account_name="IDC 机房"))
        db.commit()

        g2 = build_graph(db)
        nm2 = node_map(g2)
        eip2, dom2, ghost2 = nm2["eip:1.2.3.4"], nm2["domain:www.demo.com"], nm2["host:10.0.0.9:8080"]
        check("C1 EIP 按 ip 登记回填账号", eip2["meta"].get("account") == "入口账号", str(eip2["meta"]))
        check("C2 EIP 自由文本账号进虚拟账号聚合（id<0）", (eip2["meta"].get("account_id") or 0) < 0)
        check("C3 域名登记回填业务", dom2["meta"].get("business") == "官网业务")
        check("C4 台账外资产按 instance 登记回填", nm2["ecs:i-bbb"]["meta"].get("account") == "测试账号"
              and nm2["ecs:i-bbb"]["meta"].get("vendor") == "tencent"
              and nm2["ecs:i-bbb"]["meta"].get("business") == "测试业务")
        check("C5 幽灵主机命中 pattern（IP:port 归一）", ghost2["meta"].get("account") == "IDC 机房",
              str(ghost2["meta"]))
        un2 = {x["key_value"] for x in own.unattributed(db, graph=g2)}
        check("C6 配置后从未归属清单消失", not ({"1.2.3.4", "10.0.0.9", "i-bbb"} & un2), str(un2))
        check("C7 只登记业务的域名仍在未归属（厂商/账号仍缺）", "www.demo.com" in un2)

        arch = build_arch(db)
        labels = [(m.get("meta") or {}).get("account") for grp in arch["groups"] for m in grp["members"]]
        check("C8 架构图成员带账号标签", "入口账号" in labels and "测试账号" in labels, str(labels))
        acc_names = {a["name"] for a in arch["accounts"]}
        check("C9 图例含虚拟账号（不污染台账）", "入口账号" in acc_names and "测试账号" in acc_names,
              str(acc_names))
        check("C9b 云账号台账无虚拟账号行",
              db.query(CloudAccount).filter(CloudAccount.name == "入口账号").count() == 0)
        eip_grp = next(g for g in arch["groups"] if g["component"] == "公网入口")
        check("C10 组件框 account_label 回填", "入口账号" in (eip_grp.get("account_label") or ""),
              str(eip_grp.get("account_label")))

        # D 字段覆盖与多条合并
        aaa = nm2["ecs:i-aaa"]["meta"]
        check("D1 IP 登记覆盖台账账号（人工登记优先）",
              aaa.get("account") == "入口账号" and aaa.get("vendor") == "aliyun"
              and not aaa.get("business"), str(aaa))
        db.add(OwnershipRule(key_type="name", key_value="web-01", match_type="exact",
                             business="官网业务"))
        db.commit()
        g3 = build_graph(db)
        aaa3 = node_map(g3)["ecs:i-aaa"]["meta"]
        check("D2 多条登记逐字段合并（IP 账号 + 名称业务都生效）",
              aaa3.get("business") == "官网业务" and aaa3.get("account") == "入口账号",
              str(aaa3))
        # 只登记业务：厂商/账号保留台账值（按字段覆盖）
        db.add(Asset(product="ecs", instance_id="i-ccc", name="job-01", account_id=1,
                     status="Running", extra="{}"))
        db.add(OwnershipRule(key_type="name", key_value="job-01", match_type="exact",
                             business="批处理业务"))
        db.commit()
        g3b = build_graph(db)
        ccc = node_map(g3b)["ecs:i-ccc"]["meta"]
        check("D3 只登记业务→厂商/账号保留台账值",
              ccc.get("account") == "prod-acc" and ccc.get("vendor") == "aliyun"
              and ccc.get("account_id") == 1 and ccc.get("business") == "批处理业务", str(ccc))
        # 绑定云账号台账：账号组以绑定为准（纠正打标），业务登记仍合并生效
        db.add(OwnershipRule(key_type="instance", key_value="i-aaa", match_type="exact",
                             account_id=1))
        db.commit()
        g4 = build_graph(db)
        aaa4 = node_map(g4)["ecs:i-aaa"]["meta"]
        check("D4 绑定云账号→与台账同名且业务保留",
              aaa4.get("account") == "prod-acc" and aaa4.get("account_id") == 1
              and aaa4.get("business") == "官网业务", str(aaa4))


# ================= E/F. 解析优先级与归一 =================
def _rule(rid, kt, kv, mt="exact", **kw):
    d = {"id": rid, "key_type": kt, "key_value": kv, "match_type": mt,
         "vendor": "", "account_name": "", "account_id": 0, "business": "", "note": ""}
    d.update(kw)
    return types.SimpleNamespace(**d)


def test_resolve() -> None:
    print("E/F. 解析优先级 + 键归一")
    check("F1 IP:port 去端口", own.norm_key("ip", "10.0.0.9:8080") == "10.0.0.9")
    check("F2 域名小写去尾点", own.norm_key("domain", "WWW.Demo.COM.") == "www.demo.com")
    check("F3 实例 ID 大小写不敏感", own.norm_key("instance", " I-AAA ") == "i-aaa")

    rules = [
        _rule(1, "name", "web-*", "pattern", account_name="PAT-NAME"),
        _rule(2, "instance", "i-aaa", "exact", account_name="EXACT-INST"),
        _rule(3, "ip", "10.0.*", "pattern", account_name="PAT-SHORT"),
        _rule(4, "ip", "10.0.0.*", "pattern", account_name="PAT-LONG"),
    ]
    hit = own.resolve(rules, instance="i-aaa", name="web-01")
    check("E1 exact 命中且键优先级 instance>name", hit and hit["account_name"] == "EXACT-INST",
          str(hit))
    hit = own.resolve(rules, name="web-01")
    check("E2 pattern 命中（无 exact 时）", hit and hit["account_name"] == "PAT-NAME", str(hit))
    hit = own.resolve(rules, ips=["10.0.0.9:8080"])
    check("E3 长 pattern 更具体优先", hit and hit["account_name"] == "PAT-LONG", str(hit))
    hit = own.resolve(rules, domain="no.such.com")
    check("E4 无命中返回 None（不猜）", hit is None, str(hit))
    hit = own.resolve(rules, instance="i-bbb", ips=["10.0.0.9"])
    check("E5 键优先级 ip>未登记 instance？否——instance 先查但无命中则 ip 命中",
          hit and hit["account_name"] == "PAT-LONG", str(hit))


# ================= G. API 链路 =================
def _make_req():
    """ASGI 直连请求壳（不触发 lifespan/startup；沿用 test_integrations_mock 套路）。

    starlette TestClient 与 httpx 0.28 不兼容（app= 参数已移除）；用
    ASGITransport + 同步壳。返回 _req(method, url, **kw)。
    """
    import asyncio

    import httpx

    import app.core.deps as deps

    fake = types.SimpleNamespace(id=1, username="smoke", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: fake

    def _req(method, url, **kw):
        async def _go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(_go())

    return _req


def test_api() -> None:
    print("G. API 链路（ASGI 直连，不起后台任务）")
    _req = _make_req()
    try:
        r = _req("GET", "/api/ownership/options")
        check("G1 options 可用", r.status_code == 200 and r.json()["key_types"], r.text[:200])
        r = _req("POST", "/api/ownership/rules", json={
            "key_type": "ip", "key_value": "9.9.9.9:443", "match_type": "exact",
            "vendor": "onprem", "account_name": "办公出口", "business": "办公网"})
        check("G2 新建登记（IP:port 归一入库）", r.status_code == 200
              and r.json()["key_value"] == "9.9.9.9", r.text[:200])
        rid = r.json()["id"]
        r = _req("POST", "/api/ownership/rules", json={
            "key_type": "ip", "key_value": "9.9.9.9", "match_type": "exact",
            "vendor": "onprem", "account_name": "重复"})
        check("G3 重复键拒绝（幂等登记）", r.status_code == 400, r.text[:200])
        r = _req("GET", "/api/ownership/preview", params={"key_type": "ip", "key_value": "9.9.9.9:443"})
        j = r.json()
        check("G4 试算命中登记", r.status_code == 200 and j["hit"]
              and j["hit"]["account_name"] == "办公出口", r.text[:200])
        r = _req("GET", "/api/ownership/preview", params={"key_type": "ip", "key_value": "1.2.3.4"})
        j = r.json()
        hint_refs = " ".join(h.get("ref", "") for h in j["hints"])
        check("G5 试算给识别线索（IP→主机）", r.status_code == 200 and "web-01" in hint_refs,
              r.text[:300])
        r = _req("GET", "/api/ownership/unattributed")
        vals = [x["key_value"] for x in r.json()]
        check("G6 未归属清单接口可用且不含已配置 IP", r.status_code == 200
              and "9.9.9.9" not in vals and "1.2.3.4" not in vals, str(vals))
        r = _req("GET", "/api/topology/graph")
        nm = node_map(r.json())
        check("G7 回流到 /topology/graph", nm["eip:1.2.3.4"]["meta"].get("account") == "入口账号")
        r = _req("PUT", f"/api/ownership/rules/{rid}", json={
            "key_type": "ip", "key_value": "9.9.9.9", "match_type": "exact",
            "vendor": "onprem", "account_name": "办公出口-改", "business": "办公网"})
        check("G8 编辑登记", r.status_code == 200 and r.json()["account_name"] == "办公出口-改")
        r = _req("DELETE", f"/api/ownership/rules/{rid}")
        check("G9 删除登记", r.status_code == 200 and r.json()["ok"])
        # 回流可撤销：对图上真实实体登记→从未归属消失；删除→回到未归属
        r = _req("POST", "/api/ownership/rules", json={
            "key_type": "domain", "key_value": "ghost.demo.com", "match_type": "exact",
            "vendor": "onprem", "account_name": "IDC 机房"})
        gid = r.json()["id"]
        r = _req("GET", "/api/ownership/unattributed")
        vals = [x["key_value"] for x in r.json()]
        check("G10a 登记后从未归属清单消失", "ghost.demo.com" not in vals, str(vals))
        _req("DELETE", f"/api/ownership/rules/{gid}")
        r = _req("GET", "/api/ownership/unattributed")
        vals = [x["key_value"] for x in r.json()]
        check("G10b 删除后回到未归属清单（回流可撤销）", "ghost.demo.com" in vals, str(vals))
    finally:
        app.dependency_overrides.clear()


def _row(items: list, **where):
    return next((x for x in items if all(x.get(k) == v for k, v in where.items())), None)


# ================= H. 五展示面回流 =================
def test_surfaces() -> None:
    print("H. 五展示面归属回流（资产/多云/成本/告警/到期）")
    with SessionLocal() as db:
        # 五面数据种子：i-hhh=待补位（account_id=99 台账外）、i-zzz=未登记对照、
        # i-ggg=绑定云账号对照、i-www=告警"已有账号名不覆盖"语义对照
        db.add(CloudAccount(id=2, name="bound-acc", vendor="tencent",
                            ak_cipher="", secret_cipher="", enabled=True, status="ok"))
        db.add(Asset(product="ecs", instance_id="i-hhh", name="reg-01", account_id=99,
                     status="Running", expire_time=datetime.now() + timedelta(days=10),
                     extra=json.dumps({"ip_public": "5.6.7.8"})))
        db.add(Asset(product="ecs", instance_id="i-zzz", name="ctl-01", account_id=1,
                     status="Running", expire_time=datetime.now() + timedelta(days=10),
                     extra="{}"))
        db.add(Asset(product="ecs", instance_id="i-ggg", name="bind-01", account_id=1,
                     status="Running", extra="{}"))
        db.add(Asset(product="ecs", instance_id="i-www", name="ovr-01", account_id=1,
                     status="Running", extra="{}"))
        d = date.today() - timedelta(days=1)
        for iid, nm, aid, amt in (("i-hhh", "reg-01", 99, 3.0), ("i-zzz", "ctl-01", 1, 2.0)):
            db.add(CostDaily(stat_date=d, source="bill", product_code="ecs",
                             product_name="云服务器ECS", instance_id=iid, instance_name=nm,
                             subscription_type="PayAsYouGo", pretax_amount=amt, account_id=aid))
        for tgt, nm, aid in (("i-hhh", "reg-01", 99), ("i-zzz", "ctl-01", 1), ("i-www", "ovr-01", 1)):
            db.add(AlertRecord(account_id=aid, rule_id=1, rule_name="r1", source="metric",
                               target=tgt, target_name=nm, level="warning",
                               title=f"t-{tgt}", content="c"))
        db.add(ResourcePackSnapshot(account_id=1, snapshot_date=d, instance_id="pkg-sss",
                                    commodity_code="OSSBAG", package_type="StoragePackage",
                                    remaining_pct=50.0, status="Available",
                                    effective_time="2026-01-01", expiry_time="2027-06-30"))
        db.commit()

    _req = _make_req()
    try:
        # ---- 登记前：空缺保持空缺 + 未登记行原状快照（红线：不造数据）----
        items = _req("GET", "/api/assets", params={"product": "ecs"}).json()["items"]
        hhh, zzz = _row(items, instance_id="i-hhh"), _row(items, instance_id="i-zzz")
        check("H0a 登记前 i-hhh 归属空缺（待补位）",
              not hhh["account_name"] and not hhh["vendor"], str(hhh))
        zzz_before = (zzz["vendor"], zzz["account_name"])
        recs = _req("GET", "/api/alerts/records").json()["items"]
        check("H0b 登记前告警 i-hhh account_name 空缺",
              _row(recs, target="i-hhh")["account_name"] == "", str(_row(recs, target="i-hhh")))
        check("H0c 登记前告警 i-zzz 采集账号名已在", _row(recs, target="i-zzz")["account_name"] == "prod-acc")
        exp = _req("GET", "/api/expiry/items").json()["items"]
        check("H0d 登记前到期 i-hhh 归属空缺",
              not _row(exp, instance_id="i-hhh")["account_name"], str(_row(exp, instance_id="i-hhh")))
        ov = _req("GET", "/api/multi-cloud/overview").json()["accounts"]
        ov_before = {x["name"]: x for x in ov}
        check("H0e 登记前多云无登记账号行（不造行）",
              "S-账号A" not in ov_before and "bound-acc" not in ov_before, str(list(ov_before)))

        # ---- 登记：自由文本账号 / 绑定云账号台账 / 资源包 ----
        with SessionLocal() as db:
            db.add(OwnershipRule(key_type="instance", key_value="i-hhh", match_type="exact",
                                 vendor="tencent", account_name="S-账号A"))
            db.add(OwnershipRule(key_type="instance", key_value="pkg-sss", match_type="exact",
                                 account_name="S-账号C"))
            db.add(OwnershipRule(key_type="instance", key_value="i-ggg", match_type="exact",
                                 account_id=2))
            db.add(OwnershipRule(key_type="instance", key_value="i-www", match_type="exact",
                                 account_name="S-账号W"))
            db.commit()

        # ---- 登记后：五面回流 ----
        items = _req("GET", "/api/assets", params={"product": "ecs"}).json()["items"]
        hhh, zzz, ggg = (_row(items, instance_id=x) for x in ("i-hhh", "i-zzz", "i-ggg"))
        check("H1a 资产列表回流（自由文本账号）",
              hhh["account_name"] == "S-账号A" and hhh["vendor"] == "tencent", str(hhh))
        check("H1b 资产列表回流（绑定云账号=台账同名）",
              ggg["account_name"] == "bound-acc" and ggg["vendor"] == "tencent", str(ggg))
        check("H1c 资产列表未登记行原状",
              (zzz["vendor"], zzz["account_name"]) == zzz_before, str(zzz))
        detail = _req("GET", f"/api/assets/{hhh['id']}").json()
        check("H2 资产详情同口径回流",
              detail["account_name"] == "S-账号A" and detail["vendor"] == "tencent", str(detail))

        ov = {x["name"]: x for x in _req("GET", "/api/multi-cloud/overview").json()["accounts"]}
        a, w, b = ov.get("S-账号A", {}), ov.get("S-账号W", {}), ov.get("bound-acc", {})
        check("H3a 多云补行（自由文本账号：资产/费用/告警聚合）",
              a.get("id", 0) < 0 and a.get("assets_by_product") == {"ecs": 1}
              and a.get("cost_7d") == 3.0 and a.get("alerts_total") == 1
              and a.get("vendor") == "tencent" and a.get("kind") == "cloud", str(a))
        check("H3b 多云补行（绑定台账：厂商/类型如实）",
              b.get("id") == 2 and b.get("vendor") == "tencent"
              and b.get("assets_by_product") == {"ecs": 1}, str(b))
        check("H3c 多云补行（告警按登记归属聚合）",
              w.get("assets_by_product") == {"ecs": 1} and w.get("alerts_total") == 1, str(w))
        check("H3d 多云默认账号行=未登记原状（登记成员迁出后余量）",
              ov["默认账号"]["assets_total"] == ov_before["默认账号"]["assets_total"] - 2,
              f"{ov_before['默认账号']['assets_total']} -> {ov['默认账号']['assets_total']}")

        daily = _req("GET", "/api/cost/daily").json()["items"]
        check("H4a 成本明细回流/未登记原状",
              _row(daily, instance_id="i-hhh")["account_name"] == "S-账号A"
              and _row(daily, instance_id="i-zzz")["account_name"] == "prod-acc",
              str(_row(daily, instance_id="i-hhh")))
        inst = _req("GET", "/api/cost/instances").json()["items"]
        check("H4b 成本实例行回流",
              _row(inst, instance_id="i-hhh")["account_name"] == "S-账号A", str(_row(inst, instance_id="i-hhh")))
        drill = _req("GET", "/api/cost/drilldown").json()
        check("H4c 成本下钻实例行回流",
              _row(drill["instances"], instance_id="i-hhh")["account_name"] == "S-账号A")
        check("H4d 成本下钻产品聚合行无实体键不解析（不猜，原样空）",
              _row(drill["products"], account_id=99)["account_name"] == ""
              and _row(drill["products"], account_id=99)["vendor"] == "")
        packs = _req("GET", "/api/cost/packs").json()["items"]
        check("H4e 资源包行回流（登记覆盖采集账号名）",
              _row(packs, instance_id="pkg-sss")["account_name"] == "S-账号C",
              str(_row(packs, instance_id="pkg-sss")))

        recs = _req("GET", "/api/alerts/records").json()["items"]
        check("H5a 告警 account_name 空缺补位回流",
              _row(recs, target="i-hhh")["account_name"] == "S-账号A", str(_row(recs, target="i-hhh")))
        check("H5b 告警已有采集账号名不覆盖（空则补语义）",
              _row(recs, target="i-www")["account_name"] == "prod-acc"
              and _row(recs, target="i-zzz")["account_name"] == "prod-acc", str(_row(recs, target="i-www")))

        exp = _req("GET", "/api/expiry/items").json()["items"]
        e_hhh, e_zzz, e_pkg = (_row(exp, instance_id=x) for x in ("i-hhh", "i-zzz", "pkg-sss"))
        check("H6a 到期资产行回流",
              e_hhh["account_name"] == "S-账号A" and e_hhh["vendor"] == "tencent", str(e_hhh))
        check("H6b 到期资源包行回流",
              e_pkg["account_name"] == "S-账号C", str(e_pkg))
        check("H6c 到期未登记行原状",
              e_zzz["account_name"] == "prod-acc" and e_zzz["vendor"] == "aliyun", str(e_zzz))
    finally:
        app.dependency_overrides.clear()


# ================= I. biz_identifiers ip/instance 键 =================
def test_biz_keys() -> None:
    print("I. 业务登记 ip/instance 键 + 业务挂接一处收口 + 迁移幂等")
    _req = _make_req()
    try:
        r = _req("POST", "/api/links/businesses", json={"name": "biz-IP", "code": "bip"})
        check("I1 创建业务线", r.status_code == 200 and r.json().get("id"), r.text[:200])
        bid = r.json()["id"]
        r = _req("POST", f"/api/links/businesses/{bid}/identifiers",
                 json={"ident_type": "ip", "ident_value": "8.8.8.8:53", "note": ""})
        check("I2 ip 键挂接（IP:port 归一入库）",
              r.status_code == 200 and r.json()["ident_value"] == "8.8.8.8", r.text[:200])
        r = _req("POST", f"/api/links/businesses/{bid}/identifiers",
                 json={"ident_type": "ip", "ident_value": "8.8.8.8", "note": ""})
        check("I3 归一后去重（幂等挂接）", r.status_code == 409, r.text[:200])
        r = _req("POST", f"/api/links/businesses/{bid}/identifiers",
                 json={"ident_type": "instance", "ident_value": "I-HHH", "note": ""})
        check("I4 instance 键挂接（大小写归一）",
              r.status_code == 200 and r.json()["ident_value"] == "i-hhh", r.text[:200])
        r = _req("POST", f"/api/links/businesses/{bid}/identifiers",
                 json={"ident_type": "foo", "ident_value": "x", "note": ""})
        check("I5 非法 ident_type 拒绝", r.status_code == 422, r.text[:200])
    finally:
        app.dependency_overrides.clear()

    with SessionLocal() as db:
        hints = own.business_hint_map(db)
        check("I6 业务挂接收口：hint map 含 ip/instance 键",
              hints.get(("ip", "8.8.8.8")) == ["biz-IP"]
              and hints.get(("instance", "i-hhh")) == ["biz-IP"], str(hints))
        check("I7 instance 键业务解析（归一命中）",
              own.business_from_hints(hints, instance="I-HHH") == "biz-IP")
        check("I8 ip 键业务解析（IP:port 归一命中）",
              own.business_from_hints(hints, ips=["8.8.8.8:53"]) == "biz-IP")
        check("I9 告警 biz 标签命中 ip（URL 目标按 IP 边界匹配）",
              "biz-IP" in resolve_biz(db, target="http://8.8.8.8:53/dns"))
        check("I10 ip 边界不误伤（11.8.8.80 不含 8.8.8.8）",
              "biz-IP" not in resolve_biz(db, target="11.8.8.80"))
        check("I11 告警 biz 标签命中 instance 键",
              "biz-IP" in resolve_biz(db, target="i-hhh"))
        meta = node_map(build_graph(db)).get("ecs:i-hhh", {}).get("meta", {})
        check("I12 业务挂接经收口回流拓扑（hint 互补，登记优先）",
              meta.get("business") == "biz-IP", str(meta))
        n_biz = db.query(BizIdentifier).count()
        n_own = db.query(OwnershipRule).count()

    # 迁移幂等（ident_type 为无约束 VARCHAR(32)，无 DDL 需求；
    # 两遍 create_all+run_migrations 后数据与新键挂接不受影响）
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    with SessionLocal() as db:
        types_left = {t for (t,) in db.query(BizIdentifier.ident_type).all()}
        check("I13 两遍迁移后数据无损且新键仍在",
              db.query(BizIdentifier).count() == n_biz
              and db.query(OwnershipRule).count() == n_own
              and {"ip", "instance"} <= types_left, str(types_left))


if __name__ == "__main__":
    test_migration()
    test_flow()
    test_resolve()
    test_api()
    test_surfaces()
    test_biz_keys()
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
