#!/usr/bin/env python3
"""数据结构回归隔离冒烟：一次性 PostgreSQL 全链路验证。

运行：cd /opt/qianshu/backend && python3 tests/test_p2_structure_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu 库、不用真实主密钥、不调任何云 API、不起后台任务（ASGI 直连不进 lifespan）。

验证项：
  A IP 归一单源：norm_ip 去端口/大小写/IPv6/空白；extra_ips 提取去重；
    ownership.norm_key('ip') 委托同源（历史形态 IP:port 归一命中）；
  B 机器↔资产↔链路互通：ip_linkage 三账对上 + /machines 行带关联资产/链路边数；
  C alert_rules account_id：迁移补列幂等；API 创建/列表过滤/更新带账号；
    评估按账号过滤（已知异账号剔除、未知/未打标保留——不猜不误杀）+ 记录按目标账号打标；
    试算同口径过滤；
  D 双概念唯一化：数据分组洗入云账号台账（保 id + 标记）；幂等；
    account_index 台账主源/分组兜底；create_account 镜像；回滚预案=删镜像行数据零改写；
  E 身份引用统一：*_by_id 双写（服务/API 写入面）+ 旧形态库迁移回填 +
    13 表 *_by_id 列齐（逐处断言的结构面）；
  F 集群引用统一：k8s_tasks.cluster_id 迁移回填；submit 双写；
    机器更新 (cluster_id, cluster_name) 双写且"不传不改"；resolve id 优先名字回落。
"""
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-p2-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("p2")
os.environ["MASTER_KEY"] = "cd" * 32          # 64 hex 测试主密钥
for _k in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
           "TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine as _sa_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402,F401  导入全量模型（与 startup create_all 同口径）
