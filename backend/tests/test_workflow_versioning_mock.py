#!/usr/bin/env python3
"""自定义工作流版本历史、一键回滚与 YAML/JSON 导入导出 mock 测试。

运行：cd /opt/qianshu/backend && python3 tests/test_workflow_versioning_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
零外部网络（METRIC_SOURCES/LOG_SOURCES 置空）；不重启 8810（TestClient/ASGITransport）。

验证项：
  H 版本落档：创建落 v1 / 更新前旧版落档（含存量定义首改兜底）/ 列表含定义全文+当前版标注
  R 一键回滚：恢复为该版本内容→version+1 新行 / 恢复幂等（内容收敛、可重复、无重复行）/
    未知版本 404 / 白名单漂移的历史版本拒绝恢复（400，无副作用）
  I 导出导入 roundtrip：YAML+JSON 双格式、引号/换行/冒号/#/{{占位符}}/类型保真、
    敏感占位符原样、format 校验
  C 冲突语义：key 冲突 409 + overwrite 覆盖（version+1）/ 全量先校验后写入（不部分导入）/
    文件内 key 重复拒 / 坏格式 400
  A 审计：restore / import 落审计（动作码已登记 ACTION_LABELS）
  P 权限：管理四端点 admin 收口（operator/viewer 403）
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-wfv-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("wfv")
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
from app.models.audit import AuditLog  # noqa: E402
from app.models.workflow_def import WorkflowDef, WorkflowDefVersion  # noqa: E402

from app.services.copilot import tools_read  # noqa: E402
from app.services.copilot.workflows import custom as wf_custom  # noqa: E402
from app.services.copilot.workflows import def_io as wf_def_io  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假工具（monkeypatch tools_read.execute，catalog 用真的） ----------------

def fake_execute(db, name, params):
    return {"items": []}


tools_read.execute = fake_execute

# ---------------- 启动隔离应用 ----------------

from app.main import app  # noqa: E402

Base.metadata.create_all(db_engine)
STUB_USER = SimpleNamespace(id=1, username="admin", role="admin", is_active=True)
app.dependency_overrides[get_db] = lambda: SessionLocal()
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
    put=lambda url, **kw: _req("PUT", url, **kw),
    delete=lambda url, **kw: _req("DELETE", url, **kw),
)

BASE = "/api/agent/workflows"

# ---------------- H 版本落档 ----------------

print("== H 版本落档（创建落 v1 / UPDATE 前旧版落档 / 当前版标注） ==")
DEMO_STEPS = [
    {"tool": "query_cost", "label": "查成本", "params_template": {"days": "{{days}}"},
     "continue_on_fail": False},
    {"tool": "pack_pools", "label": "看资源包", "params_template": {}, "continue_on_fail": True},
]
r = client.post(f"{BASE}/custom", json={
    "key": "wf_ver", "name": "成本巡检", "domain": "分析域",
    "description": "v1 描述", "steps": DEMO_STEPS})
check("H 创建成功（version=1）", r.status_code == 200 and r.json().get("version") == 1, r.text[:200])

r = client.get(f"{BASE}/custom/wf_ver/versions")
d = r.json()
items = d.get("items") or []
check("H 创建后历史=1 条且当前版标注",
      r.status_code == 200 and len(items) == 1 and items[0]["version"] == 1
      and items[0]["current"] is True and items[0]["created_by"] == "admin"
      and items[0]["name"] == "成本巡检" and items[0]["steps_n"] == 2
      and d.get("current_version") == 1, r.text[:300])

r = client.put(f"{BASE}/custom/wf_ver", json={"name": "成本巡检 v2", "steps": DEMO_STEPS[:1]})
check("H PUT 更新 version+1（2）", r.status_code == 200 and r.json().get("version") == 2, r.text[:200])
r = client.get(f"{BASE}/custom/wf_ver/versions")
items = r.json().get("items") or []
old = next((it for it in items if it["version"] == 1), None)
cur = next((it for it in items if it["current"]), None)
check("H UPDATE 前旧版本落档（v1 内容=旧名/旧步骤）",
      len(items) == 2 and old is not None and old["current"] is False
      and old["name"] == "成本巡检" and old["steps_n"] == 2
      and old["def"]["description"] == "v1 描述", str(items)[:400])
check("H 当前版标注=v2（name=新名，def 全文可审）",
      cur is not None and cur["version"] == 2 and cur["name"] == "成本巡检 v2"
      and cur["def"]["steps"] == DEMO_STEPS[:1], str(cur)[:300])

# 存量定义兜底：直接写库绕过 create_def（功能上线前的老定义），首改也要把旧版落档
_db = SessionLocal()
_db.add(WorkflowDef(key="wf_legacy", name="存量定义", description="上线前建的",
                    domain="存量域", steps_json=json.dumps(DEMO_STEPS, ensure_ascii=False),
                    created_by="old_user", enabled=True, version=5))
_db.commit()
_db.close()
r = client.get(f"{BASE}/custom/wf_legacy/versions")
items = r.json().get("items") or []
check("H 存量定义未改过：列表补当前态（不写库）",
      len(items) == 1 and items[0]["version"] == 5 and items[0]["current"] is True
      and items[0]["name"] == "存量定义", r.text[:300])
r = client.put(f"{BASE}/custom/wf_legacy", json={"name": "存量定义改"})
items = client.get(f"{BASE}/custom/wf_legacy/versions").json().get("items") or []
by_v = {it["version"]: it for it in items}
check("H 存量定义首改：UPDATE 前旧版落档（v5=旧内容）+ 新版 v6 落行",
      r.status_code == 200 and r.json().get("version") == 6
      and 5 in by_v and by_v[5]["name"] == "存量定义" and by_v[5]["created_by"] == "old_user"
      and 6 in by_v and by_v[6]["name"] == "存量定义改" and by_v[6]["current"] is True,
      str(items)[:400])

# ---------------- R 一键回滚 ----------------

print("== R 一键回滚（version+1 新行 / 幂等 / 边界） ==")
r = client.post(f"{BASE}/custom/wf_ver/versions/1/restore")
d = r.json()
check("R 恢复 v1 成功（内容=该版本内容，version+1=3，restored_from）",
      r.status_code == 200 and d.get("name") == "成本巡检"
      and d.get("description") == "v1 描述" and d.get("version") == 3
      and d.get("restored_from") == 1 and d.get("steps") == DEMO_STEPS, r.text[:300])
items = client.get(f"{BASE}/custom/wf_ver/versions").json().get("items") or []
by_v = {it["version"]: it for it in items}
check("R 恢复→version+1 新行（v3 落档=恢复内容）+ 当前版标注 v3",
      3 in by_v and by_v[3]["def"]["name"] == "成本巡检"
      and by_v[3]["current"] is True and len(items) == 3, str(items)[:400])

r = client.post(f"{BASE}/custom/wf_ver/versions/1/restore")
d = r.json()
_db = SessionLocal()
n_rows = _db.query(WorkflowDefVersion).filter(WorkflowDefVersion.key == "wf_ver").count()
n_distinct = (_db.query(WorkflowDefVersion.version)
              .filter(WorkflowDefVersion.key == "wf_ver").distinct().count())
_db.close()
check("R 恢复幂等：再恢复内容收敛不变、version=4、无重复 (key,version) 行",
      r.status_code == 200 and d.get("name") == "成本巡检" and d.get("version") == 4
      and d.get("steps") == DEMO_STEPS and n_rows == 4 and n_distinct == 4,
      f"v={d.get('version')} rows={n_rows} distinct={n_distinct}")

r = client.post(f"{BASE}/custom/wf_ver/versions/999/restore")
check("R 未知历史版本 404", r.status_code == 404, r.text[:200])

# 白名单漂移：手工落一条不合规历史版本（run_shell 已非白名单），恢复应 400 且无副作用
_db = SessionLocal()
_db.add(WorkflowDefVersion(key="wf_ver", version=900,
                           def_json=json.dumps({
                               "key": "wf_ver", "name": "坏历史", "description": "",
                               "domain": "", "steps": [{"tool": "run_shell", "label": "危险"}],
                               "enabled": True}, ensure_ascii=False),
                           created_by="admin"))
_db.commit()
_db.close()
r = client.post(f"{BASE}/custom/wf_ver/versions/900/restore")
cur_ver = client.get(f"{BASE}/custom/wf_ver/versions").json().get("current_version")
check("R 白名单漂移的历史版本拒恢复（400，定义不动）",
      r.status_code == 400 and cur_ver == 4, f"{r.status_code} {r.text[:150]} ver={cur_ver}")

# ---------------- I 导出导入 roundtrip ----------------

print("== I 导出导入 roundtrip（YAML/JSON 双格式、类型保真、占位符原样） ==")
RT_STEPS = [
    {"tool": "host_metrics", "label": '查主机 "x": #1\n多行',
     "params_template": {"instance": 'a"b: {{inst}}\n#', "window": "24h"},
     "continue_on_fail": True},
    {"tool": "query_cost", "label": "查成本",
     "params_template": {"days": 7, "product": "7", "top": 1.5},
     "continue_on_fail": False},
    {"tool": "save_alert_rule", "label": "存告警规则",
     "params_template": {"name": '告警"规: #则', "source": "metric", "threshold": 90.5,
                         "duration": 300, "channel_ids": [1, 2], "silences": [],
                         "enabled": True},
     "continue_on_fail": False},
    {"tool": "reset_user_password", "label": "重置密码",
     "params_template": {"new_password": "{{pwd}}"}, "continue_on_fail": False},
]
r = client.post(f"{BASE}/custom", json={
    "key": "wf_rt", "name": '往返 "定义": #1', "domain": "分析域",
    "description": "含 {{占位}} 与冒号: 井号#", "steps": RT_STEPS})
check("I 往返定义创建（含数组/数字/布尔字面量）", r.status_code == 200, r.text[:300])

r = client.get(f"{BASE}/custom/wf_rt/export?format=yaml")
check("I 导出 YAML（content-type/文件名/敏感占位符原样）",
      r.status_code == 200 and "application/yaml" in r.headers.get("content-type", "")
      and 'qianshu-workflow-wf_rt.yaml' in r.headers.get("content-disposition", "")
      and '{{pwd}}' in r.text and 'qianshu.workflow.v1' in r.text, r.text[:300])
yaml_text = r.text
r = client.post(f"{BASE}/custom/import", json={"content": yaml_text, "overwrite": True})
d = r.json()
after = next((x for x in (client.get(f"{BASE}/custom").json().get("items") or [])
              if x["key"] == "wf_rt"), None)
check("I YAML 导入→内容逐字段一致（类型保真 7/\"7\"/1.5/数组/布尔）",
      r.status_code == 200 and d.get("imported") == 1
      and after is not None and after["steps"] == RT_STEPS
      and after["name"] == '往返 "定义": #1', json.dumps(after, ensure_ascii=False)[:400])

r = client.get(f"{BASE}/custom/wf_rt/export?format=json")
check("I 导出 JSON（两格式都提供）",
      r.status_code == 200 and "application/json" in r.headers.get("content-type", "")
      and json.loads(r.text)["workflows"][0]["steps"] == RT_STEPS, r.text[:200])
r = client.post(f"{BASE}/custom/import", json={"content": r.text, "overwrite": True})
after = next((x for x in (client.get(f"{BASE}/custom").json().get("items") or [])
              if x["key"] == "wf_rt"), None)
check("I JSON 导入→内容一致（roundtrip 闭环）",
      r.status_code == 200 and after is not None and after["steps"] == RT_STEPS)

r = client.get(f"{BASE}/custom/wf_rt/export?format=xml")
check("I format 非法 400", r.status_code == 400, r.text[:200])

# 裸定义对象（非导出包）也能导；手写 YAML（无引号 {{}} / 整行注释）能导
bare = {"key": "wf_bare", "name": "裸定义", "domain": "", "description": "",
        "enabled": True, "steps": DEMO_STEPS}
hand_yaml = (
    "# 手写 YAML（含整行注释）\n"
    "key: wf_hand\n"
    "name: \"手写定义\"\n"
    "domain: \"\"\n"
    "description: \"\"\n"
    "enabled: true\n"
    "steps:\n"
    "  - tool: query_cost\n"
    "    label: 查成本\n"
    "    params_template:\n"
    "      days: {{days}}\n"
    "    continue_on_fail: false\n")
r = client.post(f"{BASE}/custom/import",
                json={"content": json.dumps(bare, ensure_ascii=False)})
check("I 裸定义对象导入", r.status_code == 200 and r.json().get("imported") == 1, r.text[:200])
r = client.post(f"{BASE}/custom/import", json={"content": hand_yaml})
after = next((x for x in (client.get(f"{BASE}/custom").json().get("items") or [])
              if x["key"] == "wf_hand"), None)
check("I 手写 YAML 导入（{{}} 裸写/注释）",
      r.status_code == 200 and after is not None
      and after["steps"][0]["params_template"] == {"days": "{{days}}"}, r.text[:300])

# ---------------- C 冲突语义 ----------------

print("== C 冲突语义（409 + overwrite / 全量校验 / 坏格式） ==")
conflict_yaml = wf_def_io.dumps_yaml({"key": "wf_rt", "name": "被覆盖的名字",
                                      "domain": "", "description": "", "enabled": True,
                                      "steps": [DEMO_STEPS[0]]})
r = client.post(f"{BASE}/custom/import", json={"content": conflict_yaml})
after = next((x for x in (client.get(f"{BASE}/custom").json().get("items") or [])
              if x["key"] == "wf_rt"), None)
check("C key 冲突 409（不带 overwrite，定义不动）",
      r.status_code == 409 and "overwrite" in r.text
      and after is not None and after["name"] == '往返 "定义": #1', r.text[:200])
r = client.post(f"{BASE}/custom/import", json={"content": conflict_yaml, "overwrite": True})
after = next((x for x in (client.get(f"{BASE}/custom").json().get("items") or [])
              if x["key"] == "wf_rt"), None)
check("C overwrite 覆盖导入（version+1，内容=导入内容）",
      r.status_code == 200 and after is not None
      and after["name"] == "被覆盖的名字" and after["version"] == 4, r.text[:200])

bad_entries = [dict(bare, key="wf_all_or_nothing"),
               {"key": "wf_bad_import", "name": "坏", "steps": [{"tool": "run_shell"}]}]
r = client.post(f"{BASE}/custom/import", json={"content": json.dumps(bad_entries)})
keys_now = {x["key"] for x in (client.get(f"{BASE}/custom").json().get("items") or [])}
check("C 全量先校验后写入（400 且 wf_all_or_nothing 不落库）",
      r.status_code == 400 and "wf_all_or_nothing" not in keys_now, f"{r.text[:150]} keys={sorted(keys_now)}")

dup_yaml = wf_def_io.dumps_yaml([dict(bare, key="wf_dup"), dict(bare, key="wf_dup")])
r = client.post(f"{BASE}/custom/import", json={"content": dup_yaml})
check("C 文件内 key 重复拒（400）", r.status_code == 400 and "重复" in r.text, r.text[:200])

r = client.post(f"{BASE}/custom/import", json={"content": "::: not yaml\n\t- bad"})
check("C 坏格式 400", r.status_code == 400, r.text[:200])
r = client.post(f"{BASE}/custom/import", json={"content": '{"hello": 1}'})
check("C 无定义内容 400", r.status_code == 400, r.text[:200])

# ---------------- A 审计 ----------------

print("== A 审计（restore / import 落审计） ==")
_db = SessionLocal()
acts = {}
for a in ("agent.workflow.restore", "agent.workflow.import", "agent.workflow.update"):
    acts[a] = (_db.query(AuditLog).filter(AuditLog.action == a)
               .order_by(AuditLog.id.desc()).all())
restore_rows = acts["agent.workflow.restore"]
import_rows = acts["agent.workflow.import"]
r_detail = json.loads(restore_rows[0].detail) if restore_rows else {}
_db.close()
check("A 恢复审计落库（restored_from 可回溯，动作码已登记）",
      len(restore_rows) >= 2 and r_detail.get("restored_from") == 1
      and restore_rows[0].resource_id == "wf_ver"
      and restore_rows[0].username == "admin",
      f"n={len(restore_rows)} detail={r_detail}")
check("A 导入审计落库",
      len(import_rows) >= 2 and any(x.resource_id == "wf_rt" for x in import_rows),
      f"n={len(import_rows)}")
from app.services.audit import ACTION_LABELS  # noqa: E402
check("A 动作码全登记（不回落原码）",
      all(k in ACTION_LABELS for k in ("agent.workflow.restore", "agent.workflow.import",
                                       "agent.workflow.create", "agent.workflow.update",
                                       "agent.workflow.delete", "agent.workflow.save_as")))

# ---------------- P 权限 ----------------

print("== P 权限（管理四端点 admin 收口） ==")
STUB_USER.role = "viewer"
codes = [
    client.get(f"{BASE}/custom/wf_ver/versions").status_code,
    client.post(f"{BASE}/custom/wf_ver/versions/1/restore").status_code,
    client.get(f"{BASE}/custom/wf_rt/export").status_code,
    client.post(f"{BASE}/custom/import", json={"content": yaml_text}).status_code,
]
check("P viewer 四端点全 403", all(c == 403 for c in codes), str(codes))
STUB_USER.role = "operator"
codes = [
    client.get(f"{BASE}/custom/wf_ver/versions").status_code,
    client.post(f"{BASE}/custom/wf_ver/versions/1/restore").status_code,
    client.get(f"{BASE}/custom/wf_rt/export").status_code,
    client.post(f"{BASE}/custom/import", json={"content": yaml_text}).status_code,
]
check("P operator 四端点全 403", all(c == 403 for c in codes), str(codes))
STUB_USER.role = "admin"
r1 = client.get(f"{BASE}/custom/no_such_wf/versions")
r2 = client.get(f"{BASE}/custom/no_such_wf/export")
check("P 未知 key versions/export 404", r1.status_code == 404 and r2.status_code == 404,
      f"{r1.status_code}/{r2.status_code}")

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
