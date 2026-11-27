#!/usr/bin/env python3
"""后端下发文案隔离 mock 测试：i18n_msg 助手 + 双语出文契约。

运行：cd /opt/qianshu/backend && python3 tests/test_i18n_msg_mock.py
隔离性：MASTER_KEY/DATABASE_URL/CAPTCHA_ENABLED 等在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不调任何网络（METRIC_SOURCES/LOG_SOURCES 清空零探测）、
绝不向任何真实/本地通知渠道发消息。全部走 TestClient + Accept-Language 头验证（不重启 8810）。

验证项：
  A lang_of 解析：含 en→en（en / en-US / 混合头）、zh-CN/无头/None→zh（缺省中文）；
    pick：双语取值+缺项回退 zh+普通串透传；t() 格式化与登记表完整性（87 条 zh/en 双全）。
  B 教程按头出文：GUIDES 15 键 + DS_TYPE_GUIDES 15 键——en 请求全英文（递归无汉字）、
    zh 请求全中文（旧文案逐字保留抽查）；占位符 {webhook_url} 已填充；响应字段全为 str（无 dict 泄漏）。
  C workflows/scenarios 双语：GET /api/agent/workflows 按头出 name/domain/description；
    GET /api/ai/scenarios 按头出 label；zh 缺省口径不变。
  D 高频错误双语：登录（用户不存在/密码错误）、改密（旧密错/弱口令）、
    clouds ack 警示 400 + AK/SK 400、确认门 403 三道闸、NEED_MODEL SSE——zh 原文逐字、en 地道译文。
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-i18n-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("i18n")
os.environ["MASTER_KEY"] = "11" * 32          # 64 hex 测试主密钥
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"           # 零数据源=探测零网络
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["PUBLIC_BASE_URL"] = ""
os.environ["CAPTCHA_ENABLED"] = "false"       # 登录直达口令校验点（人机验证另测）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.i18n_msg import MSG, lang_of, pick, t  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.agent_change import AgentChangeTicket  # noqa: E402
from app.models.user import User  # noqa: E402

# app.main 全量路由导入 = 全量模型注册（ChangeEvent/AgentSession/AiSetting/审计等表齐全）
from app.main import app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


CJK = re.compile(r"[一-鿿]")


def walk_strings(v):
    """响应 JSON 里的全部字符串（dict/list 递归）。"""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from walk_strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from walk_strings(x)


# ---- 建表 + 种子（全部隔离自建的临时库）----
Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(username="u1", password_hash=hash_password("Passw0rd!x"), display_name="u1",
            role="viewer", is_active=True))
db.commit()

STUB_USER = SimpleNamespace(id=1, username="tester", role="admin", is_active=True,
                            password_hash=hash_password("OldPassw0rd1"), must_change_password=False)

# ---- TestClient：get_db/get_current_user 走依赖覆盖（不碰真实库真实账号）----
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402

app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_user] = lambda: STUB_USER
# starlette TestClient 与 httpx 0.28 不兼容；ASGITransport 走 asyncio.run 同步壳（零网络）
import httpx as _httpx  # noqa: E402


def _req(method: str, url: str, **kw):
    async def _go():
        async with _httpx.AsyncClient(
                transport=_httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


client = SimpleNamespace(
    get=lambda url, **kw: _req("GET", url, **kw),
    post=lambda url, **kw: _req("POST", url, **kw),
    put=lambda url, **kw: _req("PUT", url, **kw),
)

ZH = {"Accept-Language": "zh-CN"}
EN = {"Accept-Language": "en"}


# ===== A lang_of / pick / t =====
print("A lang_of / pick / t")
check("A 含 en → en", lang_of(SimpleNamespace(headers={"accept-language": "en"})) == "en")
check("A en-US → en", lang_of(SimpleNamespace(headers={"accept-language": "en-US"})) == "en")
check("A 混合头含 en → en", lang_of(SimpleNamespace(headers={"accept-language": "fr-FR, en;q=0.8"})) == "en")
check("A zh-CN → zh", lang_of(SimpleNamespace(headers={"accept-language": "zh-CN"})) == "zh")
check("A 无头 → zh（缺省中文）", lang_of(SimpleNamespace(headers={})) == "zh")
check("A request=None → zh", lang_of(None) == "zh")
check("A pick 取 en", pick({"zh": "中", "en": "EN"}, "en") == "EN")
check("A pick 缺 en 回退 zh", pick({"zh": "中"}, "en") == "中")
check("A pick 普通串透传", pick("plain", "en") == "plain")
check("A t 格式化", t("cloud.test_failed", "en", reason="X") == "Test failed: X"
      and t("cloud.test_failed", "zh", reason="X") == "测试失败: X")
check("A MSG ≥87 条 zh/en 双全",
      len(MSG) >= 87 and all(isinstance(v, dict) and v.get("zh") and v.get("en") for v in MSG.values()),
      f"{len(MSG)} 条")
# zh 原文逐字锁定（双语化不得改动中文口径）
check("A zh 原文逐字（登录/验证码）",
      t("auth.bad_password", "zh") == "密码错误"
      and t("auth.slider_expired", "zh") == "验证已过期，请重新滑动"
      and t("auth.slider_misaligned", "zh") == "拼图未对齐，请重试"
      and t("auth.captcha_bad", "zh") == "验证码错误或已过期")
check("A zh 原文逐字（NEED_MODEL/确认门）",
      t("agent.need_model", "zh") == "未配置 AI 模型，请先在 设置 → AI 模型 中配置并启用"
      and t("agent.gate_no_items", "zh") == "没有可执行的变更项（所选项均已完成/已跳过/已回滚——重放不受理）"
      and t("agent.gate_status", "zh", status="done").startswith("工单状态为 done，不允许确认执行"))


# ===== B 教程按头出文 =====
print("B 教程按头出文（en 全英文 / zh 全中文）")
GUIDE_KEYS = ["gitlab", "gitea", "github", "jenkins", "skywalking", "prometheus", "loki",
              "otlp", "k8s", "zabbix", "aliyun", "tencent", "aws", "vsphere", "kvm"]

en_bad = []
for k in GUIDE_KEYS:
    d = _req("GET", f"/api/integrations/guide/{k}", headers=EN).json()
    for s in walk_strings(d):
        if CJK.search(s):
            en_bad.append((k, s[:40]))
check("B 15 教程 en 请求全英文（递归无汉字）", not en_bad, str(en_bad[:3]))

zh_no_cjk = []
for k in GUIDE_KEYS:
    d = _req("GET", f"/api/integrations/guide/{k}", headers=ZH).json()
    if not CJK.search(d.get("summary", "")) or not any(CJK.search(s) for s in d.get("steps") or []):
        zh_no_cjk.append(k)
check("B 15 教程 zh 请求全中文（summary/steps 均含汉字）", not zh_no_cjk, str(zh_no_cjk))

d = _req("GET", "/api/integrations/guide/gitlab", headers=EN).json()
check("B en 内容为地道译文（gitlab summary 标记）",
      d["summary"].startswith("Push / MR / pipeline events"), d["summary"][:60])
check("B 占位符已填充（en）", "{webhook_url}" not in json.dumps(d) and d["webhook_url"].endswith("/api/events/webhook/gitlab"))
check("B en 字段全为 str（无双语 dict 泄漏）",
      all(isinstance(x, str) for x in [d["name"], d["summary"], d["endpoint"], d["test_hint"]]
          + list(d["steps"]) + list(d["notes"])))
d = _req("GET", "/api/integrations/guide/gitlab", headers=ZH).json()
check("B zh 内容逐字保留（gitlab summary 标记）",
      d["summary"].startswith("代码推送 / MR / 流水线事件"), d["summary"][:60])
d = _req("GET", "/api/integrations/guide/k8s", headers=EN).json()
check("B en 代码块注释已译（k8s YAML）",
      "Built-in view" in d["code_blocks"][0]["code"] and "千枢" not in json.dumps(d))
d = _req("GET", "/api/integrations/guide/k8s", headers=ZH).json()
check("B zh 代码块原文保留（k8s YAML）", "千枢只读接入" in d["code_blocks"][0]["code"])
d = _req("GET", "/api/integrations/guide/aliyun", headers=EN).json()
check("B en 名称已译（aliyun）", d["name"] == "Alibaba Cloud (deep support)", d["name"])

r = _req("GET", "/api/integrations/overview", headers=EN)
d = r.json()
ov_bad = [s for g in d["guides"] for s in (g["name"], g["summary"]) if CJK.search(s)]
ov_bad += [s for s in d["category_labels"].values() if CJK.search(s)]
check("B overview en：教程摘要+类别名全英文", r.status_code == 200 and not ov_bad, str(ov_bad[:3]))
check("B overview en 类别名", d["category_labels"]["event"] == "Event sources"
      and d["category_labels"]["cloud"] == "Cloud vendors", str(d["category_labels"]))
d = _req("GET", "/api/integrations/overview", headers=ZH).json()
check("B overview zh 类别名原口径", d["category_labels"]["event"] == "事件源"
      and d["category_labels"]["cloud"] == "云厂商"
      and any(g["name"] == "GitLab" for g in d["guides"]))

r = _req("GET", "/api/integrations/ds-guides", headers=EN)
d = r.json()
ds_bad = [(k, s[:40]) for k, row in d.items() for s in walk_strings(row) if CJK.search(s)]
check("B ds-guides 14 类 en 全英文", r.status_code == 200 and len(d) == 15 and not ds_bad, str(ds_bad[:3]))
d = _req("GET", "/api/integrations/ds-guides", headers=ZH).json()
check("B ds-guides zh 原文（zabbix 标记）",
      "apiinfo.version" in d["zabbix"]["notes"][0] and "自动拼 /api_jsonrpc.php" in d["zabbix"]["url_placeholder"]
      and d["sls"]["label"] == "阿里云 SLS" and d["http"]["label"] == "自定义 HTTP API")
r = _req("GET", "/api/integrations/ds-guides", headers=EN)
check("B ds-guides en 标签已译", r.json()["sls"]["label"] == "Alibaba Cloud SLS"
      and r.json()["http"]["label"] == "Custom HTTP API")


# ===== C workflows / scenarios 双语 =====
print("C workflows / scenarios 双语")
r = _req("GET", "/api/agent/workflows", headers=EN)
items = {i["key"]: i for i in r.json()["items"]}
check("C workflows 13 个 en 出文", r.status_code == 200 and len(items) == 13, str(len(items)))
check("C workflows en name/domain/description",
      items["cost_checkup"]["name"] == "Cost checkup"
      and items["cost_checkup"]["domain"] == "Analysis"
      and items["access_cloud"]["name"] == "Onboard cloud account"
      and items["topology_refine"]["name"] == "Topology redraw"
      # 「我已知晓」是服务端 ack 输入契约字面量（en 文案亦须原样引用），剔除后不得再有汉字
      and not any(CJK.search((i["name"] + i["domain"] + i["description"]).replace("我已知晓", ""))
                  for i in items.values()),
      str([(k, i["name"]) for k, i in items.items()][:3]))
r = _req("GET", "/api/agent/workflows", headers=ZH)
items_zh = {i["key"]: i for i in r.json()["items"]}
check("C workflows zh 原口径（缺省中文）",
      items_zh["cost_checkup"]["name"] == "成本体检"
      and items_zh["cost_checkup"]["domain"] == "分析域"
      and items_zh["access_cloud"]["domain"] == "接入域"
      and items_zh["fault_diagnosis"]["description"].startswith("故障诊断："))

r = _req("GET", "/api/ai/scenarios", headers=EN)
labels_en = [x["label"] for x in r.json()]
check("C scenarios en label", r.status_code == 200
      and labels_en == ["AI Analysis", "Inspection Center", "Fault Diagnosis", "QianShu Copilot"],
      str(labels_en))
r = _req("GET", "/api/ai/scenarios", headers=ZH)
labels_zh = [x["label"] for x in r.json()]
check("C scenarios zh label（缺省中文原口径）",
      labels_zh == ["AI 分析", "巡检中心", "故障诊断", "千枢智能体"], str(labels_zh))


# ===== D 高频错误双语 =====
print("D 高频错误双语")
r = _req("POST", "/api/auth/login", headers=EN,
         json={"username": "ghost", "password": "x"})
check("D 登录用户不存在 en", r.status_code == 401 and r.json()["detail"] == "User does not exist.", r.text[:120])
r = _req("POST", "/api/auth/login", headers=ZH,
         json={"username": "ghost", "password": "x"})
check("D 登录用户不存在 zh 原文", r.status_code == 401 and r.json()["detail"] == "用户不存在", r.text[:120])
r = _req("POST", "/api/auth/login", headers=EN,
         json={"username": "u1", "password": "wrong-pass"})
check("D 密码错误 en", r.status_code == 401 and r.json()["detail"] == "Incorrect password.", r.text[:120])
r = _req("POST", "/api/auth/login", headers=ZH,
         json={"username": "u1", "password": "wrong-pass"})
check("D 密码错误 zh 原文", r.status_code == 401 and r.json()["detail"] == "密码错误", r.text[:120])

r = _req("POST", "/api/auth/password", headers=EN,
         json={"old_password": "wrong", "new_password": "NewPassw0rd1"})
check("D 改密旧密错 en", r.status_code == 400 and r.json()["detail"] == "Current password is incorrect.", r.text[:120])
r = _req("POST", "/api/auth/password", headers=ZH,
         json={"old_password": "wrong", "new_password": "NewPassw0rd1"})
check("D 改密旧密错 zh 原文", r.status_code == 400 and r.json()["detail"] == "当前密码不正确", r.text[:120])
r = _req("POST", "/api/auth/password", headers=EN,
         json={"old_password": "OldPassw0rd1", "new_password": "12345678"})
check("D 弱口令 en", r.status_code == 400
      and r.json()["detail"] == "This password is too common (matched the weak-password list). Please choose another one.",
      r.text[:120])
r = _req("POST", "/api/auth/password", headers=ZH,
         json={"old_password": "OldPassw0rd1", "new_password": "12345678"})
check("D 弱口令 zh 原文", r.status_code == 400 and r.json()["detail"] == "密码过于常见（弱口令库命中），请换一个", r.text[:120])

r = _req("POST", "/api/clouds/accounts", headers=EN,
         json={"name": "i18n-acct", "vendor": "aliyun", "ak": "A" * 12, "sk": "B" * 12, "ack": ""})
check("D ack 警示 400 en", r.status_code == 400 and "read the security notice" in r.json()["detail"]
      and "「我已知晓」" in r.json()["detail"], r.text[:160])
r = _req("POST", "/api/clouds/accounts", headers=ZH,
         json={"name": "i18n-acct", "vendor": "aliyun", "ak": "A" * 12, "sk": "B" * 12, "ack": ""})
check("D ack 警示 400 zh 原文", r.status_code == 400
      and r.json()["detail"].startswith("请阅读安全警示并输入「我已知晓」后保存"), r.text[:160])
r = _req("POST", "/api/clouds/accounts", headers=EN,
         json={"name": "i18n-acct2", "vendor": "aliyun", "ak": "A" * 12, "sk": ""})
check("D AK/SK 成对 400 en", r.status_code == 400
      and r.json()["detail"] == "AK/SK must be provided as a pair (at least 8 characters each).", r.text[:120])
r = _req("POST", "/api/clouds/accounts", headers=ZH,
         json={"name": "i18n-acct2", "vendor": "aliyun", "ak": "A" * 12, "sk": ""})
check("D AK/SK 成对 400 zh 原文", r.status_code == 400 and r.json()["detail"] == "AK/SK 需成对填写（至少 8 位）", r.text[:120])

# 确认门 403 三道闸（工单直接落临时库；confirm 执行路径在闸门即被拒，零副作用）
ack_item = json.dumps([{"idx": 0, "title": "x", "tool": "create_cloud_account",
                        "params": {}, "status": "pending", "ack_required": True}])
done_item = json.dumps([{"idx": 0, "title": "x", "tool": "noop",
                         "params": {}, "status": "done", "ack_required": False}])
t_ack = AgentChangeTicket(title="ack", change_list_json=ack_item, status="pending")
t_done_items = AgentChangeTicket(title="done-items", change_list_json=done_item, status="pending")
t_done = AgentChangeTicket(title="done", change_list_json=ack_item, status="done")
db.add_all([t_ack, t_done_items, t_done])
db.commit()

r = _req("POST", f"/api/agent/changes/{t_done.id}/confirm", headers=EN, json={"ack": "我已知晓"})
check("D 确认门·工单状态 403 en", r.status_code == 403 and "Ticket status is done" in r.json()["detail"]
      and "confirmation gate" in r.json()["detail"], r.text[:160])
r = _req("POST", f"/api/agent/changes/{t_done.id}/confirm", headers=ZH, json={"ack": "我已知晓"})
check("D 确认门·工单状态 403 zh 原文", r.status_code == 403
      and r.json()["detail"].startswith("工单状态为 done，不允许确认执行"), r.text[:160])
r = _req("POST", f"/api/agent/changes/{t_done_items.id}/confirm", headers=EN, json={})
check("D 确认门·无可执行项 403 en", r.status_code == 403
      and r.json()["detail"].startswith("No executable change items"), r.text[:160])
r = _req("POST", f"/api/agent/changes/{t_done_items.id}/confirm", headers=ZH, json={})
check("D 确认门·无可执行项 403 zh 原文", r.status_code == 403
      and r.json()["detail"] == "没有可执行的变更项（所选项均已完成/已跳过/已回滚——重放不受理）", r.text[:160])
r = _req("POST", f"/api/agent/changes/{t_ack.id}/confirm", headers=EN, json={"ack": ""})
check("D 确认门·凭证 ack 403 en", r.status_code == 403 and "security notice" in r.json()["detail"]
      and "「我已知晓」" in r.json()["detail"] and "v1-20261104" in r.json()["detail"], r.text[:200])
r = _req("POST", f"/api/agent/changes/{t_ack.id}/confirm", headers=ZH, json={"ack": ""})
check("D 确认门·凭证 ack 403 zh 原文", r.status_code == 403
      and "该变更含凭证类操作（云账号接入/改凭证）" in r.json()["detail"]
      and "警示版本 v1-20261104" in r.json()["detail"], r.text[:200])

# NEED_MODEL SSE（无 AiSetting → resolve_model None → error 事件按头出文）
r = _req("POST", "/api/agent/sessions", headers=EN, json={})
sid = r.json()["id"]
body = _req("POST", f"/api/agent/sessions/{sid}/messages", headers=EN,
            json={"q": "hello"}).text
check("D NEED_MODEL SSE en", "NEED_MODEL" in body and "No AI model is configured" in body, body[:200])
r = _req("POST", "/api/agent/sessions", headers=ZH, json={})
sid = r.json()["id"]
body = _req("POST", f"/api/agent/sessions/{sid}/messages", headers=ZH,
            json={"q": "hello"}).text
check("D NEED_MODEL SSE zh 原文", "NEED_MODEL" in body and "未配置 AI 模型" in body, body[:200])

db.close()

print(f"\ni18n 双语 mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