from app.core.migrate import (  # noqa: E402
    IDENTITY_REF_COLUMNS,
    migrate_alert_rule_account,
    migrate_identity_refs,
    migrate_k8s_task_cluster_ref,
    run_migrations,
)
from app.models.account import Account  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.alert_rule import AlertRule  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.expiry_ignore import ExpiryIgnore  # noqa: E402
from app.models.k8s import K8sCluster  # noqa: E402
from app.models.k8s_task import K8sTask  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.models.machine import K8sMachine  # noqa: E402
from app.models.ownership import OwnershipRule  # noqa: E402
from app.models.oncall import AlertAck  # noqa: E402
from app.services import refs  # noqa: E402
from app.services import tenant  # noqa: E402
from app.services.ipnorm import extra_ips, ip_linkage, norm_ip  # noqa: E402
from app.services.ownership import norm_key  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _make_req():
    """ASGI 直连请求壳（不触发 lifespan/startup；沿用 test_ownership_smoke 套路）。"""
    import asyncio

    import httpx

    import app.core.deps as deps

    fake = types.SimpleNamespace(id=7, username="p2smoke", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: fake

    def _req(method, url, **kw):
        async def _go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(_go())

    return _req


# ================= A. IP 归一单源 =================
def test_ipnorm() -> None:
    print("A. IP 归一（三命名空间一个解析函数）")
    check("A1 IPv4:port 去端口", norm_ip("192.168.1.10:8080") == "192.168.1.10")
    check("A2 裸 IPv4 原样", norm_ip(" 10.0.0.1 ") == "10.0.0.1")
    check("A3 [IPv6]:port 归一", norm_ip("[::1]:9000") == "::1")
    check("A4 非端口尾巴不切", norm_ip("192.168.1.10:abc") == "192.168.1.10:abc")
    check("A5 主机名大小写归一", norm_ip("Node-A.Example.COM") == "node-a.example.com")
    check("A6 extra_ips 提取+归一+去重",
          extra_ips({"ip_public": "1.2.3.4:443", "ip_internal": "192.168.1.10",
                     "private_ips": ["192.168.1.10", "10.0.0.2"]})
          == ["1.2.3.4", "192.168.1.10", "10.0.0.2"])
    check("A7 norm_key ip 委托同源（IP:port 归一）",
          norm_key("ip", "9.9.9.9:443") == "9.9.9.9" == norm_ip("9.9.9.9:443"))


# ================= B. 机器↔资产↔链路互通 =================
def test_linkage() -> None:
    print("B. 机器↔资产↔链路按 IP 互通")
    with SessionLocal() as db:
        db.add(K8sMachine(host_name="web-01", ip="192.168.1.10", ssh_port=22))
        db.add(K8sMachine(host_name="orphan", ip="172.16.0.9"))
        db.add(Asset(product="ecs", instance_id="i-web", name="web-01", account_id=1,
                     status="Running",
                     extra=json.dumps({"ip_internal": "192.168.1.10", "ip_public": "1.2.3.4"})))
        # 链路边带端口（历史形态）——归一后与机器对上；同后端按路径拆 2 条规则边
        db.add(LinkEdge(chain="outer", host="www.demo.com", upstream_addr="192.168.1.10:8080",
                        path_pattern="/", ecs_name="web-01", status="confirmed"))
        db.add(LinkEdge(chain="outer", host="www.demo.com", upstream_addr="192.168.1.10:8080",
                        path_pattern="/api", ecs_name="web-01", status="confirmed"))
        db.commit()

        lk = ip_linkage(db, "192.168.1.10")
        check("B1 ip_linkage 对上资产", lk["asset_count"] == 1
              and lk["assets"][0]["name"] == "web-01", str(lk))
        check("B2 ip_linkage 对上链路边数（带端口归一）", lk["link_count"] == 2, str(lk))
        lk2 = ip_linkage(db, "172.16.0.9")
        check("B3 无关联机器如实为空（不猜）",
              lk2["asset_count"] == 0 and lk2["link_count"] == 0, str(lk2))

    _req = _make_req()
    try:
        r = _req("GET", "/api/machines")
        items = {x["ip"]: x for x in r.json()["items"]}
        m = items.get("192.168.1.10", {})
        check("B4 /machines 行带关联资产/链路边数",
              r.status_code == 200 and m.get("asset_count") == 1
              and m.get("link_count") == 2
              and (m.get("assets") or [{}])[0].get("instance_id") == "i-web", str(m)[:300])
        mo = items.get("172.16.0.9", {})
        check("B5 无关联行如实为 0（不造）",
              mo.get("asset_count") == 0 and mo.get("link_count") == 0, str(mo))
    finally:
        app.dependency_overrides.clear()


# ================= C. alert_rules account_id =================
def test_alert_rule_account() -> None:
    print("C. alert_rules 账号维度（编辑/评估/展示带账号过滤）")
    with SessionLocal() as db:
        db.add(CloudAccount(id=2, name="prod-acc", vendor="aliyun", enabled=True, status="ok"))
        db.add(CloudAccount(id=5, name="sls-acc", vendor="aliyun", enabled=True, status="ok"))
        db.add(Asset(product="ecs", instance_id="i-accA", name="a", account_id=2,
                     status="Running", extra="{}"))
        db.add(Asset(product="ecs", instance_id="i-accB", name="b", account_id=3,
                     status="Running", extra="{}"))
        db.commit()

    _req = _make_req()
    try:
        # 编辑面：创建带账号
        r = _req("POST", "/api/alerts/rules", json={
            "name": "p2-acc-rule", "source": "metric", "level": "warning",
            "product": "ecs", "metric": "CPUUtilization",
            "comparator": ">", "threshold": 90, "duration": 300,
            "account_id": 2})
        j = r.json()
        check("C1 创建规则带账号（回包 account_id/account_name）",
              r.status_code == 200 and j.get("account_id") == 2
              and j.get("account_name") == "prod-acc", r.text[:300])
        # 展示面：按账号过滤
        r2 = _req("GET", "/api/alerts/rules", params={"account_id": 2})
        names = [x["name"] for x in r2.json()]
        check("C2 列表按账号过滤（命中）", "p2-acc-rule" in names, str(names))
        r3 = _req("GET", "/api/alerts/rules", params={"account_id": 5})
        check("C3 列表按账号过滤（异账号不命中）",
              "p2-acc-rule" not in [x["name"] for x in r3.json()])
        # 编辑面：改绑定
        rid = j["id"]
        r4 = _req("PUT", f"/api/alerts/rules/{rid}", json={"account_id": 5})
        check("C4 更新账号绑定", r4.status_code == 200
              and r4.json().get("account_id") == 5
              and r4.json().get("account_name") == "sls-acc", r4.text[:200])
        _req("PUT", f"/api/alerts/rules/{rid}", json={"account_id": 2})
    finally:
        app.dependency_overrides.clear()

    # 评估面：绑定账号只评估该账号目标；记录按目标账号打标
    import app.services.alert_eval as ae

    orig_metric, orig_sls, orig_notify = ae._eval_metric_rule, ae._eval_sls_rule, ae._notify
    ae._eval_metric_rule = lambda rule: {
        "i-accA": [True, 99.0, ""], "i-accB": [True, 98.0, ""], "ghost-1": [True, 97.0, ""]}
    ae._eval_sls_rule = lambda db_, rule: {"demo/logstore": (True, 3.0)}
    ae._notify = lambda *a, **k: []
    try:
        with SessionLocal() as db:
            db.add(AlertRule(name="p2-sls-rule", source="sls", level="warning",
                             project="demo", logstore="logstore", query_expr="*",
                             comparator=">", threshold=1, duration=300, account_id=5))
            db.commit()
        with SessionLocal() as db:
            ae.run_evaluation(db)
        with SessionLocal() as db:
            got = {(r.rule_name, r.target): r for r in db.query(AlertRecord).all()}
            check("C5 异账号目标被剔除",
                  ("p2-acc-rule", "i-accB") not in got, str(list(got)))
            check("C6 本账号目标保留且按目标账号打标",
                  ("p2-acc-rule", "i-accA") in got
                  and got[("p2-acc-rule", "i-accA")].account_id == 2, str(list(got)))
            check("C7 未知目标保留（不猜归属不误杀）",
                  ("p2-acc-rule", "ghost-1") in got, str(list(got)))
            check("C8 SLS 规则记录按规则绑定账号打标",
                  ("p2-sls-rule", "demo/logstore") in got
                  and got[("p2-sls-rule", "demo/logstore")].account_id == 5, str(list(got)))
    finally:
        ae._eval_metric_rule, ae._eval_sls_rule, ae._notify = orig_metric, orig_sls, orig_notify

    # 试算面：同口径按账号过滤（回放序列剔除异账号）
    import app.services.alert_sim as sim

    orig_vm = sim.vm_query_range
    _now = int(time.time())
    _pts = [[str(_now - 600 + i * 20), "95"] for i in range(30)]   # 近 10 分钟全越限
    sim.vm_query_range = lambda *a, **k: [
        {"metric": {"instance_id": "i-accA"}, "values": list(_pts)},
        {"metric": {"instance_id": "i-accB"}, "values": list(_pts)},
    ]
    try:
        with SessionLocal() as db:
            out = sim.simulate(db, {"source": "metric", "product": "ecs",
                                    "metric": "CPUUtilization", "threshold": 90,
                                    "duration": 300, "account_id": 2}, days=1)
        fired = out["samples_summary"].get("targets_fired") or []
        check("C9 试算同口径过滤（异账号不进回放）",
              "i-accA" in fired and "i-accB" not in fired, str(fired))
    finally:
        sim.vm_query_range = orig_vm


# ================= D. account_id 双概念唯一化 =================
def test_account_concept() -> None:
    print("D. 双概念唯一化：account_id 显式指向 cloud_accounts（分组=数据分组）")
    with SessionLocal() as db:
        # 用 8/9 避开 C 段已存在的台账 id（id 空间同轴后 1:1 对位，见 D1a 碰撞语义）
        db.add(Account(id=8, name="默认账号", kind="cloud", remark="首个账号"))
        db.add(Account(id=9, name="prod账号", kind="cloud", remark=""))
        db.add(Account(id=2, name="碰撞分组", kind="cloud", remark="与已有台账 id 相撞"))
        db.commit()
    run_migrations(engine)   # 洗入
    with SessionLocal() as db:
        bridges = {b.id: b for b in db.query(CloudAccount).all()}
        check("D1 分组洗入台账（保 id + 来源标记）",
              bridges.get(8) is not None and bridges[8].name == "默认账号"
              and "accounts_p22" in (bridges[8].extra_json or "")
              and bridges.get(9) is not None and bridges[9].name == "prod账号",
              str({k: v.name for k, v in bridges.items()}))
        check("D2 镜像行不猜厂商/不进采集（vendor 空 + enabled=0）",
              (bridges[8].vendor or "") == "" and not bridges[8].enabled
              and (bridges[9].vendor or "") == "" and not bridges[9].enabled)
        check("D1a id 碰撞不覆盖既有台账行（不改人工数据）",
              bridges.get(2) is not None and bridges[2].name == "prod-acc", str(bridges.get(2)))
        n = db.query(CloudAccount).count()
        run_migrations(engine)   # 幂等
        with SessionLocal() as db2:
            check("D3 迁移幂等（重跑不增行）", db2.query(CloudAccount).count() == n)
        amap = tenant.account_index(db)
        check("D4 account_index 台账主源解析", amap.get(8, {}).get("name") == "默认账号"
              and amap.get(9, {}).get("name") == "prod账号", str(amap))
        # 回滚预案：删镜像行 → 数据零改写、展示回退分组兜底（不炸不丢）
        db.query(CloudAccount).filter(CloudAccount.id == 9).delete()
        db.commit()
        amap2 = tenant.account_index(db)
        check("D5 回滚预案：删镜像行后展示回退兜底且数据无损",
              amap2.get(9, {}).get("name") == "prod账号"
              and db.query(Account).count() == 3, str(amap2))
        run_migrations(engine)   # 重建派生镜像（幂等可重复）

    # 写入端收口：新建数据分组 → 同 id 镜像进台账
    _req = _make_req()
    try:
        r = _req("POST", "/api/settings/accounts", json={"name": "p2-new-group", "kind": "cloud"})
        gid = r.json().get("id")
        check("D6 新建分组 API 可用", r.status_code == 200 and gid, r.text[:200])
        with SessionLocal() as db:
            row = db.get(CloudAccount, gid)
            check("D7 新分组自动镜像进台账（同 id）",
                  row is not None and row.name == "p2-new-group"
                  and "accounts_p22" in (row.extra_json or ""), str(row))
        r2 = _req("GET", "/api/settings/accounts")
        ids = [x["id"] for x in r2.json()]
        check("D8 分组列表幂等（重复拉取不重复镜像）", ids.count(gid) == 1, str(ids))
    finally:
        app.dependency_overrides.clear()


# ================= E. 身份引用统一（user） =================
def test_user_refs() -> None:
    print("E. user 引用统一：*_by_id 双写 + 旧库回填 + 全类列齐")
    with SessionLocal() as db:
        db.add(K8sCluster(id=1, name="prod-k8s", api_url="https://127.0.0.1:6443"))
        db.commit()

    # 写入面 1：服务层双写
    from app.services.calibration import confirm_item
    from app.services.oncall import ack_alert

    with SessionLocal() as db:
        ack_alert(db, 101, "p2smoke", "在管", user_id=7)
        a = db.query(AlertAck).first()
        check("E1 ack 服务双写 (ack_by_id, ack_by)",
              a is not None and a.ack_by_id == 7 and a.ack_by == "p2smoke", str(a))
        from app.models.calibration import CalibrationItem

        db.add(CalibrationItem(week="2026-W46", item_type="new_host", ref="x"))
        db.commit()
        cid = db.query(CalibrationItem).first().id
        confirm_item(db, cid, "p2smoke", user_id=7)
        it = db.get(CalibrationItem, cid)
        check("E2 校准确认双写", it.confirmed_by_id == 7 and it.confirmed_by == "p2smoke")

    # 写入面 2：API 层双写（归属登记 / 到期忽略）
    _req = _make_req()
    try:
        r = _req("POST", "/api/ownership/rules", json={
            "key_type": "ip", "key_value": "6.6.6.6:80", "match_type": "exact",
            "vendor": "onprem", "account_name": "p2-acc"})
        with SessionLocal() as db:
            row = db.query(OwnershipRule).filter(OwnershipRule.key_value == "6.6.6.6").first()
            check("E3 归属登记双写", row is not None and row.created_by_id == 7
                  and row.created_by == "p2smoke", str(row))
        r2 = _req("POST", "/api/expiry/ignores", json={"target_key": "ecs:i-p2", "note": ""})
        with SessionLocal() as db:
            row = db.query(ExpiryIgnore).first()
            check("E4 到期忽略双写", r2.status_code == 200 and row is not None
                  and row.created_by_id == 7 and row.created_by == "p2smoke", r2.text[:200])
    finally:
        app.dependency_overrides.clear()

    # 写入面 3：业务草稿 / 任务提交（submitted_by_id）
    from app.services.biz_map import generate_biz_draft
    from app.services.task_engine import submit, task_out

    with SessionLocal() as db:
        db.add(LinkEdge(chain="outer", host="p2.demo.com", upstream_addr="10.9.9.9:80",
                        ecs_name="p2-ecs", app_name="", req_count=12, status="confirmed"))
        db.commit()
        draft_result = generate_biz_draft(db, created_by="p2smoke", created_by_id=7)
        db.flush()   # SessionLocal autoflush=False；API 入口随后 commit，本隔离直调需显式 flush
        from app.models.business import BizIdentifier
        from app.services.biz_map import resolve_biz

        b = db.query(BizIdentifier).filter(BizIdentifier.ident_value == "p2.demo.com").first()
        check("E5 业务草稿双写", b is not None and b.created_by_id == 7, str(draft_result))
        check("E5b 自动生成只落草稿且不参与归属",
              b is not None and b.business.status == "draft"
              and resolve_biz(db, target="p2.demo.com") == [])
        t = submit(db, "backup", "p2 备份", {"cluster_name": "prod-k8s"}, [],
                   runner=lambda *a, **k: (True, ""), submitted_by="p2smoke",
                   submitted_by_id=7)
        out = task_out(t)
        check("E6 任务提交人双写", out["submitted_by_id"] == 7
              and out["submitted_by"] == "p2smoke", str(out)[:200])

    # 结构面：13 表 *_by_id 列齐（逐处断言的类级兜底）
    with engine.connect() as conn:
        missing = []
        for table, col in IDENTITY_REF_COLUMNS:
            rows = conn.execute(text("SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema='public' AND table_name = :t"),
                                {"t": table}).fetchall()
            cols = {r[0] for r in rows}
            if f"{col}_id" not in cols:
                missing.append(f"{table}.{col}_id")
        check(f"E7 全类 {len(IDENTITY_REF_COLUMNS)} 表 *_by_id 列齐", not missing, str(missing))

    # 旧形态库回填（真实迁移路径：先建旧形状表 → 迁移 → 回填 + 幂等）
    e2 = _sa_engine(test_database_url("p2mig"))
    with e2.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        conn.execute(text("INSERT INTO users VALUES (3, 'alice')"))
        conn.execute(text(
            "CREATE TABLE alert_acks (id INTEGER PRIMARY KEY, alert_id INTEGER,"
            " ack_by VARCHAR(64) DEFAULT '', ack_at TIMESTAMP, note TEXT)"))
        conn.execute(text("INSERT INTO alert_acks (id, alert_id, ack_by) VALUES (1, 9, 'alice')"))
        conn.execute(text("INSERT INTO alert_acks (id, alert_id, ack_by) VALUES (2, 9, 'ghost')"))
        conn.execute(text(
            "CREATE TABLE k8s_tasks (id INTEGER PRIMARY KEY, task_type VARCHAR(32),"
            " title VARCHAR(255), cluster_name VARCHAR(64) DEFAULT '', status VARCHAR(16),"
            " meta_json TEXT, steps_json TEXT, error TEXT, submitted_by VARCHAR(64) DEFAULT '',"
            " created_at TIMESTAMP, started_at TIMESTAMP, finished_at TIMESTAMP, retries INTEGER)"))
        conn.execute(text("INSERT INTO k8s_tasks (id, task_type, cluster_name) VALUES (1, 'backup', 'prod-k8s')"))
        conn.execute(text(
            "CREATE TABLE k8s_clusters (id INTEGER PRIMARY KEY, name VARCHAR(64))"))
        conn.execute(text("INSERT INTO k8s_clusters VALUES (4, 'prod-k8s')"))
        conn.execute(text(
            "CREATE TABLE alert_rules (id INTEGER PRIMARY KEY, name VARCHAR(128))"))
        conn.execute(text("INSERT INTO alert_rules VALUES (1, 'old-rule')"))
    migrate_identity_refs(e2)
    migrate_k8s_task_cluster_ref(e2)
    migrate_alert_rule_account(e2)
    with e2.connect() as conn:
        rows = conn.execute(text("SELECT id, ack_by, ack_by_id FROM alert_acks ORDER BY id")).fetchall()
        check("E8 旧库回填：username→users.id", rows[0][2] == 3, str(rows))
        check("E9 旧库回填：对不上用户留 0（不造引用）", rows[1][2] == 0, str(rows))
        trow = conn.execute(text("SELECT cluster_id FROM k8s_tasks WHERE id=1")).fetchone()
        check("F3 旧库回填：cluster_name→cluster_id", trow[0] == 4, str(trow))
        arow = conn.execute(text("SELECT account_id FROM alert_rules WHERE id=1")).fetchone()
        check("C10 旧库补列默认 0=全部账号", arow[0] == 0, str(arow))
    migrate_identity_refs(e2)   # 幂等
    migrate_k8s_task_cluster_ref(e2)
    migrate_alert_rule_account(e2)
    with e2.connect() as conn:
        rows = conn.execute(text("SELECT ack_by_id FROM alert_acks ORDER BY id")).fetchall()
        check("E10 迁移幂等（重跑不改值）", [r[0] for r in rows] == [3, 0], str(rows))


# ================= F. 集群引用统一 =================
def test_cluster_refs() -> None:
    print("F. cluster 引用统一：(cluster_id, cluster_name) 双写 + 不传不改")
    with SessionLocal() as db:
        cid, cname = refs.resolve_cluster_ref(db, 0, "prod-k8s")
        check("F1 名字回落解析 id", cid == 1 and cname == "prod-k8s", f"{cid},{cname}")
        cid2, cname2 = refs.resolve_cluster_ref(db, 1, "")
        check("F2 id 优先解析名字快照", cid2 == 1 and cname2 == "prod-k8s", f"{cid2},{cname2}")
        cid3, cname3 = refs.resolve_cluster_ref(db, 0, "no-such")
        check("F4 对不上保留快照不造引用", cid3 == 0 and cname3 == "no-such", f"{cid3},{cname3}")

    with SessionLocal() as db:
        m = db.query(K8sMachine).filter(K8sMachine.ip == "172.16.0.9").first()
        mid = m.id
        db.commit()
    _req = _make_req()
    try:
        body = {"host_name": "orphan", "ip": "172.16.0.9", "ssh_port": 22,
                "ssh_user": "root", "role": "node", "cluster_name": "prod-k8s"}
        r = _req("PUT", f"/api/machines/{mid}", json=body)
        check("F5 机器分配集群：(id,name) 双写", r.status_code == 200
              and r.json().get("cluster_id") == 1
              and r.json().get("cluster_name") == "prod-k8s", r.text[:200])
        body2 = {"host_name": "orphan", "ip": "172.16.0.9", "ssh_port": 22,
                 "ssh_user": "root", "role": "node"}   # 不带 cluster 字段
        r2 = _req("PUT", f"/api/machines/{mid}", json=body2)
        check("F6 机器编辑不传集群=不改（不再误清）",
              r2.status_code == 200 and r2.json().get("cluster_id") == 1
              and r2.json().get("cluster_name") == "prod-k8s", r2.text[:200])
    finally:
        app.dependency_overrides.clear()

    # 任务侧：submit 按名字解析落 cluster_id（E6 已建任务）
    with SessionLocal() as db:
        t = db.query(K8sTask).order_by(K8sTask.id.desc()).first()
        check("F7 任务 cluster_id 落库（名字→id）",
              t is not None and t.cluster_id == 1 and t.cluster_name == "prod-k8s", str(t))
        t2 = submit_noop_check(db)
        check("F8 未登记集群：只留名字快照 id=0", t2, "")


def submit_noop_check(db) -> bool:
    from app.services.task_engine import submit

    t = submit(db, "backup", "无集群任务", {"cluster_name": "no-such"}, [],
               runner=lambda *a, **k: (True, ""))
    return t.cluster_id == 0 and t.cluster_name == "no-such"


if __name__ == "__main__":
    # 迁移幂等（B）：create_all + run_migrations 两遍
    print("0. 迁移幂等（两遍 create_all + run_migrations）")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(engine)
    cols = {c["name"] for c in insp.get_columns("alert_rules")}
    check("01 alert_rules.account_id 已就绪", "account_id" in cols, str(cols))
    cols2 = {c["name"] for c in insp.get_columns("k8s_tasks")}
    check("02 k8s_tasks.cluster_id 已就绪", "cluster_id" in cols2, str(cols2))

    test_ipnorm()
    test_linkage()
    test_alert_rule_account()
    test_account_concept()
    test_user_refs()
    test_cluster_refs()
    # 等 task_engine 后台线程收尾：退出时清理临时库不能打断仍在提交的任务
    import time as _time
    for _ in range(50):
        with SessionLocal() as _db:
            _active = _db.query(K8sTask).filter(
                K8sTask.status.in_(("pending", "running"))).count()
        if not _active:
            break
        _time.sleep(0.1)
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
