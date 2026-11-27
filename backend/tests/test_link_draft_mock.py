#!/usr/bin/env python3
"""链路边规则感知隔离冒烟：skipped/busy 文案、规则边建模、
幂等迁移、多云转发规则拉取、前端渲染文案——全 mock，不碰真实库/真实云/线上进程。

隔离口径（与 test_cloud_migrate_smoke.py 同款）：DATABASE_URL/MASTER_KEY 在 import
app.* 之前改写为临时值——不碰真实 qianshu.db、不调任何真实云 API、不碰 8810 进程。

验证项：
  A skipped：SLS 目标未配置 -> {skipped, message}，文案含配置指引，无 edge_count 字段；
    仅配应用日志不配 SLB 目标同样跳过（硬跑会把既有草稿删空的回归守卫）；
  B busy：生成中再点 -> {busy, message}；
  C 正常分支：规则边粒度（host × 路径 × 后端）建模 + 每边独立指标 + 二级路径证据
    + 野域名剔除 + 重刷幂等 + confirmed 状态保留；
  D 旧粒度行（path_pattern=''）状态继承：就地升级保 id/状态，拆出的后续规则边继承状态，
    无流量旧边原样保留；
  E 云转发规则：命中规则贴 rule_name；「有规则无流量」补 rule_source='cloud' 规则边；
  F 幂等迁移：旧表重建（旧行不丢、新列默认值、新唯一约束生效、二跑 no-op）；
  G 多云转发规则拉取：阿里 DescribeRules/DescribeVServerGroups 系 + 腾讯
    DescribeListeners/DescribeTargets（SDK 方法名 introspection 核对 + mock 断言）；
  H _edge_out 新字段透出；
  I 前端 formatDraftResult 渲染文案（从 LinksDraft.vue 抽源码跑 node）：skipped/busy
    不再出现 undefined。

用法：python3 tests/test_link_draft_mock.py   （退出码 0=全过）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置 ----
TMPDIR = tempfile.mkdtemp(prefix="qianshu-link-mock-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ.setdefault("MASTER_KEY", "0123456789abcdef0123456789abcdef")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine as app_engine  # noqa: E402
from app.core.migrate import migrate_link_edges_rule_dim  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.services import link_draft as LD  # noqa: E402
from app.services.cloud import provider as P  # noqa: E402

Base.metadata.create_all(app_engine)
DB = SessionLocal

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- mock SLS / VM / 凭证 ----------------

class FakeSLS:
    """按 query 形态分发的假 SLS（全部只读语义，无网络）。"""

    def __init__(self):
        self.ok_rows = []      # host 状态码画像
        self.main_rows = []    # host × upstream × pp 主聚合
        self.sub_rows = []     # 二级路径
        self.app_total = []    # AppName 总量
        self.app_err = []      # AppName ERROR
        self.calls = 0

    def __call__(self, ak, sk, project, logstore, query, start, end, limit=20, **kw):
        self.calls += 1
        if "AppName" in query:
            rows = self.app_err if "LogLevel: ERROR" in query else self.app_total
        elif "as pp2" in query:
            rows = self.sub_rows
        elif "count_if" in query:
            rows = self.main_rows
        elif "http_host, status" in query:
            rows = self.ok_rows
        else:
            rows = []
        return {"logs": [dict(r) for r in rows], "is_sql": True, "count": len(rows)}


def install_env(fake: FakeSLS, slb='[["proj","slb-log","outer"]]', app_log='["proj","app-log"]'):
    settings.SLS_SLB_TARGETS = slb
    settings.SLS_APP_LOG = app_log
    LD.sls_query_logs = fake
    LD.vm_query_range = lambda *a, **k: []
    import app.collector.credential as cred
    cred.resolve_aliyun_credential = lambda db: ("ak-mock", "sk-mock", "mock")
    cred.resolve_sls_credential = lambda db: ("ak-mock", "sk-mock", "mock")


def wipe(db):
    db.query(LinkEdge).delete()
    db.commit()


def main_rows_default():
    """api.example.com 三条规则边 + 一条野域名边（应被剔除）+ 一条坏 upstream（应跳过）。"""
    return [
        dict(http_host="api.example.com", upstream_addr="10.0.0.1:8080", pp="/api",
             cnt=1000, c5xx=5, c4xx=10, rt=0.05),
        dict(http_host="api.example.com", upstream_addr="10.0.0.2:8080", pp="/static",
             cnt=300, c5xx=0, c4xx=1, rt=0.01),
        dict(http_host="api.example.com", upstream_addr="10.0.0.3:8080", pp="/",
             cnt=200, c5xx=0, c4xx=0, rt=0.02),
        dict(http_host="bot.example.com", upstream_addr="10.0.0.4:8080", pp="/",
             cnt=900, c5xx=0, c4xx=0, rt=0.001),
        dict(http_host="api.example.com", upstream_addr="55259", pp="/api",
             cnt=7, c5xx=7, c4xx=0, rt=0.0),
    ]


def ok_rows_default():
    return [
        dict(http_host="api.example.com", status="200", cnt=1200),
        dict(http_host="api.example.com", status="404", cnt=50),
        dict(http_host="bot.example.com", status="200", cnt=10),
        dict(http_host="bot.example.com", status="404", cnt=90),
    ]


def sub_rows_default():
    return [
        dict(http_host="api.example.com", upstream_addr="10.0.0.1:8080", pp2="/api/users", cnt=600),
        dict(http_host="api.example.com", upstream_addr="10.0.0.1:8080", pp2="/api/orders", cnt=400),
        dict(http_host="api.example.com", upstream_addr="10.0.0.2:8080", pp2="/static/img", cnt=300),
    ]


# ==================== A：skipped ====================

def test_skipped():
    print("== A skipped（SLS 未配置） ==")
    db = DB()
    fake = FakeSLS()
    install_env(fake, slb='', app_log='')
    r = LD.generate_link_draft(db)
    check("A skipped 返回 skipped 字段", r.get("skipped") == "SLS 目标未配置", str(r))
    check("A 无 edge_count（前端不会拼 undefined）", "edge_count" not in r, str(r))
    msg = r.get("message", "")
    check("A message 含安装向导指引", "安装向导" in msg, msg)
    check("A message 含 SLS_SLB_TARGETS 配置键", "SLS_SLB_TARGETS" in msg, msg)
    check("A status 记录 last_result", LD.get_status().get("last_result", {}).get("skipped") == "SLS 目标未配置")

    # 只配应用日志不配 SLB 目标：同样跳过（回归守卫——硬跑会把既有草稿删空）
    install_env(fake, slb='', app_log='["proj","app-log"]')
    r2 = LD.generate_link_draft(db)
    check("A 仅应用日志也跳过（护住草稿不被删空）", r2.get("skipped") == "SLS 目标未配置", str(r2))
    db.close()


# ==================== B：busy ====================

def test_busy():
    print("== B busy ==")
    db = DB()
    fake = FakeSLS()
    install_env(fake)
    LD._lock.acquire()
    try:
        r = LD.generate_link_draft(db)
        check("B 返回 busy", r.get("busy") is True, str(r))
        check("B message 可行动", "稍候" in r.get("message", ""), str(r))
    finally:
        LD._lock.release()
    db.close()


# ==================== C：正常分支（规则边建模） ====================

def test_generate_rule_edges():
    print("== C 规则边建模（host × 路径 × 后端） ==")
    db = DB()
    wipe(db)
    fake = FakeSLS()
    fake.main_rows = main_rows_default()
    fake.ok_rows = ok_rows_default()
    fake.sub_rows = sub_rows_default()
    fake.app_total = [dict(AppName="api.example.com", cnt=100)]
    install_env(fake)
    LD._fetch_cloud_rules = lambda db_: []   # 本场景不带云规则

    r = LD.generate_link_draft(db)
    check("C edge_count=3（坏 upstream/野域名不入边）", r.get("edge_count") == 3, str(r))
    check("C 野域名剔除 1 个", r.get("filtered_host_count") == 1
          and r.get("filtered_hosts_top") == ["bot.example.com"], str(r))
    check("C 坏 upstream 跳过计数", r.get("skipped_no_upstream") == 1, str(r))
    check("C message 人话", r.get("message", "").startswith("草稿已生成：3 条边"), r.get("message"))

    rows = {(e.host, e.path_pattern, e.upstream_addr): e
            for e in db.query(LinkEdge).all()}
    check("C 三行规则边键正确",
          set(rows) == {("api.example.com", "/api", "10.0.0.1:8080"),
                        ("api.example.com", "/static", "10.0.0.2:8080"),
                        ("api.example.com", "/", "10.0.0.3:8080")},
          str(set(rows)))
    e_api = rows[("api.example.com", "/api", "10.0.0.1:8080")]
    check("C 每边独立指标（/api 边 1000 req / 5xx=5 / RT=50ms）",
          e_api.req_count == 1000 and e_api.cnt_5xx == 5 and abs(e_api.rt_avg_ms - 50.0) < 0.1,
          f"{e_api.req_count}/{e_api.cnt_5xx}/{e_api.rt_avg_ms}")
    check("C 二级路径证据（/api 边内 /api/users、/api/orders）",
          "/api/users" in e_api.path_summary and "/api/orders" in e_api.path_summary,
          e_api.path_summary)
    e_static = rows[("api.example.com", "/static", "10.0.0.2:8080")]
    check("C /static 边证据不越界（不含 /api/*）", "/api/users" not in e_static.path_summary,
          e_static.path_summary)
    check("C rule_source 默认 log", e_api.rule_source == "log" and e_api.rule_name == "")

    # ---- 重刷幂等 + confirmed 状态保留 ----
    fake.main_rows[0] = dict(fake.main_rows[0], cnt=1100)   # 证据刷新
    e_api.status, e_api.note = "confirmed", "人工确认"
    db.commit()
    r2 = LD.generate_link_draft(db)
    rows2 = {(e.host, e.path_pattern, e.upstream_addr): e
             for e in db.query(LinkEdge).all()}
    check("C 重刷不重复建行", len(rows2) == 3 and r2.get("edge_count") == 3, str(len(rows2)))
    e2 = rows2[("api.example.com", "/api", "10.0.0.1:8080")]
    check("C confirmed 状态/备注保留且证据刷新",
          e2.status == "confirmed" and e2.note == "人工确认" and e2.req_count == 1100,
          f"{e2.status}/{e2.note}/{e2.req_count}")
    check("C kept_confirmed 计数", r2.get("kept_confirmed") == 1, str(r2))
    db.close()


# ==================== D：旧粒度行状态继承 ====================

def test_legacy_state_inherit():
    print("== D 旧粒度行（path_pattern=''）状态继承 ==")
    db = DB()
    wipe(db)
    fake = FakeSLS()
    fake.main_rows = [
        dict(http_host="api.example.com", upstream_addr="10.0.0.9:8080", pp="/api",
             cnt=500, c5xx=0, c4xx=0, rt=0.02),
        dict(http_host="api.example.com", upstream_addr="10.0.0.9:8080", pp="/",
             cnt=100, c5xx=0, c4xx=0, rt=0.01),
    ]
    fake.ok_rows = [dict(http_host="api.example.com", status="200", cnt=600)]
    fake.sub_rows = []
    install_env(fake)
    LD._fetch_cloud_rules = lambda db_: []

    # 旧粒度确认行（迁移后形态：path_pattern=''）+ 一条流量已消失的旧确认行
    l1 = LinkEdge(chain="outer", host="api.example.com", upstream_addr="10.0.0.9:8080",
                  path_pattern="", status="confirmed", note="老确认", confirmed_by="alice",
                  req_count=999)
    l2 = LinkEdge(chain="outer", host="old.example.com", upstream_addr="10.0.0.7:8080",
                  path_pattern="", status="confirmed", note="保留", confirmed_by="alice",
                  req_count=88)
    db.add_all([l1, l2])
    db.commit()
    id1 = l1.id

    LD.generate_link_draft(db)
    rows = db.query(LinkEdge).all()
    by_id = {e.id: e for e in rows}
    check("D 旧边就地升级保 id", id1 in by_id, str(sorted(by_id)))
    up = by_id.get(id1)
    check("D 就地升级：path_pattern 填上 + 状态/备注/确认人保留",
          up is not None and up.path_pattern == "/api" and up.status == "confirmed"
          and up.note == "老确认" and up.confirmed_by == "alice",
          f"{getattr(up, 'path_pattern', None)}/{getattr(up, 'status', None)}/{getattr(up, 'note', None)}")
    same_up = [e for e in rows if e.upstream_addr == "10.0.0.9:8080"]
    check("D 拆出的后续规则边继承旧边状态", len(same_up) == 2
          and all(e.status == "confirmed" for e in same_up)
          and any(e.path_pattern == "/" for e in same_up),
          str([(e.path_pattern, e.status) for e in same_up]))
    l2_now = [e for e in rows if e.host == "old.example.com"]
    check("D 流量已消失的旧确认行原样保留", len(l2_now) == 1 and l2_now[0].path_pattern == ""
          and l2_now[0].note == "保留",
          str([(e.path_pattern, e.note) for e in l2_now]))
    db.close()


# ==================== E：云转发规则标注与补边 ====================

CLOUD_RULES = [
    dict(rule_key="aliyun:lb-1:rule-1", rule_id="rule-1", rule_name="API转发规则",
         domain="api.example.com", url="/api/*", path_prefix="/api",
         backends=["10.0.0.1:8080"], vgroup_name="vg-api",
         lb_id="lb-1", lb_name="外层SLB", listener_port=443,
         address_type="internet", vendor="aliyun", region="cn-hangzhou"),
    dict(rule_key="aliyun:lb-1:rule-2", rule_id="rule-2", rule_name="旧后台",
         domain="admin.example.com", url="/admin/*", path_prefix="/admin",
         backends=["10.0.0.8:8080"], vgroup_name="vg-admin",
         lb_id="lb-1", lb_name="内层SLB", listener_port=80,
         address_type="intranet", vendor="aliyun", region="cn-hangzhou"),
]


def test_cloud_rule_labels():
    print("== E 云转发规则：标注 + 有规则无流量补边 ==")
    db = DB()
    wipe(db)
    fake = FakeSLS()
    fake.main_rows = main_rows_default()
    fake.ok_rows = ok_rows_default()
    fake.sub_rows = sub_rows_default()
    install_env(fake)
    LD._fetch_cloud_rules = lambda db_: [dict(r) for r in CLOUD_RULES]

    r = LD.generate_link_draft(db)
    rows = {(e.host, e.path_pattern, e.upstream_addr): e
            for e in db.query(LinkEdge).all()}
    e_api = rows.get(("api.example.com", "/api", "10.0.0.1:8080"))
    check("E 命中规则贴 rule_name", e_api is not None and e_api.rule_name == "API转发规则",
          getattr(e_api, "rule_name", None))
    cloud = rows.get(("admin.example.com", "/admin", "10.0.0.8:8080"))
    check("E 有规则无流量补边（rule_source=cloud / req=0 / intranet->inner）",
          cloud is not None and cloud.rule_source == "cloud" and cloud.req_count == 0
          and cloud.chain == "inner" and cloud.rule_name == "旧后台",
          str(cloud))
    check("E 结果统计", r.get("cloud_rules_total") == 2 and r.get("cloud_rules_matched") == 1
          and r.get("cloud_only_edges") == 1 and r.get("rule_edges") == 1, str(r))
    check("E message 含规则统计", "云转发规则标注 1 条" in r.get("message", ""), r.get("message"))

    # 再刷一轮：补边不重复建行
    LD.generate_link_draft(db)
    n = db.query(LinkEdge).filter(LinkEdge.host == "admin.example.com").count()
    check("E 重刷补边幂等（admin 行不膨胀）", n == 1, str(n))
    db.close()


# ==================== F：幂等迁移 ====================

OLD_DDL = """
CREATE TABLE link_edges (
	id INTEGER NOT NULL,
	chain VARCHAR(16) NOT NULL,
	host VARCHAR(255) NOT NULL,
	upstream_addr VARCHAR(64) NOT NULL,
	ecs_name VARCHAR(255) NOT NULL,
	app_name VARCHAR(255) NOT NULL,
	req_count INTEGER NOT NULL,
	cnt_4xx INTEGER NOT NULL,
	cnt_5xx INTEGER NOT NULL,
	rt_avg_ms FLOAT NOT NULL,
	ecs_cpu FLOAT,
	app_error_cnt INTEGER NOT NULL,
	path_summary TEXT NOT NULL,
	status VARCHAR(16) NOT NULL,
	note VARCHAR(500) NOT NULL,
	confirmed_by VARCHAR(64),
	confirmed_at TIMESTAMP,
	generated_at TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_link_edge UNIQUE (chain, host, upstream_addr)
)
"""


def test_migration():
    print("== F 幂等迁移（旧表重建，旧行不丢） ==")
    mig_db = os.path.join(TMPDIR, "legacy.db")
    eng = create_engine(test_database_url("linkmig"))
    with eng.begin() as conn:
        conn.execute(text(OLD_DDL))
        conn.exec_driver_sql(
            "INSERT INTO link_edges (id, chain, host, upstream_addr, ecs_name, app_name,"
            " req_count, cnt_4xx, cnt_5xx, rt_avg_ms, ecs_cpu, app_error_cnt, path_summary,"
            " status, note, confirmed_by, confirmed_at, generated_at) VALUES"
            " (1,'outer','a.example.com','10.0.0.1:80','web','app',100,1,2,12.5,NULL,0,"
            " '[{\"path\":\"/\",\"cnt\":100}]','confirmed','老行','alice',NULL,'2026-11-01 00:00:00'),"
            " (2,'inner','b.example.com','10.0.0.2:80','', '',5,0,0,3.0,NULL,0,'','draft','','',NULL,"
            " '2026-11-01 00:00:00')")
    migrated = migrate_link_edges_rule_dim(eng)
    check("F 首跑返回 True", migrated is True)
    with eng.connect() as conn:
        cols = {c["name"] for c in __import__("sqlalchemy").inspect(eng).get_columns("link_edges")}
        rows = conn.execute(text(
            "SELECT id, host, path_pattern, rule_name, rule_source, status, note, confirmed_by"
            " FROM link_edges ORDER BY id")).fetchall()
        idx = [(i["name"], i.get("column_names")) for i in __import__("sqlalchemy").inspect(eng).get_indexes("link_edges")]
    check("F 新三维列就位", {"path_pattern", "rule_name", "rule_source"} <= cols, str(sorted(cols)))
    check("F 旧行不丢且新列默认", len(rows) == 2
          and all(r[2] == "" and r[3] == "" and r[4] == "log" for r in rows)
          and rows[0][5] == "confirmed" and rows[0][6] == "老行" and rows[0][7] == "alice",
          str(rows))
    check("F 索引保留", {"ix_link_edges_chain", "ix_link_edges_host", "ix_link_edges_status"}
          <= {n for n, _ in idx}, str(idx))
    with eng.begin() as conn:   # 新唯一约束 (chain,host,path_pattern,upstream) 生效
        conn.exec_driver_sql(
            "INSERT INTO link_edges (chain, host, upstream_addr, path_pattern, rule_name,"
            " rule_source, ecs_name, app_name, req_count, cnt_4xx, cnt_5xx, rt_avg_ms,"
            " app_error_cnt, path_summary, status, note, generated_at) VALUES"
            " ('outer','a.example.com','10.0.0.1:80','/api','','log','','',1,0,0,0,0,'','draft','',"
            " '2026-11-02 00:00:00')")
    dup_ok = False
    try:
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO link_edges (chain, host, upstream_addr, path_pattern, rule_name,"
                " rule_source, ecs_name, app_name, req_count, cnt_4xx, cnt_5xx, rt_avg_ms,"
                " app_error_cnt, path_summary, status, note, generated_at) VALUES"
                " ('outer','a.example.com','10.0.0.1:80','/api','x','log','','',1,0,0,0,0,'','draft','',"
                " '2026-11-02 00:00:00')")
    except IntegrityError:
        dup_ok = True
    check("F 新唯一约束生效（同规则边重复插入被拒）", dup_ok)
    again = migrate_link_edges_rule_dim(eng)
    check("F 二跑幂等（no-op）", again is False)
    # 迁移后 ORM 读写可用
    with Session(eng) as s:
        e = s.query(LinkEdge).filter(LinkEdge.host == "a.example.com",
                                     LinkEdge.path_pattern == "/api").first()
        check("F 迁移后 ORM 可读", e is not None and e.rule_source == "log", str(e))


# ==================== G：多云转发规则拉取（mock） ====================

def test_providers():
    print("== G 多云转发规则拉取（SDK introspection + mock） ==")
    # —— SDK 方法名 introspection（真实签名核对，防拍脑袋 API 名）——
    try:
        from alibabacloud_slb20140515.client import Client as SlbClient
        from alibabacloud_slb20140515 import models as slbm
        check("G 阿里 SDK：describe_rules / describe_vserver_groups / "
              "describe_vserver_group_attribute 存在（真实方法名）",
              all(hasattr(SlbClient, m) for m in
                  ("describe_rules", "describe_vserver_groups",
                   "describe_vserver_group_attribute", "describe_load_balancers",
                   "describe_load_balancer_listeners")))
        check("G 阿里 SDK 方法/模型命名不一致已核对（方法无下划线，模型类有 VServer）",
              hasattr(SlbClient, "describe_vserver_group_attribute")
              and not hasattr(SlbClient, "describe_v_server_group_attribute"))
        check("G 阿里 SDK 模型存在",
              all(hasattr(slbm, m) for m in
                  ("DescribeRulesRequest", "DescribeVServerGroupAttributeRequest",
                   "DescribeLoadBalancersRequest", "DescribeLoadBalancerListenersRequest")))
    except Exception as e:  # pragma: no cover
        check("G 阿里 SDK introspection", False, str(e))
    try:
        from tencentcloud.clb.v20180317.clb_client import ClbClient
        from tencentcloud.clb.v20180317 import models as cm
        check("G 腾讯 SDK：DescribeListeners/DescribeTargets 存在（真实方法名）",
              hasattr(ClbClient, "DescribeListeners") and hasattr(ClbClient, "DescribeTargets"))
        check("G 腾讯 SDK 无 DescribeForwardRules（文档动作名，实现按真实签名走）",
              not hasattr(ClbClient, "DescribeForwardRules"))
        check("G 腾讯 SDK 模型 RuleOutput/RuleTargets/Backend 存在",
              all(hasattr(cm, m) for m in ("RuleOutput", "RuleTargets", "Backend",
                                           "DescribeListenersRequest", "DescribeTargetsRequest")))
    except Exception as e:  # pragma: no cover
        check("G 腾讯 SDK introspection", False, str(e))

    # —— 阿里 mock ——
    def fake_slb_client(ak, sk, region):
        lb1 = SimpleNamespace(load_balancer_id="lb-1", load_balancer_name="外层SLB",
                              address_type="internet")
        lb2 = SimpleNamespace(load_balancer_id="lb-2", load_balancer_name="内层SLB",
                              address_type="intranet")

        class C:
            def describe_load_balancers(self, req):
                all_lbs = [lb1, lb2]
                batch = all_lbs[req.page_number - 1:req.page_number]
                return SimpleNamespace(body=SimpleNamespace(
                    load_balancers=SimpleNamespace(load_balancer=batch),
                    total_count=2))

            def describe_load_balancer_listeners(self, req):
                if req.load_balancer_id == "lb-1":
                    return SimpleNamespace(body=SimpleNamespace(listeners=[
                        SimpleNamespace(listener_port=443, listener_protocol="HTTP"),
                        SimpleNamespace(listener_port=22, listener_protocol="TCP")]))
                return SimpleNamespace(body=SimpleNamespace(listeners=[
                    SimpleNamespace(listener_port=80, listener_protocol="HTTP")]))

            def describe_rules(self, req):
                if req.load_balancer_id == "lb-1":
                    return SimpleNamespace(body=SimpleNamespace(rules=SimpleNamespace(rule=[
                        SimpleNamespace(rule_id="rule-1", rule_name="API转发规则",
                                        domain="api.example.com", url="/api/*",
                                        vserver_group_id="vg-1")])))
                return SimpleNamespace(body=SimpleNamespace(rules=SimpleNamespace(rule=[
                    SimpleNamespace(rule_id="rule-2", rule_name="",
                                    domain="admin.example.com", url="/admin/*",
                                    vserver_group_id="vg-2")])))

            def describe_vserver_group_attribute(self, req):
                members = {"vg-1": [SimpleNamespace(server_ip="10.0.0.1", port=8080),
                                    SimpleNamespace(server_ip="10.0.0.2", port=8080)],
                           "vg-2": [SimpleNamespace(server_ip="10.0.0.8", port=8080)]}
                return SimpleNamespace(body=SimpleNamespace(backend_servers=SimpleNamespace(
                    backend_server=members.get(req.vserver_group_id, []))))

        return C()

    P._slb_client = fake_slb_client
    acc = SimpleNamespace(region="cn-hangzhou", name="t", vendor="aliyun")
    rules = P.AlibabaProvider().fetch_forward_rules(acc, "ak", "sk")
    check("G 阿里规则数=2（TCP 监听不产 URL 规则）", len(rules) == 2, str(rules))
    r1 = next((x for x in rules if x["rule_id"] == "rule-1"), {})
    check("G 阿里字段归一（rule/domain/url/地址类型）",
          r1.get("rule_name") == "API转发规则" and r1.get("domain") == "api.example.com"
          and r1.get("url") == "/api/*" and r1.get("address_type") == "internet", str(r1))
    check("G 阿里后端组成员 -> ip:port", r1.get("backends") == ["10.0.0.1:8080", "10.0.0.2:8080"],
          str(r1.get("backends")))
    r2 = next((x for x in rules if x["rule_id"] == "rule-2"), {})
    check("G 阿里内层 LB address_type=intranet", r2.get("address_type") == "intranet", str(r2))

    # —— 腾讯 mock ——
    def fake_tencent_client(service, version, ak, sk, region):
        lb = SimpleNamespace(LoadBalancerId="clb-1", LoadBalancerName="CLB",
                             LoadBalancerType="OPEN")

        class C:
            def DescribeLoadBalancers(self, req):
                return SimpleNamespace(LoadBalancerSet=[lb], TotalCount=1)

            def DescribeListeners(self, req):
                rule = SimpleNamespace(LocationId="loc-1", Domain="api.example.com",
                                       Url="/api/*", ListenerId="lbl-1")
                tcp = SimpleNamespace(Protocol="TCP", Port=8080, Rules=[])
                http = SimpleNamespace(Protocol="HTTPS", Port=443, Rules=[rule])
                return SimpleNamespace(Listeners=[tcp, http])

            def DescribeTargets(self, req):
                rt = SimpleNamespace(LocationId="loc-1", Targets=[
                    SimpleNamespace(Port=8080, PrivateIpAddresses=["10.0.1.1"], EniIp=""),
                    SimpleNamespace(Port=8081, PrivateIpAddresses=[], EniIp="10.0.1.2")])
                ln = SimpleNamespace(ListenerId="lbl-1", Rules=[rt], Targets=[])
                return SimpleNamespace(Listeners=[ln])

        return C()

    P._tencent_client = fake_tencent_client
    acc2 = SimpleNamespace(region="ap-guangzhou", name="t2", vendor="tencent")
    trules = P.TencentProvider().fetch_forward_rules(acc2, "ak", "sk")
    check("G 腾讯规则数=1（TCP 监听跳过）", len(trules) == 1, str(trules))
    t = trules[0] if trules else {}
    check("G 腾讯字段归一（LocationId/url/domain）",
          t.get("rule_id") == "loc-1" and t.get("url") == "/api/*"
          and t.get("domain") == "api.example.com", str(t))
    check("G 腾讯规则后端（PrivateIp/EniIp 归一 ip:port）",
          t.get("backends") == ["10.0.1.1:8080", "10.0.1.2:8081"], str(t.get("backends")))
    check("G 腾讯 OPEN -> internet", t.get("address_type") == "internet", str(t))

    # —— 骨架厂商返回 []（不抛）——
    check("G 骨架厂商 fetch_forward_rules 返回空",
          P.get_provider("volc").fetch_forward_rules(None, "", "") == [])
    check("G 自建 fetch_forward_rules 返回空",
          P.get_provider("onprem").fetch_forward_rules(None, "", "") == [])


# ==================== H：_edge_out 新字段 ====================

def test_edge_out():
    print("== H _edge_out 字段透出 ==")
    from app.api.v1.link import _edge_out

    db = DB()
    wipe(db)
    e = LinkEdge(chain="outer", host="h.example.com", upstream_addr="10.0.0.1:80",
                 path_pattern="/api", rule_name="R1", rule_source="cloud",
                 req_count=0, path_summary='[{"path": "/api/x", "cnt": 1}]')
    db.add(e)
    db.commit()
    out = _edge_out(e)
    check("H 新字段在响应里",
          out.get("path_pattern") == "/api" and out.get("rule_name") == "R1"
          and out.get("rule_source") == "cloud", str(out))
    check("H 旧字段不回归", out.get("paths") == [{"path": "/api/x", "cnt": 1}]
          and out.get("status") == "draft", str(out))
    db.close()


# ==================== I：前端渲染文案（抽源码跑 node） ====================

def test_frontend_message():
    print("== I 前端 formatDraftResult 文案 ==")
    vue = "/opt/qianshu/frontend/src/components/LinksDraft.vue"
    src = open(vue, encoding="utf-8").read()
    check("I 旧 undefined 拼串已移除", "草稿已生成：${r.edge_count} 条边" not in src
          or "formatDraftResult" in src, "")
    m = re.search(r"function formatDraftResult\(r\) \{", src)
    if not m:
        check("I formatDraftResult 存在", False, "函数未找到")
        return
    start = m.start()
    depth, i = 0, src.index("{", m.end() - 1)
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    fn_src = src[start:i + 1]
    vectors = [
        None,
        {},
        {"busy": True, "message": "草稿生成中，请稍候再点「生成 / 刷新草稿」"},
        {"skipped": "SLS 目标未配置", "message": "未生成草稿：SLS 目标未配置。请在「安装向导」…"},
        {"skipped": "SLS 目标未配置"},
        {"edge_count": 3, "filtered_host_count": 1},
        {"edge_count": 3, "filtered_host_count": 0, "cloud_rules_total": 2,
         "rule_edges": 1, "cloud_only_edges": 1},
    ]
    runner = os.path.join(TMPDIR, "fmt.mjs")
    with open(runner, "w", encoding="utf-8") as f:
        f.write(fn_src + "\n")
        f.write(f"const vectors = {json.dumps(vectors, ensure_ascii=False)}\n")
        f.write("console.log(JSON.stringify(vectors.map(v => formatDraftResult(v))))\n")
    try:
        proc = subprocess.run(["node", runner], capture_output=True, text=True, timeout=30)
        out = json.loads(proc.stdout)
    except Exception as e:
        check("I node 运行", False, str(e))
        return
    check("I node 跑通 7 个向量", len(out) == len(vectors), proc.stderr[:200])
    texts = [o.get("text", "") for o in out]
    check("I 所有文案无 undefined", all("undefined" not in t for t in texts), str(texts))
    check("I busy 文案走 message", out[2]["type"] == "warning" and "稍候" in texts[2], texts[2])
    check("I skipped 文案走 message（含安装向导指引）",
          out[3]["type"] == "warning" and "安装向导" in texts[3], texts[3])
    check("I skipped 无 message 兜底不裸奔",
          out[4]["type"] == "warning" and "SLS 目标未配置" in texts[4]
          and "草稿未生成" in texts[4], texts[4])
    check("I 正常分支文案", texts[5] == "草稿已生成：3 条边（剔除野域名 1 个）", texts[5])
    check("I 规则统计文案", "云转发规则标注 1 条" in texts[6], texts[6])
    check("I 缺 edge_count 明确报错", out[1]["type"] == "error" and "缺边数" in texts[1], texts[1])


def main():
    test_skipped()
    test_busy()
    test_generate_rule_edges()
    test_legacy_state_inherit()
    test_cloud_rule_labels()
    test_migration()
    test_providers()
    test_edge_out()
    test_frontend_message()
    print(f"\n== 结果: {len(PASS)} 过 / {len(FAIL)} 挂 ==")
    for f in FAIL:
        print(f"  FAIL: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
