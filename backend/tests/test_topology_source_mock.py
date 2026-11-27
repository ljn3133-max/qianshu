#!/usr/bin/env python3
"""拓扑按数据源分版 mock 测试。

隔离口径：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为一次性 PostgreSQL，
不碰生产库、不调任何真实云 API、不碰 8810 进程。

验证项：
  A 组件识别：tc_mongodb/tc_es/tc_postgres/tc_cdn 有 kind/role，且进入图（防止被静默丢弃）；
    ssl_cert 不成为节点，domain 实证节点存在；
  B 聚合键：任意视图任意组 members 不跨 source/account；同名组件跨云 -> 两个 gid；
  C 单源视图：source=aliyun 不含腾讯资产，腾讯对端落 g:ext:* 外部占位，边 cross_source=true；
    未归属公共入口（域名）在同源有边时保留且不算跨源；
  D sources 索引：全量返回、计数与实际分布一致；mixed_groups_eliminated > 0；
  E 脱敏：分组成立、账号名/资产名置空、id 序号化且组键一致；
  F 扫描签名：旧裸 dict 基线 -> 只重建不刷屏（note），新基线 version=2；
  G API：?source= 生效；非法 source 返回 400 并列出合法键。

用法：python3 tests/test_topology_source_mock.py   （退出码 0=全过）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

TMPDIR = tempfile.mkdtemp(prefix="qianshu-topo-source-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ.setdefault("MASTER_KEY", "0123456789abcdef0123456789abcdef")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.models.topology_scan import TopologyScan  # noqa: E402
from app.services.topology import (  # noqa: E402
    SIGNATURE_VERSION, SOURCE_ALL, _group_signature, build_arch, build_graph,
    classify_asset, run_scan, valid_sources)
import app.main  # noqa: E402,F401  全量注册模型后再建表（业务标识/变更等联查不因缺表炸事务）

Base.metadata.create_all(engine)
db = SessionLocal()

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 造数：三朵云 + 未归属 ----------------
ali = CloudAccount(name="阿里生产", vendor="aliyun", region="cn-hangzhou")
tc = CloudAccount(name="腾讯生产", vendor="tencent", region="ap-guangzhou")
vc = CloudAccount(name="内网vCenter", vendor="vcenter",
                  extra_json=json.dumps({"datacenter": "DC1"}))
db.add_all([ali, tc, vc])
db.commit()


def asset(acc_id, prod, iid, name, region="", zone="", extra=None):
    return Asset(account_id=acc_id, product=prod, instance_id=iid, name=name,
                 region=region, zone=zone, status="running",
                 extra=json.dumps(extra or {}))


db.add_all([
    # 阿里云：ECS 同名组件 + RDS + SLB（后端指向腾讯 CVM -> 制造跨云边）
    asset(ali.id, "ecs", "i-ali-web", "order-ecs", "cn-hangzhou", "",
          {"ip_internal": "10.0.0.1", "vpc_id": "vpc-a"}),
    asset(ali.id, "ecs", "i-ali-web2", "order-ecs", "cn-hangzhou", "",
          {"ip_internal": "10.0.0.2", "vpc_id": "vpc-a"}),
    asset(ali.id, "ecs", "i-ali-api", "ali-api", "cn-hangzhou", "",
          {"ip_internal": "10.0.0.3"}),
    asset(ali.id, "rds", "rds-1", "order-mysql", "cn-hangzhou"),
    asset(ali.id, "slb", "lb-1", "web-slb", "cn-hangzhou", "",
          {"backends": [{"ip": "10.2.0.5", "port": 80, "weight": 100}]}),
    # 腾讯云：同名 CVM/CDB + 四个曾被静默丢弃的 tc_* 产品码
    asset(tc.id, "cvm", "ins-tc-web", "order-ecs", "ap-guangzhou", "",
          {"ip_internal": "10.2.0.5"}),
    asset(tc.id, "cdb", "cdb-1", "order-mysql", "ap-guangzhou"),
    asset(tc.id, "tc_mongodb", "m-1", "mongo-1", "ap-guangzhou"),
    asset(tc.id, "tc_es", "es-1", "es-log", "ap-guangzhou"),
    asset(tc.id, "tc_postgres", "pg-1", "pg-1", "ap-guangzhou"),
    asset(tc.id, "tc_cdn", "d-1", "cdn-1"),
    # 私有云：与阿里云 ECS 同名（旧聚合键会混框）
    asset(vc.id, "vmware_vm", "vm-1", "order-ecs", zone="clusterA",
          extra={"cluster": "clusterA", "guest_ip": "10.1.0.1"}),
    # 台账外资产：必须落未归属
    Asset(account_id=0, product="vmware_vm", instance_id="vm-ghost",
          name="ghost", region="", zone="", status="running", extra="{}"),
    # 证书不是拓扑成员；域名资产 + 实证链路生成域名节点
    asset(ali.id, "ssl_cert", "cert1", "cert1", "global"),
    asset(ali.id, "domain", "api.example.com", "api.example.com", "global"),
])
db.add(LinkEdge(chain="outer", host="api.example.com", upstream_addr="10.0.0.3",
                ecs_name="ali-api", req_count=100, status="confirmed"))
db.commit()

# ---------------- A 组件识别 ----------------
print("A 组件识别")
check("tc_mongodb -> db", classify_asset("tc_mongodb", "mongo-1", "tencent")[0] == "db")
check("tc_postgres -> db", classify_asset("tc_postgres", "pg-1", "tencent")[0] == "db")
check("tc_es -> bigdata/搜索引擎", classify_asset("tc_es", "es-log", "tencent")[:2] == ("bigdata", "搜索引擎"))
check("tc_cdn -> gateway", classify_asset("tc_cdn", "cdn-1", "tencent")[0] == "gateway")

g_all = build_graph(db, source=SOURCE_ALL)
types_by_id = {n["id"]: n.get("type") for n in g_all["nodes"]}
for iid in ("tc_mongodb:m-1", "tc_es:es-1", "tc_postgres:pg-1", "tc_cdn:d-1"):
    check(f"{iid} 进入图（不再静默丢弃）", iid in types_by_id)
check("domain 实证节点存在", "domain:api.example.com" in types_by_id)
check("ssl_cert 不成为节点", not any("cert1" in i for i in types_by_id),
      str([i for i in types_by_id if "cert" in i]))

# ---------------- B 聚合键不混云/账号 ----------------
print("B 聚合键不混云/账号")
arch_all = build_arch(db, source=SOURCE_ALL)
by_gid = {g["id"]: g for g in arch_all["groups"]}
check("同名 ECS 跨三云 -> 三个 gid",
      all(k in by_gid for k in ("g:aliyun:%d:app:order-ecs:all" % ali.id,
                                "g:tencent:%d:app:order-ecs:all" % tc.id,
                                "g:private:%d:app:order-ecs:all" % vc.id)),
      str([k for k in by_gid if "order-ecs" in k]))
mixed = []
for g in arch_all["groups"]:
    srcs = {(m.get("meta") or {}).get("source") for m in g["members"]}
    aids = {(m.get("meta") or {}).get("account_id") for m in g["members"]}
    if len(srcs) > 1 or len(aids) > 1:
        mixed.append(g["id"])
check("任意组 members 不跨 source/account", not mixed, str(mixed))
ali_db = [g for g in arch_all["groups"] if g["source"] == "aliyun" and g["role"] == "db"]
check("阿里云数据库独立成框（不并入腾讯）",
      len(ali_db) == 1 and ali_db[0]["count"] == 1, str(ali_db))
ali_app = [g for g in arch_all["groups"] if g["source"] == "aliyun" and g["role"] == "app"]
check("同云同名主机合并（阿里 order-ecs 单框 2 台）",
      any(g["label"] == "order-ecs" and g["count"] == 2 for g in ali_app), str(ali_app))
check("账号标签不再出现『N 个账号混合』",
      not any("混合" in (g.get("account_label") or "") for g in arch_all["groups"]))
check("组字段齐全", all({"source", "source_label", "account_id", "account",
                        "regions", "container"} <= set(g) for g in arch_all["groups"]))
check("私有云地域回退到集群", any(g["regions"] == [{"region": "clusterA", "count": 1}]
                                 for g in arch_all["groups"] if g["source"] == "private"))

# ---------------- C 单源视图 ----------------
print("C 单源视图")
arch_ali = build_arch(db, source="aliyun")
ids_ali = {g["id"] for g in arch_ali["groups"]}
check("aliyun 视图不含腾讯实体 gid（ext 占位除外）",
      not any(i.startswith("g:tencent:") for i in ids_ali), str(ids_ali))
check("aliyun 视图含阿里 gid", any(":aliyun:" in i for i in ids_ali))
ext = [g for g in arch_ali["groups"] if g["external"]]
check("腾讯对端落外部占位 g:ext:tencent:*", any(g["id"] == "g:ext:tencent:app" for g in ext),
      str([g["id"] for g in ext]))
check("外部占位无成员、标 external", all(g["count"] >= 1 and not g["members"] and g["external"] for g in ext))
cross = [l for l in arch_ali["links"] if l.get("cross_source")]
check("跨云边标 cross_source 并带标签",
      bool(cross) and all(l["cross_source_label"] for l in cross), str(arch_ali["links"]))
check("外部占位图例标签为『腾讯云』",
      any(g["source_label"] == "腾讯云" for g in ext))
# 公共入口：域名与阿里云 ECS 有边 -> 保留且不算跨源
check("未归属域名在 ali 视图保留", any(g["source"] == "unassigned" for g in arch_ali["groups"]),
      str([(g["id"], g["source"]) for g in arch_ali["groups"]]))
check("公共入口边不标跨源",
      not any(l["cross_source"] for l in arch_ali["links"]
              if l["source"].startswith("g:unassigned:")))

arch_tc = build_arch(db, source="tencent")
check("tencent 视图含四个 tc_* 组件",
      sum(1 for g in arch_tc["groups"] if g["source"] == "tencent") >= 5,
      str([g["id"] for g in arch_tc["groups"]]))

# ---------------- D sources 索引 / 混框统计 ----------------
print("D sources 索引")
counts = {s["key"]: s["count"] for s in arch_all["sources"]}
check("sources 返回全量且含五桶",
      {"all", "aliyun", "tencent", "private", "unassigned"} <= set(counts), str(counts))
check("sources.all = 其它桶之和",
      counts["all"] == sum(v for k, v in counts.items() if k != "all"), str(counts))
check("汇总排第一", arch_all["sources"][0]["key"] == "all")
check("mixed_groups_eliminated > 0（造了跨云同名组件）",
      arch_all["stats"]["mixed_groups_eliminated"] > 0,
      str(arch_all["stats"]))
check("单源视图也返回全量 sources（页签不缩水）",
      {s["key"] for s in arch_ali["sources"]} == {s["key"] for s in arch_all["sources"]})

# ---------------- E 脱敏 ----------------
print("E 脱敏")
arch_r = build_arch(db, redact=True, source=SOURCE_ALL)
check("脱敏后仍分云", any(g["source"] == "aliyun" for g in arch_r["groups"]))
check("脱敏后账号名置空", all(g["account"] == "" for g in arch_r["groups"]))
asset_members = [m for g in arch_r["groups"] for m in g["members"]
                 if (m.get("meta") or {}).get("product")]
check("脱敏后资产名置空", all(m["meta"].get("name") == "" for m in asset_members))
check("脱敏后实例 ID 置空", all(m["meta"].get("instance_id") == "" for m in asset_members))
check("脱敏后 id 序号化且成员可回指组",
      all(m["id"].startswith("n") and g["id"].count(":") == 5
          for g in arch_r["groups"] for m in g["members"]))

# ---------------- F 扫描签名升级 ----------------
print("F 扫描签名升级")
old = TopologyScan(scanned_at=__import__("datetime").datetime.now(), trigger="manual",
                   groups=1, links=0, members=1, duration_ms=1,
                   diff_json="{}",
                   synced_json=json.dumps({"_signature": {"g:app:x:all": {"label": "x", "role": "app", "count": 1}}}))
db.add(old)
db.commit()
res = run_scan(db, trigger="test", full=False)
check("旧裸 dict 基线 -> note 重建", "重建基线" in (res.get("diff") or {}).get("note", ""),
      str(res.get("diff")))
last = db.query(TopologyScan).order_by(TopologyScan.id.desc()).first()
sig = json.loads(last.synced_json)["_signature"]
check("新基线带 version=2", sig.get("version") == SIGNATURE_VERSION and "groups" in sig)
res2 = run_scan(db, trigger="test", full=False)
check("同版本二次扫描 diff 平静", not (res2.get("diff") or {}).get("added")
      and not (res2.get("diff") or {}).get("removed"), str(res2.get("diff")))

# ---------------- G API ----------------
print("G API")
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
import httpx  # noqa: E402

STUB = SimpleNamespace(id=1, username="tester", role="admin", is_active=True)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_user] = lambda: STUB


def _get(url: str):
    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            return await c.get(url)
    return asyncio.run(_go())


r = _get("/api/topology/arch?source=aliyun")
body = r.json()
check("GET /topology/arch?source=aliyun 200", r.status_code == 200)
check("API 透传 source", body.get("source") == "aliyun")
check("API 组不跨云",
      not any(g["id"].startswith("g:tencent:") for g in body.get("groups", [])))
check("API 带 sources/mixed 统计",
      isinstance(body.get("sources"), list)
      and "mixed_groups_eliminated" in (body.get("stats") or {}))
r2 = _get("/api/topology/graph?source=tencent")
check("GET /topology/graph?source=tencent 200", r2.status_code == 200)
r3 = _get("/api/topology/arch?source=not-a-cloud")
detail = r3.json().get("detail", "") if r3.headers.get("content-type", "").startswith("application/json") else ""
check("非法 source -> 400", r3.status_code == 400, str(r3.status_code))
check("400 列出合法键", all(k in detail for k in ("all", "aliyun", "tencent", "private", "unassigned")),
      detail)
check("valid_sources 不含私有厂商裸键",
      not ({"esxi", "kvm", "onprem"} & valid_sources()), str(sorted(valid_sources())))

db.close()
print(f"\n{'=' * 60}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
