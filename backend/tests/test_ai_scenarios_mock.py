#!/usr/bin/env python3
"""多模型场景矩阵隔离 mock 测试：GET/PUT 场景矩阵 + 兜底解析 + 审计。

运行：cd /opt/qianshu/backend && python3 tests/test_ai_scenarios_mock.py
隔离性：MASTER_KEY/DATABASE_URL 等在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不调任何网络（LLM 全程零调用，resolve_model 只解密本地密文）。

验证项：
  A 路由注册：GET /ai/scenarios、PUT /ai/scenarios/{scenario} 挂进 openapi。
  B GET 场景矩阵：ask/inspection/diagnosis/copilot 4 场景全量（含未绑定行），
    未绑定 setting_id=0 / enabled=True / 空 setting_label，场景中文名正确。
  C PUT 绑定：绑定后 GET 回显 setting_label/model；同值重放幂等（状态不变）。
  D 未绑定兜底 resolve_model：场景未绑定 → 兜底第一个启用连接；绑定 → 用绑定连接；
    场景开关关 → resolve_model 返回 None（开关关闭时不兜底）；解绑 → 回兜底。
  E 场景开关关=enabled=False（GET 如实回显）+ resolve_model=None（开关真断电）。
  F 校验：未知场景 404；绑定不存在的连接 400；operator 写 403 / 读 200。
  G 审计落账：每次成功 PUT 落一条 ai_scenario.update（含 label 中文登记），失败请求不落账。
"""
import os
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-aiscen-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("aiscen")
os.environ["MASTER_KEY"] = "cd" * 32          # 64 hex 测试主密钥
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"           # 零数据源=零探测
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.ai_scenario import AiScenarioModel  # noqa: E402
from app.models.ai_setting import AiSetting  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.services.ai_scenarios import SCENARIOS, resolve_model  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---- 建表（隔离自建的 /tmp 临时库）----
Base.metadata.create_all(engine)
db = SessionLocal()

STUB_USER = SimpleNamespace(id=1, username="tester", role="admin", is_active=True)

# 两个模型连接（A、B 都启用：矩阵可绑定任一；兜底=第一个启用=A）
db.add(AiSetting(provider_label="conn-A", base_url="https://a.example/v1",
                 api_key_cipher=encrypt_secret("sk-test-A"), model="model-a", enabled=True))
db.add(AiSetting(provider_label="conn-B", base_url="https://b.example/v1",
                 api_key_cipher=encrypt_secret("sk-test-B"), model="model-b", enabled=True))
db.commit()
IDA = db.query(AiSetting).filter_by(provider_label="conn-A").one().id
IDB = db.query(AiSetting).filter_by(provider_label="conn-B").one().id

# ---- TestClient 壳（同 test_integrations_mock.py：不触发 lifespan/startup，零网络）----
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.main import app  # noqa: E402

app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_user] = lambda: STUB_USER
import asyncio  # noqa: E402


