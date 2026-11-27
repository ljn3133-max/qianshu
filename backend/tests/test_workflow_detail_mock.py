#!/usr/bin/env python3
"""工作流统一展示 mock 测试：detail 详情接口 + 内置「另存为自定义」。

运行：cd /opt/qianshu/backend && python3 tests/test_workflow_detail_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
工具取数走假工具（monkeypatch tools_read.execute），零外部网络。不重启 8810（TestClient/ASGITransport）。

验证项：
  A detail 内置 13 个全出：逐 key 200、步骤序列/工具/失败策略/参数表齐、敏感参数带标记
  B detail 自定义出 + 掩码：步骤 tool/params_template/continue_on_fail 齐；
    敏感字面量（直插 DB 模拟历史数据）出参掩码成 ****，凭证不出详情
  C 另存为副本幂等：-copy 后缀进自定义区、迁移工具步骤/内建步骤如实剔除（omitted_labels）；
    副本真跑（假工具）走引擎；同 key 再来=409 不覆盖用户副本；overwrite=true 幂等覆盖
    （version+1、单行不重复）；全内建步骤的内置 -> 400 明确文案（access_datasource）
  D 无权限拒：save-as 仅 admin（operator/viewer 403）；detail 读角色可用、非白名单角色 403
  E 清单互通：GET /workflows 带 steps_summary（内置/自定义都有），既有字段零变化
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-wfd-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("wfd")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine as db_engine  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.models.workflow_def import WorkflowDef  # noqa: E402

from app.services.copilot import tools_read  # noqa: E402
from app.services.copilot import workflows as wf  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假工具（monkeypatch tools_read.execute） ----------------

FAKE_CALLS: list = []


def fake_execute(db, name, params):
    FAKE_CALLS.append((name, dict(params or {})))
    return {"items": [{"mock": name}], "total": 1}


tools_read.execute = fake_execute

# ---------------- 启动隔离应用（全量注册模型/路由后建表） ----------------

from app.main import app  # noqa: E402

Base.metadata.create_all(db_engine)
STUB_USER = SimpleNamespace(id=1, username="admin", role="admin", is_active=True)
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

BASE = "/api/agent/workflows"
BUILTIN_KEYS = {"access_cloud", "access_datasource", "access_k8s", "access_virt",
                "cost_checkup", "fault_diagnosis", "capacity_checkup", "change_impact",
                "topology_refine", "alert_tune", "inspection_custom", "dashboard_custom",
                "onboard_init"}

# ---------------- A detail：内置 13 个全出 ----------------

print("== A detail 内置 13 个全出（步骤序列/工具/失败策略/参数表） ==")
seen = []
for key in sorted(BUILTIN_KEYS):
    r = client.get(f"{BASE}/{key}/detail")
    d = r.json() if r.status_code == 200 else {}
    ok = (r.status_code == 200 and d.get("builtin") is True and d.get("custom") is False
          and d.get("key") == key and d.get("name") and d.get("domain")
          and d.get("description") and isinstance(d.get("steps"), list) and d["steps"]
          and isinstance(d.get("params_schema"), dict)
          and all({"idx", "label", "kind", "tools", "continue_on_fail"} <= set(s)
                  for s in d["steps"]))
    if ok:
        seen.append(key)
    check(f"A detail 出步骤结构化详情: {key}", ok, r.text[:200])
check("A 内置 13 个 detail 全出", len(seen) == 13, f"ok={len(seen)}")

d = client.get(f"{BASE}/access_k8s/detail").json()
schema = d.get("params_schema") or {}
check("A 敏感参数带标记（token sensitive=true，name 不带）",
      schema.get("token", {}).get("sensitive") is True
      and schema.get("name", {}).get("sensitive") is False, json.dumps(schema)[:300])
d = client.get(f"{BASE}/alert_tune/detail").json()
by_label = {s["label"]: s for s in d["steps"]}
check("A 步骤序列/工具/失败策略齐（建变更工单=change 带工具，调参建议=内建）",
      by_label.get("建变更工单", {}).get("kind") == "change"
      and "save_alert_rule" in by_label.get("建变更工单", {}).get("tools", [])
      and by_label.get("建变更工单", {}).get("will_ticket") is True
      and by_label.get("调参建议", {}).get("kind") == "internal"
      and all(s.get("continue_on_fail") is True for s in d["steps"]),
      str(by_label)[:300])

r = client.get(f"{BASE}/cost_checkup/detail", headers={"Accept-Language": "en"})
d = r.json()
check("A detail 按 Accept-Language 出英文（name/domain 双语载体）",
      r.status_code == 200 and d.get("name") == "Cost checkup" and d.get("domain") == "Analysis",
      r.text[:200])

r = client.get(f"{BASE}/no_such_wf/detail")
check("A 未知 key 404", r.status_code == 404, r.text[:200])
r = client.get(f"{BASE}/custom/detail")
check("A 保留路由名 key 404（custom 不是工作流）", r.status_code == 404, r.text[:200])

# ---------------- B detail：自定义出 + 敏感掩码 ----------------

print("== B detail 自定义出 + 敏感参数掩码 ==")
DEMO_STEPS = [
    {"tool": "query_cost", "label": "查成本", "params_template": {"days": "{{days}}"}},
    {"tool": "pack_pools", "label": "看资源包", "params_template": {}, "continue_on_fail": True},
]
r = client.post(f"{BASE}/custom", json={
    "key": "wf_detail_demo", "name": "详情演示", "domain": "自定义域",
    "description": "detail 自定义出参", "steps": DEMO_STEPS})
check("B 自定义定义创建", r.status_code == 200, r.text[:200])
d = client.get(f"{BASE}/wf_detail_demo/detail").json()
check("B detail 自定义出（custom=True、步骤 tool/params_template/continue_on_fail 齐）",
      d.get("custom") is True and d.get("builtin") is False and d.get("key") == "wf_detail_demo"
      and len(d.get("steps") or []) == 2
      and d["steps"][0]["tool"] == "query_cost" and d["steps"][0]["params_template"] == {"days": "{{days}}"}
      and d["steps"][0]["continue_on_fail"] is False
      and d["steps"][1]["continue_on_fail"] is True
      and d.get("params_hint") == ["days"] and d.get("enabled") is True,
      json.dumps(d, ensure_ascii=False)[:300])

# 直插 DB 模拟历史/导入数据里的敏感字面量（API 校验层拒收，detail 层必须掩码兜底）
_db = SessionLocal()
_db.add(WorkflowDef(
    key="wf_secret_rows", name="历史敏感行", description="", domain="",
    steps_json=json.dumps([{"tool": "reset_user_password", "label": "重置密码",
                            "params_template": {"new_password": "S3CRETVALUE999"},
                            "continue_on_fail": False}]),
    created_by="admin", enabled=True, version=1))
_db.commit()
_db.close()
d = client.get(f"{BASE}/wf_secret_rows/detail").json()
dumped = json.dumps(d, ensure_ascii=False)
check("B 敏感参数掩码（****，凭证不出详情）",
      d.get("custom") is True
      and d["steps"][0]["params_template"].get("new_password") == "****"
      and "S3CRETVALUE999" not in dumped, dumped[:300])

# ---------------- C 另存为自定义（-copy 副本 + 幂等） ----------------

print("== C 内置「另存为自定义」：-copy 副本 / 迁移与剔除 / 幂等 ==")
# cost_checkup 已收敛为全内建步骤（无可迁移工具），用含工具步骤的 change_impact 验证迁移。
r = client.post(f"{BASE}/change_impact/save-as", json={"overwrite": False})
d = r.json()
check("C 另存为成功（key=change_impact-copy 进自定义区）",
      r.status_code == 200 and d.get("key") == "change_impact-copy"
      and d.get("custom") is True and d.get("source_key") == "change_impact"
      and d.get("version") == 1 and d.get("created_by") == "admin", r.text[:300])
check("C 迁移工具步骤 + 内建步骤如实剔除（omitted_labels）",
      d.get("copied_steps") == 2 and len(d.get("steps") or []) == 2
      and set(d.get("omitted_labels") or []) == {"画像漂移", "时间窗对齐"}
      and all(s.get("continue_on_fail") is True for s in d["steps"])
      and any(s.get("tool") == "recent_changes" for s in d["steps"]),
      json.dumps(d, ensure_ascii=False)[:400])
check("C 副本参数模板全部为 {{占位}}（原入参名直通，凭证不落副本定义）",
      any((s.get("params_template") or {}).get("hours") == "{{hours}}" for s in d["steps"])
      and all(str(v).startswith("{{") and str(v).endswith("}}")
              for s in d["steps"] for v in (s.get("params_template") or {}).values()),
      json.dumps(d.get("steps"), ensure_ascii=False)[:300])

_db = SessionLocal()
rows = _db.query(WorkflowDef).filter(WorkflowDef.key == "change_impact-copy").count()
_db.close()
check("C 副本落库单行", rows == 1, f"rows={rows}")

d2 = client.get(f"{BASE}/change_impact-copy/detail").json()
check("C 副本 detail 与自定义同构（可编辑语义：custom=True）",
      d2.get("custom") is True and d2.get("builtin") is False
      and len(d2.get("steps") or []) == 2, json.dumps(d2, ensure_ascii=False)[:200])

FAKE_CALLS.clear()
r = client.post(f"{BASE}/change_impact-copy/run",
                json={"params": {"hours": 24, "target": "api.example.com"}})
d = r.json()
check("C 副本真跑（假工具过引擎，占位符代入）",
      r.status_code == 200 and d.get("status") == "done"
      and any(name == "recent_changes" and str(params.get("hours")) == "24"
              for name, params in FAKE_CALLS), r.text[:300])

r = client.post(f"{BASE}/change_impact/save-as", json={"overwrite": False})
check("C 再次另存为（不带 overwrite）409——不覆盖用户副本", r.status_code == 409, r.text[:200])
r = client.post(f"{BASE}/change_impact/save-as", json={"overwrite": True})
d = r.json()
_db = SessionLocal()
rows = _db.query(WorkflowDef).filter(WorkflowDef.key == "change_impact-copy").count()
_db.close()
check("C overwrite 幂等覆盖（version=2、单行不重复）",
      r.status_code == 200 and d.get("version") == 2 and rows == 1,
      f"{r.text[:200]} rows={rows}")

r = client.post(f"{BASE}/access_datasource/save-as", json={"overwrite": False})
check("C 全内建步骤内置 -> 400 明确文案（无可迁移工具步骤，不硬塞假工具）",
      r.status_code == 400 and "内建逻辑" in r.text and "新建工作流" in r.text,
      r.text[:300])
r = client.post(f"{BASE}/wf_detail_demo/save-as", json={"overwrite": False})
check("C 自定义 key 不支持另存为（400）", r.status_code == 400, r.text[:200])
r = client.post(f"{BASE}/no_such_wf/save-as", json={"overwrite": False})
check("C 未知 key 另存为 400", r.status_code == 400, r.text[:200])

# ---------------- D 权限 ----------------

print("== D 权限：save-as 仅 admin / detail 读角色可用 ==")
STUB_USER.role = "operator"
r1 = client.post(f"{BASE}/change_impact/save-as", json={"overwrite": True})
r2 = client.get(f"{BASE}/change_impact/detail")
check("D operator：save-as 403 / detail 200",
      r1.status_code == 403 and r2.status_code == 200, f"{r1.status_code}/{r2.status_code}")
STUB_USER.role = "viewer"
r1 = client.post(f"{BASE}/change_impact/save-as", json={"overwrite": True})
r2 = client.get(f"{BASE}/change_impact/detail")
check("D viewer：save-as 403 / detail 200",
      r1.status_code == 403 and r2.status_code == 200, f"{r1.status_code}/{r2.status_code}")
STUB_USER.role = "guest"   # 白名单外角色
r1 = client.get(f"{BASE}/cost_checkup/detail")
r2 = client.get(f"{BASE}")
check("D 白名单外角色：detail 403 / 清单 403", r1.status_code == 403 and r2.status_code == 403,
      f"{r1.status_code}/{r2.status_code}")
STUB_USER.role = "admin"

# ---------------- E 清单互通（steps_summary，既有字段零变化） ----------------

print("== E GET /workflows 清单带步骤摘要 ==")
r = client.get(f"{BASE}")
items = r.json().get("items") or []
builtin = [i for i in items if not i.get("custom")]
customs = [i for i in items if i.get("custom")]
by_key = {i["key"]: i for i in items}
check("E 清单步骤摘要（内置 13 + 自定义都有 steps_summary）",
      r.status_code == 200 and len(builtin) == 13 and len(customs) == 3
      and all(isinstance(i.get("steps_summary"), list) and i["steps_summary"] for i in items),
      f"builtin={len(builtin)} custom={len(customs)}")
check("E 内置 steps_summary=步骤名链（cost_checkup 4 步）",
      by_key.get("cost_checkup", {}).get("steps_summary")
      == ["成本事实", "钱主要花在哪里", "哪里不正常", "现在先做什么"],
      str(by_key.get("cost_checkup", {}).get("steps_summary")))
check("E 既有字段零变化（key/name/domain/description/last_run/params_schema）",
      all({"key", "name", "domain", "description", "last_run", "params_schema"} <= set(i)
          for i in builtin)
      and all({"key", "name", "domain", "description", "last_run", "params_schema",
               "custom", "enabled", "params_hint"} <= set(i) for i in customs),
      str(list(builtin[0].keys())))

check("E 内置注册表 13 key 不变（只加不改）", set(wf.WORKFLOWS) == BUILTIN_KEYS)

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
