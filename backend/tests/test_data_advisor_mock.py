#!/usr/bin/env python3
"""数据完整性顾问 mock 测试。

口径：不碰真实 qianshu.db——临时库 + 假 VM（label values）/ 假 Zabbix（JSON-RPC）
HTTP 服务真实出数，data_advisor 走真实 httpx 探测（不是 mock 探测函数）。
验证项：
  1) 六类缺口各自检出：应用日志未接 / 指标覆盖不全（VM+Zabbix 双源）/ 有账单无资产 /
     有资产无账单（子用例）/ 告警规则为空 / 画像拓扑未生成+待确认 / 域名缺登记+缺证书证据
  2) 全覆盖满分：score==100 且 gaps 为空
  3) 建议 entry 正确：每条缺口 entry 均为前端真实路由，且各面落对应入口
  4) 口径回归：SLS 隐式凭证源算已接入；告警缺口只算有阈值规则通道产品线；
     账单实例按产品线+主资源ID核对，已释放（Deleted 历史）不报、停用/vCenter 不参与；
     **不做账号级资产/账单错位**（付款账号、合并出账、vCenter 都合法）
  5) 指标分母 = CMS+VMware+Agent+K8s，无通道产品线只列明；缺口按通道给入口
  6) 日志源 queryable 与 status 同口径（无凭证的 SLS 不显示为空、不报"可查"）
  7) 只读零写库：check + tools_read.execute 前后全表行数一致
  8) 工具注册：tools_read 清单/分发/未知工具报错

用法：cd /opt/qianshu/backend && python3 tests/test_data_advisor_mock.py   （退出码 0=全过）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-dadv-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("advisor")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["SLS_APP_LOG"] = ""
os.environ["SLS_SLB_TARGETS"] = ""
os.environ["VM_URL"] = "http://127.0.0.1:1"
# 凭证不得从本机环境泄漏进测试（否则 SLS 隐式源会让"日志缺口"用例失效）
os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = ""
os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = ""
os.environ["TENCENTCLOUD_SECRET_ID"] = ""
os.environ["TENCENTCLOUD_SECRET_KEY"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect as sa_inspect, text  # noqa: E402

# 全量模型注册（models/__init__ 只收部分；create_all/计数需要全部表在 metadata 里）
import importlib  # noqa: E402
import pathlib  # noqa: E402

for _p in (pathlib.Path(__file__).resolve().parent.parent / "app" / "models").glob("*.py"):
    if _p.stem != "__init__":
        importlib.import_module(f"app.models.{_p.stem}")

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.alert_rule import AlertRule  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.cost_anomaly import CostAnomaly  # noqa: E402
from app.models.cost_daily import CostDaily  # noqa: E402
from app.models.domain_registry import DomainRegistry  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.models.machine import K8sMachine  # noqa: E402
from app.models.object_profile import ObjectProfile  # noqa: E402
from app.models.topology_scan import TopologyScan  # noqa: E402
from app.services import data_advisor
from app.services.copilot import tools_read

PASS, FAIL = [], []

# 前端真实路由（entry 白名单；router/index.js 对照）
REAL_ENTRIES = {
    "/integrations?tab=data", "/clouds", "/ops", "/bootstrap", "/monitor",
    "/alerts?tab=rules", "/profiles", "/topology", "/expiry", "/expiry?tab=domains",
    "/synth",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假 VM（Prom 兼容 label values）与假 Zabbix（JSON-RPC） ----------------

VM_IDS: set = set()       # 运行期可改：instance_id 标签值
ZB_HOSTS: set = set()     # 运行期可改：受监控主机名


class _VMHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/v1/label/instance_id/values"):
            body = json.dumps({"status": "success", "data": sorted(VM_IDS)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


class _ZBHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        method = req.get("method")
        if method == "apiinfo.version":
            result = "6.0.0"
        elif method == "user.login":
            result = "sess-token"
        elif method == "host.get":
            result = [{"host": h, "name": h} for h in sorted(ZB_HOSTS)]
        else:
            result = True   # user.logout 等
        body = json.dumps({"jsonrpc": "2.0", "result": result, "id": req.get("id", 1)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler) -> tuple:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------- 库表工具 ----------------

def wipe():
    conn = engine.connect()
    trans = conn.begin()
    for t in reversed(Base.metadata.sorted_tables):
        conn.execute(t.delete())
    trans.commit()
    conn.close()


def counts() -> dict:
    insp = sa_inspect(engine)
    out = {}
    with SessionLocal() as db:
        for t in insp.get_table_names():
            out[t] = db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
    return out


def seed_account(db, account_id: int, name: str, vendor: str = "aliyun",
                 enabled: bool = True, with_creds: bool = False):
    db.add(CloudAccount(
        id=account_id, name=name, vendor=vendor, enabled=enabled,
        ak_cipher=encrypt_secret("ak-test") if with_creds else "",
        secret_cipher=encrypt_secret("sk-test") if with_creds else "",
    ))
    db.commit()


def seed_assets(db, specs, account_id: int = 1):
    """specs: [(instance_id, product, name)]，默认 account_id=1。"""
    for iid, product, name in specs:
        db.add(Asset(account_id=account_id, product=product, instance_id=iid, name=name,
                     region="cn-hangzhou", status="Running"))
    db.commit()


# ---------------- 用例 ----------------

def case_six_gaps(vm_url: str, zb_url: str):
    print("[1] 六类缺口各自检出（VM+Zabbix 双源真实探测）")
    wipe()
    VM_IDS.clear()
    VM_IDS.add("i-a")                       # 只覆盖 i-a
    ZB_HOSTS.clear()
    ZB_HOSTS.add("srv-i-b")                 # Zabbix 按主机名覆盖 i-b
    settings.LOG_SOURCES = "[]"
    settings.SLS_APP_LOG = ""
    settings.METRIC_SOURCES = json.dumps([
        {"name": "vm1", "type": "vm", "url": vm_url},
        {"name": "zb1", "type": "zabbix", "url": zb_url, "token": "tk"},
    ])
    db = SessionLocal()
    seed_assets(db, [("i-a", "ecs", "srv-i-a"), ("i-b", "ecs", "srv-i-b"),
                     ("i-c", "ecs", "srv-i-c"), ("i-r1", "rds", "db-i-r1")])
    db.add(CostDaily(account_id=1, stat_date=date.today() - timedelta(days=1),
                     source="bill", product_code="ecs", instance_id="i-ghost",
                     billing_item="item", pretax_amount=1.0))
    db.add(LinkEdge(chain="outer", host="ghost.example.com", upstream_addr="10.0.0.1:80"))
    db.add(CostAnomaly(detect_date=date.today(), type="profile_drift", target_key="k1",
                       title="漂移1", ack_status="open"))
    db.add(CostAnomaly(detect_date=date.today(), type="chain_drift", target_key="k2",
                       title="漂移2", ack_status="open"))
    db.commit()

    out = data_advisor.check(db)
    areas = {g["area"] for g in out["gaps"]}
    check("六面缺口全覆盖", set(data_advisor.AREA_LABELS) <= areas, str(sorted(areas)))
    by_area = {}
    for g in out["gaps"]:
        by_area.setdefault(g["area"], []).append(g["gap"])

    check("日志缺口检出", any("无可查询的日志源" in s for s in by_area.get("logs", [])),
          str(by_area.get("logs")))
    check("指标缺口检出（2 台 CMS 资产无覆盖）",
          any(s.startswith("2 台云监控产品资产无指标") for s in by_area.get("metrics", [])),
          str(by_area.get("metrics")))
    check("有账单无资产检出", any("有账单无资产" in s for s in by_area.get("cloud", [])),
          str(by_area.get("cloud")))
    check("告警规则缺口检出", any(("无告警规则覆盖" in s) or ("告警规则为空" in s)
                                for s in by_area.get("alerts", [])),
          str(by_area.get("alerts")))
    check("画像未生成检出", any("对象画像未生成" in s for s in by_area.get("graph", [])),
          str(by_area.get("graph")))
    check("拓扑未生成检出", any("拓扑未生成" in s for s in by_area.get("graph", [])),
          str(by_area.get("graph")))
    check("待确认量检出", any("2 条画像/链路变化待确认" in s for s in by_area.get("graph", [])),
          str(by_area.get("graph")))
    check("域名缺登记检出", any("缺登记" in s for s in by_area.get("domains", [])),
          str(by_area.get("domains")))
    check("证书缺到期证据检出", any("缺到期证据" in s for s in by_area.get("domains", [])),
          str(by_area.get("domains")))
    check("得分在 0-100 且不满分", 0 <= out["score"] < 100, str(out["score"]))
    ratio_of = {a["area"]: a["ratio"] for a in out["areas"]}
    seq = [g["area"] for g in out["gaps"]]
    check("gaps 按面覆盖率升序（越缺越前）",
          seq == sorted(seq, key=lambda a: ratio_of[a]),
          str([(a, ratio_of[a]) for a in seq]))

    # entry 正确：全量白名单 + 各面落对应入口
    entries = {g["entry"] for g in out["gaps"]}
    check("entry 全部为真实路由", entries <= REAL_ENTRIES, str(entries - REAL_ENTRIES))
    want = {"logs": "/clouds", "metrics": "/clouds", "cloud": "/ops",
            "alerts": "/alerts?tab=rules"}
    for area, ent in want.items():
        got = {g["entry"] for g in out["gaps"] if g["area"] == area}
        check(f"entry 正确（{area}→{ent}）", got == {ent}, str(got))
    dom_entries = {g["entry"] for g in out["gaps"] if g["area"] == "domains"}
    check("entry 正确（domains→登记/拨测）", dom_entries == {"/expiry", "/synth"}, str(dom_entries))
    check("entry 正确（graph→画像/拓扑/链路入口）",
          {g["entry"] for g in out["gaps"] if g["area"] == "graph"} <= {"/profiles", "/topology", "/bootstrap"},
          str({g["entry"] for g in out["gaps"] if g["area"] == "graph"}))
    for g in out["gaps"]:
        if not all(g.get(k) for k in ("area", "gap", "impact", "suggestion", "entry")):
            check("缺口字段齐全", False, str(g))
            break
    else:
        check("缺口字段齐全", True)
    db.close()


def case_asset_without_bill():
    print("[2] 子用例：有资产无账单（账单未接入）")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_assets(db, [("i-a", "ecs", "srv-i-a")])
    db.commit()
    out = data_advisor.check(db)
    cloud = [g for g in out["gaps"] if g["area"] == "cloud"]
    check("有资产无账单检出", any("有资产无账单" in g["gap"] for g in cloud), str(cloud))
    check("私有化可忽略提示在建议里", any("私有化" in g["suggestion"] for g in cloud), str(cloud))
    check("entry=/clouds", all(g["entry"] == "/clouds" for g in cloud) and bool(cloud), str(cloud))
    db.close()


def case_logs_query_source():
    print("[3] 日志面与查询页同源（SLS 隐式凭证源不算未接入）")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.SLS_APP_LOG = ""
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "reader", with_creds=True)
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "logs")
    logs_gaps = [g for g in out["gaps"] if g["area"] == "logs"]
    check("有阿里云凭证即视为日志已接入", area["ratio"] == 1.0 and not logs_gaps,
          f"{area} {logs_gaps}")
    check("detail 标明隐式 SLS 源", "SLS" in area["detail"], area["detail"])

    from app.services import bootstrap as bootstrap_svc
    report = bootstrap_svc.local_report(db)
    logs_face = next(f for f in report["faces"] if f["key"] == "logs")
    check("底座体检日志面同口径（绿色、可查询）",
          logs_face["status"] == "green" and "可查询日志源" in logs_face["detail"],
          str(logs_face))
    db.close()


def case_alert_capability():
    print("[4] 告警面只对有指标通道的产品线计缺口 + 停用账号资产不计入")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "reader", enabled=True)
    seed_account(db, 9, "old-off", enabled=False)
    seed_assets(db, [("i-e1", "ecs", "ecs-1"), ("o-1", "oss", "bucket-1"),
                     ("c-1", "cdn", "cdn-1"), ("w-1", "waf", "waf-1")], account_id=3)
    seed_assets(db, [("h-old", "host", "old-host")], account_id=9)
    db.add(AlertRule(name="r-ecs", source="metric", product="ecs", metric="CPUUtilization",
                     threshold=90.0, enabled=True, account_id=3))
    db.commit()
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "alerts")
    alerts_gaps = [g for g in out["gaps"] if g["area"] == "alerts"]
    check("ecs 已覆盖 + oss/cdn/waf 不制造缺口", area["ratio"] == 1.0 and not alerts_gaps,
          f"{area} {alerts_gaps}")
    check("无指标通道产品线在 detail 如实列出",
          all(p in area["detail"] for p in ("oss", "waf")) and "cdn" in area["detail"],
          area["detail"])
    check("停用账号的 host 资产不计入告警面", "host" not in area["detail"], area["detail"])
    db.close()


def case_billing_normalized():
    print("[5] 账单实例按产品线+主资源ID核对，停用/vCenter 账号不参与")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "reader", vendor="aliyun", enabled=True)
    seed_account(db, 4, "vc", vendor="vcenter", enabled=True)
    seed_account(db, 9, "old-off", vendor="aliyun", enabled=False)
    seed_assets(db, [("i-1", "ecs", "ecs-1"), ("r-1", "redis", "redis-1")], account_id=3)
    seed_assets(db, [("vm-1", "vmware_vm", "vm-1")], account_id=4)
    today = date.today() - timedelta(days=1)
    for code, iid in (("ecs", "i-1"), ("kvstore", "r-1;backup"), ("nas", "010ec4b855;hybrid"),
                      ("cdn", "cdn-example-0001;AP1")):
        db.add(CostDaily(account_id=3, stat_date=today, source="bill", product_code=code,
                         instance_id=iid, billing_item="item", pretax_amount=1.0))
    db.add(CostDaily(account_id=9, stat_date=today, source="bill", product_code="ecs",
                     instance_id="i-ghost", billing_item="item", pretax_amount=1.0))
    db.commit()
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "cloud")
    cloud_gaps = [g for g in out["gaps"] if g["area"] == "cloud"]
    check("归一后无假缺口", not cloud_gaps and area["ratio"] == 1.0, f"{area} {cloud_gaps}")
    check("detail 显示 0 个不在资产库", "0 个不在资产库" in area["detail"], area["detail"])
    check("停用账号账单与 vCenter 不参与错位", "账号 1 个" in area["detail"], area["detail"])
    db.close()


def case_cloud_account_roles():
    print("[6] 账号级不判错位：付款账号/合并出账/vCenter 都是合法形态")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "member", vendor="aliyun", enabled=True)    # 有资产、无自己的账单
    seed_account(db, 4, "vc", vendor="vcenter", enabled=True)       # 根本没有账单能力
    seed_account(db, 5, "payer", vendor="aliyun", enabled=True)     # 有账单、无资源
    seed_assets(db, [("i-1", "ecs", "ecs-1")], account_id=3)
    seed_assets(db, [("vm-1", "vmware_vm", "vm-1")], account_id=4)
    db.add(CostDaily(account_id=5, stat_date=date.today() - timedelta(days=1),
                     source="bill", product_code="ecs", instance_id="i-1",
                     billing_item="item", pretax_amount=1.0))
    db.commit()
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "cloud")
    cloud_gaps = [g for g in out["gaps"] if g["area"] == "cloud"]
    check("付款/成员/vCenter 形态不报账号错位",
          not cloud_gaps and area["ratio"] == 1.0, f"{area} {cloud_gaps}")
    check("detail 明示账号级不做错位判定", "不做错位判定" in area["detail"], area["detail"])
    check("账单侧只统计计费账号（1 个）", "账号 1 个" in area["detail"], area["detail"])
    db.close()


def case_billing_health():
    print("[7] 账单健康：新接入给宽限 / 有史停更报断供")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "reader", vendor="aliyun", enabled=True)
    seed_assets(db, [("i-1", "ecs", "ecs-1")], account_id=3)
    db.commit()
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "cloud")
    cloud_gaps = [g for g in out["gaps"] if g["area"] == "cloud"]
    check("新接入无账单不报缺口", not cloud_gaps and area["ratio"] == 1.0,
          f"{area} {cloud_gaps}")
    check("detail 说明 T+1 宽限", "新接入" in area["detail"], area["detail"])

    db.add(CostDaily(account_id=3, stat_date=date.today() - timedelta(days=5),
                     source="bill", product_code="ecs", instance_id="i-1",
                     billing_item="item", pretax_amount=1.0))
    db.commit()
    out = data_advisor.check(db)
    by_area = {}
    for g in out["gaps"]:
        by_area.setdefault(g["area"], []).append(g)
    check("停更 5 天检出账单断供",
          any("账单数据断供 5 天" in g["gap"] for g in by_area.get("cloud", [])),
          str(by_area.get("cloud")))
    check("断供入口=云账号管理（核查权限）",
          all(g["entry"] == "/clouds" for g in by_area.get("cloud", [])),
          str(by_area.get("cloud")))
    db.close()


def case_released_vs_orphan():
    print("[8] 已释放实例（资产历史 Deleted）不算孤儿，从未同步的才算")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "reader", vendor="aliyun", enabled=True)
    seed_assets(db, [("i-live", "ecs", "ecs-live"), ("i-old", "ecs", "ecs-old")],
                account_id=3)
    db.query(Asset).filter(Asset.instance_id == "i-old").update({"status": "Deleted"})
    for iid in ("i-live", "i-old", "i-ghost"):
        db.add(CostDaily(account_id=3, stat_date=date.today() - timedelta(days=1),
                         source="bill", product_code="ecs", instance_id=iid,
                         billing_item="item", pretax_amount=1.0))
    db.commit()
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "cloud")
    cloud_gaps = [g for g in out["gaps"] if g["area"] == "cloud"]
    text = " ".join(g["gap"] for g in cloud_gaps)
    check("只有从未同步的 i-ghost 报有账单无资产",
          len(cloud_gaps) == 1 and "i-ghost" in text and "i-old" not in text,
          f"{area} {cloud_gaps}")
    check("detail 把已释放单独计数", "已释放" in area["detail"], area["detail"])
    check("近 7 天核对 3 个、1 个不在资产库",
          "近 7 天可核对实例级资源 3 个，1 个不在资产库" in area["detail"], area["detail"])
    db.close()


def case_metrics_channels(vm_url: str):
    print("[9] 指标分母含 VMware/Agent/K8s，无通道产品线只列明；缺口按通道给入口")
    wipe()
    VM_IDS.clear()
    VM_IDS.update({"i-cms", "vm-ok", "h-ok"})
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.METRIC_SOURCES = json.dumps([{"name": "vm1", "type": "vm", "url": vm_url}])
    db = SessionLocal()
    seed_account(db, 3, "reader", vendor="aliyun", enabled=True)
    seed_account(db, 4, "vc", vendor="vcenter", enabled=True)
    seed_assets(db, [("i-cms", "ecs", "ecs-ok"), ("i-miss", "ecs", "ecs-miss"),
                     ("c-1", "cvm", "tencent-cvm"), ("o-1", "oss", "bucket-1"),
                     ("h-ok", "host", "agent-host")], account_id=3)
    seed_assets(db, [("vm-ok", "vmware_vm", "vm-ok"), ("vm-miss", "vmware_vm", "vm-miss"),
                     ("vm-off", "vmware_vm", "vm-off")], account_id=4)
    db.query(Asset).filter(Asset.instance_id == "vm-off").update({"status": "stopped"})
    db.add(K8sMachine(host_name="node-x", ip="10.0.0.9"))
    db.commit()
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "metrics")
    gaps = [g for g in out["gaps"] if g["area"] == "metrics"]
    text = " ".join(g["gap"] for g in gaps)
    entries = {g["entry"] for g in gaps}
    check("VMware/Agent 已覆盖，只报 CMS/VMware/K8s 三类缺口",
          "ecs-miss" in text and "vm-miss" in text and "node-x" in text
          and "agent-host" not in text and "vm-ok" not in text, text)
    check("缺口按通道给真实入口",
          entries == {"/clouds", "/monitor", "/integrations?tab=data"}, str(entries))
    check("无通道产品线只列 detail 不报缺口",
          "cvm 1" in area["detail"] and "oss 1" in area["detail"]
          and "cvm" not in text and "oss" not in text, area["detail"])
    check("关机 VM 排除且单独计数",
          "vm-off" not in text and "已排除关机/挂起 1 台" in area["detail"], area["detail"])
    check("分母包含 VMware/Agent/K8s",
          "VMware 2" in area["detail"] and "Agent 1" in area["detail"]
          and "K8s 1" in area["detail"], area["detail"])
    db.close()


def case_logs_queryable_status():
    print("[10] 日志源 queryable 与 status 同口径（无凭证/无地址不可选、空态引导出现）")
    from app.services import log_query

    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.METRIC_SOURCES = "[]"
    settings.LOG_SOURCES = json.dumps([{"name": "sls-manual", "type": "sls"}])
    db = SessionLocal()
    av = log_query.available_sources(db)
    src = av["sources"][0]
    check("登记 SLS 但无凭证 → 空态引导",
          av["empty"] is True and av["default"] is None, str(av))
    check("无凭证源不可选（queryable=False/status=no_credential）",
          src["queryable"] is False and src["status"] == "no_credential", str(src))
    out = data_advisor.check(db)
    logs_area = next(a for a in out["areas"] if a["area"] == "logs")
    check("顾问同口径判日志缺口", logs_area["ratio"] == 0.0
          and any(g["area"] == "logs" for g in out["gaps"]), str(logs_area))

    settings.LOG_SOURCES = json.dumps([{"name": "loki-empty", "type": "loki"}])
    av = log_query.available_sources(db)
    check("Loki 缺地址 → 不可查、空态引导",
          av["empty"] is True and av["sources"][0]["status"] == "pending", str(av))

    settings.LOG_SOURCES = json.dumps([{"name": "loki1", "type": "loki",
                                        "url": "http://loki:3100"}])
    av = log_query.available_sources(db)
    check("Loki 有地址 → 可查、默认源",
          av["empty"] is False and av["default"] == "loki1"
          and av["sources"][0]["queryable"] is True, str(av))

    # 快照（智能体上下文）必须与日志页同口径：配置源 + SLS 隐式源
    from app.services.copilot import snapshot as snap_mod
    ds = snap_mod._datasources(db)
    check("快照日志源与日志页同口径（含 status，ready 才计入启用）",
          any(x["name"] == "loki1" and x["status"] == "ready" for x in ds["log"])
          and ds["enabled_count"] >= 1, str(ds))

    settings.LOG_SOURCES = "[]"
    orig = log_query._sls_cred_source
    log_query._sls_cred_source = lambda _db: "云账号#3"
    try:
        ds = snap_mod._datasources(db)
        check("仅云账号凭证也可在快照看到隐式 SLS 源",
              any(x["name"] == log_query.IMPLICIT_SLS_NAME and x["status"] == "ready"
                  for x in ds["log"]), str(ds))
        out = data_advisor.check(db)
        logs_area = next(a for a in out["areas"] if a["area"] == "logs")
        check("隐式 SLS 源让顾问日志面就绪（不再误报未接入）",
              logs_area["ratio"] == 1.0
              and not any(g["area"] == "logs" for g in out["gaps"]), str(logs_area))
    finally:
        log_query._sls_cred_source = orig
    db.close()


def case_graph_account_scope():
    print("[11] 待确认异动按启用账号收窄（停用账号不拖累画像面）")
    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = "[]"
    settings.METRIC_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "on", vendor="aliyun", enabled=True)
    seed_account(db, 9, "off", vendor="aliyun", enabled=False)
    db.add(ObjectProfile(object_type="host", object_key="i-1", label="i-1",
                         learned=True, status="normal"))
    db.add(CostAnomaly(detect_date=date.today(), type="profile_drift", target_key="k-on",
                       title="on", ack_status="open", account_id=3))
    db.add(CostAnomaly(detect_date=date.today(), type="chain_drift", target_key="k-off",
                       title="off", ack_status="open", account_id=9))
    db.commit()
    out = data_advisor.check(db)
    area = next(a for a in out["areas"] if a["area"] == "graph")
    gaps = " ".join(g["gap"] for g in out["gaps"] if g["area"] == "graph")
    check("停用账号异动不计入待确认",
          "待确认变化 1 条" in area["detail"] and "1 条画像/链路变化待确认" in gaps,
          f"{area} {gaps}")
    db.close()


def case_rule_advisor_scope():
    print("[12] 自动建规则与顾问同口径（停用账号/域名证书/CDN 不进缺口）")
    from app.services import rule_advisor

    wipe()
    VM_IDS.clear()
    ZB_HOSTS.clear()
    settings.METRIC_SOURCES = "[]"
    settings.LOG_SOURCES = "[]"
    db = SessionLocal()
    seed_account(db, 3, "on", vendor="aliyun", enabled=True)
    seed_account(db, 9, "off", vendor="aliyun", enabled=False)
    seed_assets(db, [("i-on", "ecs", "ecs-on")], account_id=3)
    seed_assets(db, [("i-off", "ecs", "ecs-off")], account_id=9)
    seed_assets(db, [("cert-1", "ssl_cert", "cert-1"),
                     ("cdn-1", "cdn", "cdn-1"),
                     ("oss-1", "oss", "oss-1")], account_id=3)
    out = rule_advisor.analyze(db, account_id=0, days=7)
    s = out["summary"]
    check("停用账号资产不参与分析（217 口径一致）",
          s["assets"] == 4 and s["metric_assets"] == 1, str(s))
    check("域名证书/CDN 不计入无通道缺口",
          s["unsupported_assets"] == 1 and s["expiry_assets"] == 1
          and s["builtin_assets"] == 1, str(s))
    check("候选只来自启用账号",
          bool(out["candidates"]) and all(c["account_id"] == 3 for c in out["candidates"]),
          str(out["candidates"])[:200])
    text = " ".join(out["findings"])
    check("findings 分别说明到期预警与内置检测",
          "由到期预警覆盖" in text and "内置检测" in text
          and "SSL_CERT 1 台由到期预警覆盖" in text, text)
    db.close()


def case_entry_actions():
    print("[6] 缺口入口与页面真实动作对齐")
    check("日志入口=云账号管理（SLS 凭证来源）", data_advisor.ENTRY["logs"] == "/clouds")
    check("有账单无资产入口=运维管理（同步云资产）", data_advisor.ENTRY["cloud"] == "/ops")
    check("账号错位入口=云账号管理", data_advisor.ENTRY["cloud_accounts"] == "/clouds")
    check("链路入口=安装向导（SLS 目标配置）", data_advisor.ENTRY["links"] == "/bootstrap")
    check("域名缺登记入口=到期总账（登记外部域名）", data_advisor.ENTRY["expiry"] == "/expiry")
    check("缺证书证据入口=拨测（新增探测目标）", data_advisor.ENTRY["synth"] == "/synth")
    check("全部入口在白名单", set(data_advisor.ENTRY.values()) <= REAL_ENTRIES,
          str(set(data_advisor.ENTRY.values()) - REAL_ENTRIES))


def case_full_coverage(vm_url: str):
    print("[3] 全覆盖满分（score==100、gaps 空）")
    wipe()
    VM_IDS.clear()
    VM_IDS.update({"i-a", "i-r1"})
    ZB_HOSTS.clear()
    settings.LOG_SOURCES = json.dumps([{"name": "loki1", "type": "loki", "url": "http://loki:3100"}])
    settings.METRIC_SOURCES = json.dumps([{"name": "vm1", "type": "vm", "url": vm_url}])
    db = SessionLocal()
    seed_assets(db, [("i-a", "ecs", "srv-i-a"), ("i-r1", "rds", "db-i-r1")])
    today = date.today()
    for code, inst in (("ecs", "i-a"), ("rds", "i-r1")):
        db.add(CostDaily(account_id=1, stat_date=today - timedelta(days=1), source="bill",
                         product_code=code, instance_id=inst, billing_item="item",
                         pretax_amount=1.0))
    db.add(AlertRule(name="r-ecs", source="metric", product="ecs", metric="CPUUtilization",
                     threshold=90.0, enabled=True))
    db.add(AlertRule(name="r-rds", source="metric", product="rds", metric="CpuUsage",
                     threshold=90.0, enabled=True))
    db.add(ObjectProfile(object_type="host", object_key="i-a", label="srv-i-a",
                         learned=True, status="normal"))
    db.add(TopologyScan(scanned_at=datetime.now(), trigger="manual"))
    db.add(LinkEdge(chain="outer", host="www.example.com", upstream_addr="10.0.0.1:80"))
    db.add(DomainRegistry(domain="www.example.com",
                          cert_not_after=datetime.now() + timedelta(days=30), sources="link"))
    db.commit()

    out = data_advisor.check(db)
    check("全覆盖 score==100", out["score"] == 100, f"score={out['score']} gaps={out['gaps']}")
    check("全覆盖 gaps 为空", out["gaps"] == [], str(out["gaps"]))
    check("areas 六面齐全且 ratio=1",
          len(out["areas"]) == 6 and all(a["ratio"] == 1 for a in out["areas"]),
          str([(a["area"], a["ratio"]) for a in out["areas"]]))
    db.close()


def case_readonly_and_tool_reg(vm_url: str):
    print("[4] 只读零写库 + 工具注册")
    # 沿用全覆盖场景的数据（不 wipe），测零写库更有意义：含各类表
    db = SessionLocal()
    before = counts()
    out1 = data_advisor.check(db)
    out2 = tools_read.execute(db, "data_completeness_check", {})
    after = counts()
    check("check 零写库", before == after,
          str({k: (before.get(k), after.get(k)) for k in after if before.get(k) != after.get(k)}))
    check("tools_read.execute 与 check 同结果", out1["score"] == out2["score"] and out1["gaps"] == out2["gaps"])

    names = tools_read.tool_names()
    check("工具进 copilot 工具清单", "data_completeness_check" in names)
    spec = next((t for t in tools_read.catalog() if t["name"] == "data_completeness_check"), None)
    check("清单条目完整（desc/params/schema/source）",
          bool(spec) and bool(spec["desc"]) and spec["params"] == {}
          and spec["schema"].get("type") == "object" and spec["source"] == "data_advisor",
          str(spec))
    check("tool_prompt_block 含工具", "data_completeness_check" in tools_read.tool_prompt_block())
    try:
        tools_read.execute(db, "no_such_tool_xyz", {})
        check("未知工具报 ToolError", False, "未抛错")
    except tools_read.ToolError:
        check("未知工具报 ToolError", True)
    db.close()


def main():
    vm_srv, vm_url = _serve(_VMHandler)
    zb_srv, zb_url = _serve(_ZBHandler)
    Base.metadata.create_all(engine)
    try:
        case_six_gaps(vm_url, zb_url)
        case_asset_without_bill()
        case_logs_query_source()
        case_alert_capability()
        case_billing_normalized()
        case_cloud_account_roles()
        case_billing_health()
        case_released_vs_orphan()
        case_metrics_channels(vm_url)
        case_logs_queryable_status()
        case_graph_account_scope()
        case_rule_advisor_scope()
        case_entry_actions()
        case_full_coverage(vm_url)
        case_readonly_and_tool_reg(vm_url)
    finally:
        vm_srv.shutdown()
        zb_srv.shutdown()
    print(f"\n结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
