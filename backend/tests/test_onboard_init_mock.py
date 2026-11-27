#!/usr/bin/env python3
"""onboard_init（AI 引导初始化）mock 测试。

运行：cd /opt/qianshu/backend && python3 tests/test_onboard_init_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
外部 IO（云 provider 同步/探活、画像采集、VM、SLS）全部 monkeypatch 假数据；
链路草稿在"SLS 未配"场景走**真实** link_draft 降级路径（零网络、干净跳过）。

验证项：
  A 无源首跑（空库）：五步全过（skip 如实）、SLS 未配跳过真实发生、无资产跳过规则、
    产物报告四段结构 + highlights、运行记录落 agent_workflow_runs、审计落 bootstrap.ai_init
  B 有源首跑（假 provider/画像 + 真 change_tools 建单）：资产同步摘要、规则工单
    按 5 条/张分批（9 条 -> 2 张）、**绝不静默写**（alert_rules 表 0 行）、
    验证步逐项"可取到/取不到"、SLS 已配时真实探测口径
  C 幂等可重跑：二次运行 0 张新工单（已提议名跳过）、产物照常、无重复规则
  D API 链路：POST /api/agent/workflows/onboard_init/run 出 artifact + runs 可查
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-onboard-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("onboard")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["SLS_SLB_TARGETS"] = ""
os.environ["SLS_APP_LOG"] = ""
os.environ["SLS_CDN_TARGET"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine as db_engine, get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.models.agent_change import AgentChangeTicket, AgentWorkflowRun  # noqa: E402  create_all 前注册表
from app.models.alert_rule import AlertRule  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402

from app.services.copilot import workflows as wf  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


STUB_USER = SimpleNamespace(id=1, username="admin", role="admin", is_active=True)

from app.main import app  # noqa: E402  全量注册模型/路由后再建表

Base.metadata.create_all(db_engine)

def _ov_get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


app.dependency_overrides[get_db] = _ov_get_db
app.dependency_overrides[get_current_user] = lambda: STUB_USER


def _req(method: str, url: str, **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


client = SimpleNamespace(
    get=lambda url, **kw: _req("GET", url, **kw),
    post=lambda url, **kw: _req("POST", url, **kw),
)

CTX = {"user": STUB_USER, "user_id": 1, "session_id": 0}

# ================= A 无源首跑（空库，SLS 未配真实跳过） =================

print("== A 无源首跑（空库） ==")
db = SessionLocal()
r = wf.run_workflow(db, "onboard_init", {}, dict(CTX))
steps_a = {s["step"]: s for s in r["steps"]}
check("A 五步齐全", len(r["steps"]) == 5 and set(steps_a) == {
    "盘点已配置源", "同步资产与拓扑", "画像与全景对象", "生成默认告警规则", "验证数据可获取"},
    str([s["step"] for s in r["steps"]]))
check("A 无源全过（无 failed）", r["status"] == "done",
      str([(s["step"], s["status"], s["detail"][:60]) for s in r["steps"]]))
check("A 盘点如实：0 账号/0 数据源/SLS 未配/0 机器",
      steps_a["盘点已配置源"]["status"] == "ok"
      and "云账号 0 个" in steps_a["盘点已配置源"]["detail"]
      and "SLS 目标：未配置" in steps_a["盘点已配置源"]["detail"],
      steps_a["盘点已配置源"]["detail"][:200])
check("A 同步步跳过云资产（无账号）+ 链路草稿 SLS 未配真实跳过",
      "未登记云账号" in steps_a["同步资产与拓扑"]["detail"]
      and "链路草稿：跳过（SLS 目标未配置）" in steps_a["同步资产与拓扑"]["detail"],
      steps_a["同步资产与拓扑"]["detail"][:300])
check("A 无资产跳过规则生成", steps_a["生成默认告警规则"]["status"] == "skipped"
      and "跳过默认告警规则" in steps_a["生成默认告警规则"]["detail"],
      steps_a["生成默认告警规则"]["detail"][:200])
check("A 工单 0 张（不静默建单）", r["tickets"] == [], str(r["tickets"]))
art = r["artifact"]
check("A 产物四段结构 + highlights",
      all(k in (art.get("report_md") or "") for k in ("### 一、生成了什么", "### 二、跳过了什么",
                                                      "### 三、数据可获取验证", "### 四、下一步建议"))
      and len(art.get("highlights") or []) >= 2 and art.get("probes"),
      (art.get("report_md") or "")[:200])
check("A 报告如实写 SLS 跳过原因",
      any("SLS" in s for s in (art.get("skipped") or [])), str(art.get("skipped"))[:200])
n_runs = db.query(AgentWorkflowRun).filter(AgentWorkflowRun.workflow_key == "onboard_init").count()
check("A 运行记录落 agent_workflow_runs", n_runs == 1 and r["run_id"] > 0, f"runs={n_runs}")
check("A 审计落 bootstrap.ai_init",
      db.query(AuditLog).filter(AuditLog.action == "bootstrap.ai_init").count() == 1)
db.close()

# ================= B 有源首跑（假 provider/画像 + 真工单） =================

print("== B 有源首跑（假云同步/画像，真 change_tools 建单） ==")

# —— 假外部 IO ——
import app.collector.vm as _vm  # noqa: E402
import app.services.cloud.provider as _cloudp  # noqa: E402
import app.services.cloud.sync_job as _cloud_sync  # noqa: E402
import app.services.datasource as _ds  # noqa: E402
import app.services.link_draft as _ld  # noqa: E402
import app.services.object_profile as _op  # noqa: E402
import app.services.profile_engine as _pe  # noqa: E402
from app.core import config as _cfg  # noqa: E402

CALLS = {"sync": [], "test": [], "ld": 0}


class _FakeProvider:
    def sync(self, db_, acc, products=None):
        CALLS["sync"].append(acc.name)
        return [{"product": "ecs", "total": 5, "created": 5, "updated": 0}]

    def test_account(self, acc, ak, sk):
        CALLS["test"].append(acc.name)
        return {"ok": True, "message": "凭证有效，可见区域 3 个"}


_cloudp.get_provider = lambda vendor: _FakeProvider()
# sync_job 直接 import 了 get_provider，必须同时替换其模块内引用，不能假设与 provider 模块共享函数对象。
_cloud_sync.get_provider = lambda vendor: _FakeProvider()
_pe.run_profiles = lambda db_: {"profiles": 3, "snapshots": 2, "event_samples": 1,
                                "drifted": 0, "mutations": 0}
_op.run_object_profiles = lambda db_, backfill_days=0: {"snapshots": 4, "learned": 2, "events": 0}


def _fake_ld(db_):
    CALLS["ld"] += 1
    return {"edges": 12, "draft": 8, "confirmed": 4}


_ld.generate_link_draft = _fake_ld
_vm.query_range = lambda *a, **k: [{"metric": {"instance_id": "i-1"},
                                    "values": [["1760000000", "12.5"]]}]
_ds.probe_metric_source = lambda s, db=None: {"ok": True, "detail": "版本 2.9.0"}
_ds.probe_log_source = lambda s, db=None: {"ok": True, "detail": "版本 8.0"}
from app.collector import credential as _cred  # noqa: E402
from app.collector import sls as _sls  # noqa: E402

_cred.resolve_aliyun_credential = lambda db_: ("ak", "sk", "test")
_cred.resolve_sls_credential = lambda db_: ("ak", "sk", "test")
_sls.list_logstores = lambda ak, sk, project: ["access-log", "app-log"]
from app.services import topology as _topo  # noqa: E402

_topo.run_scan = lambda db_, trigger="manual", full=True: {
    "scan_id": 1, "stats": {"groups": 3, "links": 7, "members": 9}}

# —— 种子：1 云账号（真加密凭证）+ 4 类资产 ——
from app.core.security import encrypt_secret  # noqa: E402

db = SessionLocal()
db.add(CloudAccount(name="prod-aliyun", vendor="aliyun",
                    ak_cipher=encrypt_secret("AKTEST1234567890"),
                    secret_cipher=encrypt_secret("SKTEST1234567890"),
                    region="cn-hangzhou", enabled=True))
for product, iid, name in [("ecs", "i-1", "web1"), ("ecs", "i-2", "web2"),
                           ("rds", "rm-1", "db1"), ("redis", "r-1", "cache1"),
                           ("slb", "lb-1", "gw1")]:
    db.add(Asset(product=product, instance_id=iid, name=name, status="Running"))
db.commit()
db.close()

# SLS 已配（验证步真实探测口径；链路草稿已被 fake 接管，不受影响）+ 一个指标源
_cfg.settings.SLS_SLB_TARGETS = '[["proj-a", "store-a", "outer"]]'
_cfg.settings.METRIC_SOURCES = '[{"name": "vm-test", "type": "victoriametrics", "url": "http://x"}]'

db = SessionLocal()
r2 = wf.run_workflow(db, "onboard_init", {}, dict(CTX))
steps_b = {s["step"]: s for s in r2["steps"]}
check("B 五步全过", r2["status"] == "done",
      str([(s["step"], s["status"], s["detail"][:60]) for s in r2["steps"]]))
check("B 盘点日志源与日志查询同源（隐式 SLS 计入）",
      "可查询日志源 1 个（SLS（阿里云凭证））" in steps_b["盘点已配置源"]["detail"],
      steps_b["盘点已配置源"]["detail"][:300])
check("B 资产同步走了 provider（1 账号）", CALLS["sync"] == ["prod-aliyun"],
      str(CALLS["sync"]))
check("B 同步步 detail 含资产同步与拓扑", "云资产同步：1/1" in steps_b["同步资产与拓扑"]["detail"]
      and "拓扑扫描" in steps_b["同步资产与拓扑"]["detail"],
      steps_b["同步资产与拓扑"]["detail"][:300])
check("B 链路草稿生成（fake，SLS 已配路径）", CALLS["ld"] == 1
      and "链路草稿：edges=12" in steps_b["同步资产与拓扑"]["detail"],
      steps_b["同步资产与拓扑"]["detail"][:300])
check("B 画像步：首轮采样 + 全景对象", "行为画像首轮采样：画像 3 个" in steps_b["画像与全景对象"]["detail"]
      and "全景对象：4 个对象快照" in steps_b["画像与全景对象"]["detail"],
      steps_b["画像与全景对象"]["detail"][:200])

# 规则工单：ecs3 + rds3 + redis2 + slb1 = 9 条 -> 2 张（5+4）。
# 返回值 tickets 是脱敏出参（params 只留键名），条目真相在工单库 change_list_json。
tickets = r2["tickets"]
db_rows = (db.query(AgentChangeTicket)
           .filter(AgentChangeTicket.status == "pending").all())
all_items = [it for row in db_rows for it in json.loads(row.change_list_json or "[]")]
check("B 规则工单 9 条 -> 2 张（MAX_ITEMS=5 分批）",
      len(tickets) == 2 and len(db_rows) == 2 and len(all_items) == 9,
      f"tickets={len(tickets)} rows={len(db_rows)} items={len(all_items)}")
check("B 工单项全部 save_alert_rule（工单库真相）",
      all(it.get("tool") == "save_alert_rule" for it in all_items),
      str([it.get("tool") for it in all_items])[:200])
rule_names = {(it.get("params") or {}).get("name") for it in all_items}
check("B 规则名按资产类型齐全",
      {"默认规则·ECS·CPU使用率", "默认规则·ECS·内存使用率", "默认规则·ECS·磁盘使用率",
       "默认规则·RDS·CPU使用率", "默认规则·RDS·磁盘使用率", "默认规则·RDS·连接数使用率",
       "默认规则·Redis·CPU使用率", "默认规则·Redis·内存使用率",
       "默认规则·SLB·5xx状态码"} == rule_names,
      str(sorted(rule_names)))
check("B 绝不静默写：alert_rules 表 0 行",
      db.query(AlertRule).count() == 0, f"rows={db.query(AlertRule).count()}")
check("B 未确认不生效（工单 pending）",
      db.query(AgentChangeTicket).filter(AgentChangeTicket.status == "pending").count() == 2)

# 验证步：逐项可取到/取不到（真实探测口径，假返回）
probes = (r2["artifact"].get("probes") or [])
by_src = {p["source"]: p for p in probes}
check("B 验证：云账号探活走 provider.test_account", CALLS["test"] == ["prod-aliyun"]
      and by_src.get("云账号·prod-aliyun", {}).get("ok") is True,
      str(by_src.get("云账号·prod-aliyun")))
check("B 验证：指标源探测可取到", by_src.get("指标源·vm-test", {}).get("ok") is True,
      str(by_src.get("指标源·vm-test")))
check("B 验证：SLS 已配真实探测可取到（logstore 数）",
      by_src.get("SLS 日志目标", {}).get("ok") is True
      and "2 个 logstore" in by_src.get("SLS 日志目标", {}).get("detail", ""),
      str(by_src.get("SLS 日志目标")))
check("B 验证：指标库/账单/机器逐项有结论",
      all(k in by_src for k in ("指标库（VictoriaMetrics）", "账单", "资产台账", "机器（Agent）")),
      str(sorted(by_src)))
check("B 产物 highlights 含规则与验证结论",
      any("待确认工单" in h for h in (r2["artifact"].get("highlights") or []))
      and any("数据可获取验证" in h for h in (r2["artifact"].get("highlights") or [])),
      str(r2["artifact"].get("highlights"))[:300])
check("B 报告写明内置覆盖（证书/资源包）",
      any("证书" in s for s in (r2["artifact"].get("skipped") or []))
      and any("资源包" in s for s in (r2["artifact"].get("skipped") or [])),
      str(r2["artifact"].get("skipped"))[:300])
db.close()

# ================= C 幂等可重跑 =================

print("== C 幂等可重跑 ==")
db = SessionLocal()
r3 = wf.run_workflow(db, "onboard_init", {}, dict(CTX))
steps_c = {s["step"]: s for s in r3["steps"]}
check("C 重跑全过", r3["status"] == "done",
      str([(s["step"], s["status"]) for s in r3["steps"]]))
check("C 重跑 0 张新工单（已提议名跳过）", r3["tickets"] == [],
      str(steps_c["生成默认告警规则"]["detail"])[:200])
check("C 规则步如实报已齐/已提议",
      "已齐" in steps_c["生成默认告警规则"]["detail"]
      or "已在待确认工单" in steps_c["生成默认告警规则"]["detail"],
      steps_c["生成默认告警规则"]["detail"][:200])
check("C 仍然 0 静默写 + 2 张 pending 未重复",
      db.query(AlertRule).count() == 0
      and db.query(AgentChangeTicket).filter(AgentChangeTicket.status == "pending").count() == 2)
check("C 运行记录累积（3 次）",
      db.query(AgentWorkflowRun).filter(AgentWorkflowRun.workflow_key == "onboard_init").count() == 3)

# 确认后（规则落库）再跑：已存在规则不再提议
db.add(AlertRule(name="默认规则·ECS·CPU使用率", source="metric", level="warning",
                 product="ecs", metric="CPUUtilization", comparator=">", threshold=90.0,
                 duration=300))
db.commit()
r4 = wf.run_workflow(db, "onboard_init", {}, dict(CTX))
steps_d = {s["step"]: s for s in r4["steps"]}
check("C 已存在规则不重复提议（落库后重跑 0 新单）", r4["tickets"] == [],
      steps_d["生成默认告警规则"]["detail"][:200])
db.query(AlertRule).delete()   # 清掉本段种的规则，D 段继续断言"全流程 0 静默写"
db.commit()
db.close()

# ================= D API 链路 =================

print("== D API 链路（TestClient，不碰 8810） ==")
r = client.get("/api/agent/workflows")
items = r.json().get("items") or []
onboard = [i for i in items if i.get("key") == "onboard_init"]
check("D 清单含 onboard_init（引导域）",
      r.status_code == 200 and onboard and onboard[0].get("domain") in ("引导域", "Bootstrap")
      and onboard[0].get("name"), r.text[:200])
r = client.post("/api/agent/workflows/onboard_init/run", json={"params": {}})
d = r.json()
check("D POST run 出 artifact + steps",
      r.status_code == 200 and d.get("run_id") and len(d.get("steps") or []) == 5
      and d.get("artifact", {}).get("report_md"), r.text[:300])
check("D run 后 rules 仍 0 行（API 链路也不静默写）",
      SessionLocal().query(AlertRule).count() == 0)
r = client.get("/api/agent/workflows/runs?workflow_key=onboard_init&limit=5")
items = r.json().get("items") or []
check("D runs 可查（含 steps/artifact 回放）",
      r.status_code == 200 and len(items) >= 4 and items[0].get("artifact"),
      r.text[:200])

# ================= E 产品级失败不能被工作流成功吞掉 =================

print("== E 分产品失败与共享 Run ==")


class _PartialProvider(_FakeProvider):
    def sync(self, db_, acc, products=None):
        return [{"product": "ecs", "ok": True, "total": 5},
                {"product": "rds", "ok": False, "error": "mock rds denied"}]


_cloud_sync.get_provider = lambda vendor: _PartialProvider()
db = SessionLocal()
r5 = wf.run_workflow(db, "onboard_init", {}, dict(CTX))
steps_e = {s["step"]: s for s in r5["steps"]}
sync_step = steps_e["同步资产与拓扑"]
check("E 单产品失败使同步步骤 failed", sync_step["status"] == "failed"
      and "mock rds denied" in sync_step["detail"], str(sync_step)[:400])
account = db.query(CloudAccount).filter(CloudAccount.name == "prod-aliyun").one()
from app.services import onboarding as _onboarding  # noqa: E402
run = _onboarding.get_run(db, account.id)
check("E 同步结果写入同一 Run 并保留 rds 失败",
      run is not None and any(x["key"] == "asset:rds" for x in _onboarding.serialize(run)["issues"]),
      _onboarding.serialize(run) if run else None)
db.close()

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