def _req(method: str, url: str, **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


client = SimpleNamespace(
    get=lambda url, **kw: _req("GET", url, **kw),
    put=lambda url, **kw: _req("PUT", url, **kw),
    post=lambda url, **kw: _req("POST", url, **kw),
)


def audit_count() -> int:
    return db.query(AuditLog).filter(AuditLog.action == "ai_scenario.update").count()


n_audit = 0

# ===== A 路由注册 =====
print("A 路由注册")
paths = {r.path for r in app.routes}
check("GET /ai/scenarios 已注册", "/api/ai/scenarios" in paths)
check("PUT /ai/scenarios/{scenario} 已注册", "/api/ai/scenarios/{scenario}" in paths)

# ===== B GET 场景矩阵（空表=4 场景全量含未绑定行）=====
print("B GET 场景矩阵")
r = client.get("/api/ai/scenarios")
rows = r.json()
check("4 场景全量", r.status_code == 200 and [x["scenario"] for x in rows] == list(SCENARIOS), str(rows))
check("场景中文名正确", [x["label"] for x in rows] == [v["zh"] for v in SCENARIOS.values()], str(rows))
check("未绑定行：setting_id=0 + 兜底默认 enabled=True",
      all(x["setting_id"] == 0 and x["enabled"] is True for x in rows), str(rows))
check("未绑定行：setting_label/model 为空（前端显示兜底文案）",
      all(x["setting_label"] == "" and x["model"] == "" for x in rows), str(rows))

# ===== D 未绑定兜底 resolve_model =====
print("D 兜底解析 resolve_model")
m = resolve_model(db, "inspection")
check("未绑定 → 兜底第一个启用连接（conn-A）",
      m is not None and m["setting_id"] == IDA and m["model"] == "model-a", str(m))
check("凭证解密可用", m is not None and m["api_key"] == "sk-test-A")

# ===== C PUT 绑定 =====
print("C PUT 绑定")
r = client.put("/api/ai/scenarios/ask", json={"setting_id": IDB, "enabled": True})
d = r.json()
check("绑定 ask→conn-B", r.status_code == 200 and d["setting_id"] == IDB
      and d["setting_label"] == "conn-B" and d["model"] == "model-b", str(d))
n_audit += 1

rows = client.get("/api/ai/scenarios").json()
ask = next(x for x in rows if x["scenario"] == "ask")
check("GET 回显绑定", ask["setting_id"] == IDB and ask["model"] == "model-b", str(ask))

m = resolve_model(db, "ask")
check("绑定后 resolve_model 用绑定连接（conn-B）", m["setting_id"] == IDB and m["model"] == "model-b", str(m))

r = client.put("/api/ai/scenarios/ask", json={"setting_id": IDB, "enabled": True})
d = r.json()
check("同值重放幂等（状态不变，可重复保存）", r.status_code == 200 and d["setting_id"] == IDB
      and d["enabled"] is True, str(d))
n_audit += 1
check("幂等：无重复行", db.query(AiScenarioModel).count() == 1)

# ===== E 场景开关关=enabled=False =====
print("E 场景开关")
r = client.put("/api/ai/scenarios/ask", json={"enabled": False})
d = r.json()
check("开关关=enabled=False", r.status_code == 200 and d["enabled"] is False and d["setting_id"] == IDB, str(d))
n_audit += 1
rows = client.get("/api/ai/scenarios").json()
ask = next(x for x in rows if x["scenario"] == "ask")
check("GET 如实回显 enabled=False", ask["enabled"] is False, str(ask))
m = resolve_model(db, "ask")
check("场景开关关 → resolve_model 返回 None（开关真断电，不兜底）", m is None, str(m))

# ===== 解绑回兜底 =====
r = client.put("/api/ai/scenarios/ask", json={"setting_id": 0, "enabled": True})
d = r.json()
check("setting_id=0 解绑回兜底", r.status_code == 200 and d["setting_id"] == 0
      and d["enabled"] is True and d["setting_label"] == "", str(d))
n_audit += 1
m = resolve_model(db, "ask")
check("解绑后 resolve_model 回兜底（conn-A）", m["setting_id"] == IDA, str(m))

# 空 body 不改状态（字段都可选）
r = client.put("/api/ai/scenarios/ask", json={})
d = r.json()
check("空 body=不改状态", r.status_code == 200 and d["setting_id"] == 0 and d["enabled"] is True, str(d))
n_audit += 1

# 仅带 enabled 的新建行：setting_id 走列默认 0（不绑连接）
r = client.put("/api/ai/scenarios/copilot", json={"enabled": False})
d = r.json()
check("新建行仅 enabled：默认不绑连接", r.status_code == 200 and d["setting_id"] == 0
      and d["enabled"] is False, str(d))
n_audit += 1

# 绑定的连接被删除 → GET 归一回兜底展示（与 resolve_model 兜底行为一致）
client.put("/api/ai/scenarios/diagnosis", json={"setting_id": IDB})
n_audit += 1
db.delete(db.get(AiSetting, IDB))
db.commit()
rows = client.get("/api/ai/scenarios").json()
diag = next(x for x in rows if x["scenario"] == "diagnosis")
check("绑定连接已删 → 归一兜底展示", diag["setting_id"] == 0 and diag["setting_label"] == "", str(diag))
check("连接删除后 resolve_model 兜底不受影响", resolve_model(db, "diagnosis")["setting_id"] == IDA)

# ===== F 校验与权限 =====
print("F 校验与权限")
r = client.put("/api/ai/scenarios/nope", json={"enabled": True})
check("未知场景 404", r.status_code == 404, str(r.status_code))
r = client.put("/api/ai/scenarios/diagnosis", json={"setting_id": 99999})
check("绑定不存在的连接 400", r.status_code == 400, str(r.status_code))
check("失败请求不落审计", audit_count() == n_audit, f"{audit_count()} != {n_audit}")

STUB_USER.role = "operator"
r = client.put("/api/ai/scenarios/ask", json={"enabled": True})
check("operator 写 403", r.status_code == 403, str(r.status_code))
r = client.get("/api/ai/scenarios")
check("operator 读 200", r.status_code == 200 and len(r.json()) == 4, str(r.status_code))
STUB_USER.role = "admin"

# ===== G 审计落账 =====
print("G 审计落账")
rows_a = db.query(AuditLog).filter(AuditLog.action == "ai_scenario.update").all()
check("每次成功 PUT 落一条审计", len(rows_a) == n_audit, f"{len(rows_a)} != {n_audit}")
check("审计带场景键与中文标签登记",
      all(a.resource_type == "ai_scenario" and a.resource_id in SCENARIOS for a in rows_a)
      and any(a.resource_id == "ask" for a in rows_a), str([(a.resource_id, a.username) for a in rows_a]))
from app.services.audit import ACTION_LABELS  # noqa: E402
check("ai_scenario.update 已登记中文动作", ACTION_LABELS.get("ai_scenario.update") == "修改 AI 场景模型绑定")

db.close()
print(f"\n场景矩阵 mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
