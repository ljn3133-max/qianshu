#!/usr/bin/env python3
"""LLM 界面语言隔离 mock 测试：回答语言跟随界面 + 动态错误文案双语
+ require_model 引导双语。

运行：cd /opt/qianshu/backend && python3 tests/test_i18n_llm_lang_mock.py
隔离性：MASTER_KEY/DATABASE_URL/CAPTCHA_ENABLED 等在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、零网络（LLM 调用全部 mock httpx.post/stream）、不发任何通知、
不重启 8810。全部走 TestClient + Accept-Language 头验证。

验证项：
  A with_lang_directive 单元 + 行为开关：en 注入 `Respond in English.`（system 尾部、
    返回副本不改入参）、zh 不注入；LLM_ANSWER_LANG_FOLLOW≠UI 时不注入（兼容旧行为）；
    无 system 消息时 prompt 尾补 system 条；copilot chat_once/chat_stream 同口径。
  B LLM 提示词按头注入（mock httpx 真跑调用链）：ask / diagnosis 全链路 en 提示词含
    English 指令、zh 不含；copilot SSE→run_turn→chat_stream 的 lang 穿透 = Accept-Language。
  C MSG 55 条：zh 原文逐字锁定 + en 无汉字；端到端按头出文抽查（integrations registry /
    clouds sync / ai 配置场景工具 / workflows 参数校验 / change_gate rollback-dismiss-skip）。
  D require_model 双语：单元 + ask/diagnosis API 502 + report API 502 按头出文。
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import date
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-llmlang-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("llmlang")
os.environ["MASTER_KEY"] = "22" * 32          # 64 hex 测试主密钥
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"           # 零数据源=registry 只有 6 条事件源行
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["PUBLIC_BASE_URL"] = ""
os.environ["CAPTCHA_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx as _real_httpx  # noqa: E402

from app.core.i18n_msg import MSG, t  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import encrypt_secret, hash_password  # noqa: E402
from app.models.agent_change import AgentChangeTicket  # noqa: E402
from app.models.inspection import InspectionRun  # noqa: E402
from app.models.user import User  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


CJK = re.compile(r"[一-鿿]")

# ---- 建表 + 种子（全部隔离自建的临时库）----
Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(username="u1", password_hash=hash_password("Passw0rd!x"), display_name="u1",
            role="viewer", is_active=True))
db.commit()

STUB_USER = SimpleNamespace(id=1, username="tester", role="admin", is_active=True,
                            password_hash=hash_password("OldPassw0rd1"), must_change_password=False)

from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402

app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_user] = lambda: STUB_USER
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

# ---- mock LLM 出口：捕获 messages 载荷，按脚本回内容 ----
CAPTURED: list = []          # [{module, messages, payload}]
SCRIPT: list = []            # 每次调用回的 content（按序弹出）


class _Resp:
    def __init__(self, content: str):
        self.status_code = 200
        self._content = content
        self.text = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _capture_post(module_name: str):
    def post(url, json=None, headers=None, timeout=None, verify=None):
        content = SCRIPT.pop(0) if SCRIPT else "{}"
        CAPTURED.append({"module": module_name, "messages": json.get("messages"),
                         "payload": json})
        return _Resp(content)
    return post


class _StreamResp:
    def __init__(self, content: str):
        self._content = content
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        yield "data: " + json.dumps({"choices": [{"delta": {"content": self._content}}]})
        yield "data: [DONE]"

    def read(self):
        return b""


def _capture_stream(module_name: str):
    def stream(method, url, json=None, headers=None, timeout=None, verify=None):
        CAPTURED.append({"module": module_name, "messages": json.get("messages"),
                         "payload": json, "stream": True})
        return _StreamResp("hi")
    return stream


import app.services.llm as llm_mod  # noqa: E402
import app.services.copilot.llm as copilot_llm_mod  # noqa: E402

_llm_httpx_orig = llm_mod.httpx
_copilot_httpx_orig = copilot_llm_mod.httpx


def _patch_llm_httpx():
    llm_mod.httpx = SimpleNamespace(post=_capture_post("services.llm"),
                                    HTTPError=_real_httpx.HTTPError)
    copilot_llm_mod.httpx = SimpleNamespace(post=_capture_post("copilot.llm"),
                                            stream=_capture_stream("copilot.llm"),
                                            HTTPError=_real_httpx.HTTPError)


def _restore_llm_httpx():
    llm_mod.httpx = _llm_httpx_orig
    copilot_llm_mod.httpx = _copilot_httpx_orig


# ===== A with_lang_directive 单元 + 开关 =====
print("A with_lang_directive / 行为开关")
from app.services.llm import with_lang_directive  # noqa: E402
from app.core.config import settings  # noqa: E402

src = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}]
out = with_lang_directive(src, "en")
check("A en 注入指令（system 尾部一行）",
      out[0]["content"] == "SYS\n\nRespond in English." and out[1]["content"] == "U",
      repr(out[0]["content"]))
check("A 返回副本不改入参（重试/多轮防重复注入）", src[0]["content"] == "SYS")
check("A zh 不注入（旧行为逐字不变）", with_lang_directive(src, "zh") is src)
check("A 缺省 lang 不注入", with_lang_directive(src) is src)
out = with_lang_directive([{"role": "user", "content": "U"}], "en")
check("A 无 system 时 prompt 尾补 system 条",
      out[-1] == {"role": "system", "content": "Respond in English."}, repr(out))

settings.LLM_ANSWER_LANG_FOLLOW = "fixed"
check("A 开关=固定中文：en 也不注入（兼容旧行为）",
      with_lang_directive(src, "en") is src)
settings.LLM_ANSWER_LANG_FOLLOW = "UI"
check("A 开关切回 UI 恢复注入",
      "Respond in English." in with_lang_directive(src, "en")[0]["content"])

# copilot chat_once / chat_stream 同口径（fake httpx 验证 payload）
_patch_llm_httpx()
_cfg = {"model": "m", "base_url": "http://llm.test", "api_key": "k", "setting_id": 1,
        "label": "L", "temperature": 0.2, "max_tokens": 64, "verify_ssl": True}
CAPTURED.clear()
SCRIPT.clear()
copilot_llm_mod.chat_once(_cfg, [{"role": "system", "content": "S"}], lang="en", db=None)
sys_msg = CAPTURED[-1]["messages"][0]["content"]
check("A copilot chat_once en 注入", sys_msg == "S\n\nRespond in English.", sys_msg)
CAPTURED.clear()
gen = copilot_llm_mod.chat_stream(_cfg, [{"role": "system", "content": "S"}], lang="zh", db=None)
list(gen)
check("A copilot chat_stream zh 不注入（payload 逐字）",
      CAPTURED[-1]["messages"][0]["content"] == "S")
_restore_llm_httpx()


# ===== D-1 require_model 双语（无 AiSetting → None 路径） =====
print("D require_model 双语")
from app.services.ai_scenarios import require_model, SCENARIOS  # noqa: E402
from app.services.llm import LLMError  # noqa: E402

for lang, label in (("zh", "AI 分析"), ("en", "AI Analysis")):
    try:
        require_model(db, "ask", lang=lang)
        check(f"D require_model {lang} 抛 LLMError", False)
    except LLMError as e:
        expect = t("ai.scenario_no_model", lang, label=label)
        check(f"D require_model {lang} 文案按 lang", str(e) == expect, str(e))
check("D zh 原文逐字（require_model）",
      t("ai.scenario_no_model", "zh", label="AI 分析")
      == "AI 分析：场景已停用或未配置可用模型，请在 设置 → AI 模型 中启用场景并配置模型连接")

# API 面：ask / diagnosis 502（无模型 → require_model 引导）
r = _req("POST", "/api/ai/ask", headers=EN, json={"question": "test q"})
check("D ask API 502 en", r.status_code == 502
      and r.json()["detail"] == t("ai.scenario_no_model", "en", label="AI Analysis"), r.text[:200])
r = _req("POST", "/api/ai/ask", headers=ZH, json={"question": "测试问题"})
check("D ask API 502 zh 原文", r.status_code == 502
      and r.json()["detail"] == t("ai.scenario_no_model", "zh", label="AI 分析"), r.text[:200])
r = _req("POST", "/api/diagnosis/run", headers=EN, json={"question": "site down"})
check("D diagnosis API 502 en", r.status_code == 502
      and r.json()["detail"] == t("ai.scenario_no_model", "en", label="Fault Diagnosis"), r.text[:200])
r = _req("POST", "/api/diagnosis/run", headers=ZH, json={"question": "网站打不开"})
check("D diagnosis API 502 zh 原文", r.status_code == 502
      and r.json()["detail"] == t("ai.scenario_no_model", "zh", label="故障诊断"), r.text[:200])


# ===== B LLM 提示词按头注入（mock httpx 真跑调用链） =====
print("B LLM 提示词按头注入（en 含 English 指令 / zh 不含）")
from app.models.ai_setting import AiSetting  # noqa: E402

db.add(AiSetting(provider_label="mock", base_url="http://llm.test",
                 api_key_cipher=encrypt_secret("sk-mock"), model="mock-model",
                 temperature=0.2, max_tokens=256, enabled=True))
db.commit()

_patch_llm_httpx()

# ask：路由 + 回答两次 chat 都按头注入
for lang, hdr in (("en", EN), ("zh", ZH)):
    CAPTURED.clear()
    SCRIPT.clear()
    SCRIPT.append('{"tools": [{"tool": "chat", "params": {}}], "reason": "t"}')
    SCRIPT.append("ANSWER")
    r = _req("POST", "/api/ai/ask", headers=hdr, json={"question": "多少台机器"})
    syses = [m for m in CAPTURED[-1]["messages"] if m["role"] == "system"]
    prompt = syses[0]["content"] if syses else ""
    if lang == "en":
        check("B ask en 提示词含 English 指令", r.status_code == 200
              and "Respond in English." in prompt, prompt[-80:])
    else:
        check("B ask zh 提示词不含指令（逐字旧行为）", r.status_code == 200
              and "Respond in" not in prompt, prompt[-80:])

# diagnosis：分诊 + 决策两次 chat 都按头注入
for lang, hdr in (("en", EN), ("zh", ZH)):
    CAPTURED.clear()
    SCRIPT.clear()
    SCRIPT.append('{"scenario": "link_fault", "reason": "r"}')
    SCRIPT.append('{"final": true, "answer": "C", "level": "info"}')
    r = _req("POST", "/api/diagnosis/run", headers=hdr, json={"question": "网站打不开"})
    syses = [m for m in CAPTURED[-1]["messages"] if m["role"] == "system"]
    prompt = syses[0]["content"] if syses else ""
    if lang == "en":
        check("B diagnosis en 提示词含 English 指令", r.status_code == 200
              and "Respond in English." in prompt, prompt[-80:])
    else:
        check("B diagnosis zh 提示词不含指令（逐字旧行为）", r.status_code == 200
              and "Respond in" not in prompt, prompt[-80:])

_restore_llm_httpx()

# copilot SSE：run_turn → chat_stream 的 lang 穿透 = Accept-Language
SEEN = {}


def _fake_chat_stream(cfg, messages, *, purpose="copilot", db=None, out=None,
                      temperature=None, max_tokens=None, lang="zh"):
    SEEN["lang"] = lang
    SEEN["messages"] = messages
    if out is not None:
        out.update({"content": "hi", "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                               "total_tokens": 2},
                    "duration_ms": 1, "model": "m", "aborted": False, "error": ""})

    def gen():
        yield "hi"
    return gen()


def _fake_chat_once(cfg, messages, *, purpose="copilot", db=None, temperature=None,
                    max_tokens=None, lang="zh"):
    SEEN["lang"] = lang
    return {"content": "hi", "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                       "total_tokens": 2}, "duration_ms": 1, "model": "m"}

_stream_orig, _once_orig = copilot_llm_mod.chat_stream, copilot_llm_mod.chat_once
copilot_llm_mod.chat_stream = _fake_chat_stream
copilot_llm_mod.chat_once = _fake_chat_once
try:
    for lang, hdr in (("en", EN), ("zh", ZH)):
        SEEN.clear()
        sid = _req("POST", "/api/agent/sessions", headers=hdr, json={}).json()["id"]
        r = _req("POST", f"/api/agent/sessions/{sid}/messages", headers=hdr,
                 json={"q": "hello"})
        check(f"B copilot SSE lang 穿透（{lang}）",
              r.status_code == 200 and SEEN.get("lang") == lang,
              f"seen={SEEN.get('lang')} status={r.status_code}")
finally:
    copilot_llm_mod.chat_stream = _stream_orig
    copilot_llm_mod.chat_once = _once_orig


# ===== C MSG 55 条：zh 逐字锁定 + en 无汉字 =====
print("C MSG 新增条目双语（zh 逐字 / en 无汉字）")
NEW_ZH = {
    "agent.ticket_not_found": "变更工单不存在",
    "agent.items_must_idx": "items 必须是变更项 idx 数组",
    "agent.item_not_found": "变更项不存在: {idx}",
    "agent.rollback_no_done": "工单没有已执行项，无需回滚",
    "agent.dismiss_has_done": "工单已有执行项，须先一键回滚再取消",
    "agent.dismiss_bad_status": "工单状态为 {status}，不允许取消",
    "agent.adjust_bad_status": "工单状态为 {status}，不允许调整变更项",
    "agent.skip_bad_idx": "idx 必须是变更项序号",
    "agent.already_skipped": "该项已是跳过态",
    "agent.skip_only_pending": "仅待确认（pending）项可跳过，当前 {status}",
    "agent.restore_only_skipped": "仅跳过态（skipped）可恢复",
    "int.event.token_missing":
        "token 未配置——外部事件会被拒收，链路不通（后端 .env 配置 EVENT_WEBHOOK_TOKEN 后重启生效）",
    "int.event.connected": "已连通 · 近 {days} 天真实收到 {count} 条事件（最近 {last}）",
    "int.event.stale":
        "曾收到事件（历史 {total} 条，最近 {last}），但近 {days} 天无新事件——链路配置正确，"
        "无法自证当前仍通；可从 {name} 发一条测试事件后复核",
    "int.event.none":
        "链路配置正确（token 已配置、接入点就绪）但尚无外部事件到达，无法自证端到端连通——"
        "请从 {name} 发一条测试事件，到达后此处显示「已连通」",
    "int.channel.disabled": "渠道已停用——告警不再推送到此渠道；在「编辑」里启用后恢复",
    "int.channel.error": "配置异常：{problems}——请编辑修复",
    "int.channel.connected": "已连通 · 近 {days} 天有真实送达记录（最近 {last}，口径=告警触发时间）",
    "int.channel.stale":
        "配置完整（凭证可解密、必填项齐全）；最近一次送达记录 {last}，近 {days} 天无发送——"
        "无法自证当前仍可用（对外发测试消息需用户在告警中心主动触发）",
    "int.channel.none":
        "配置完整（凭证可解密、必填项齐全）但尚无发送记录，无法自证真实送达"
        "——对外发测试消息需用户在告警中心主动触发",
    "int.problem.bad_webhook": "webhook 地址不合法",
    "int.problem.decrypt_fail": "webhook 凭证解密失败（MASTER_KEY 变更？）",
    "int.status.connected": "已连通",
    "int.status.unverified": "未验证",
    "int.status.error": "异常",
    "int.status.disabled": "已停用",
    "int.detail.ds_not_probed": "尚未探测——点「测试」做真实版本探测",
    "int.detail.k8s_last_sync": "最近同步 {ts}",
    "int.detail.k8s_not_probed": "尚未探测——点「测试」做真实连通测试",
    "int.detail.cloud_last_test": "最近测试 {ts}",
    "int.detail.cloud_not_tested": "尚未测试——点「测试可用性」做真实探测",
    "int.last.event": "最近事件",
    "int.last.send": "最近发送",
    "int.last.sync": "最近同步",
    "int.last.test": "最近测试",
    "wf.param.int": "{field} 必须是整数，收到 {raw!r}",
    "wf.param.num": "{field} 必须是数字，收到 {raw!r}",
    "wf.param.bool": "{field} 必须是布尔值，收到 {raw!r}",
    "wf.param.min": "{field} 不能小于 {min}",
    "wf.param.max": "{field} 不能大于 {max}",
    "wf.param.empty": "{field} 不能为空",
    "wf.param.not_object": "params 必须是对象",
    "wf.param.required": "{field} 必填（{desc}）",
    "wf.param.enum": "{field} 取值非法 {value!r}，可选: {choices}",
    "wf.param.invalid": "参数校验不过：{detail}",
    "wf.steps_empty": "steps 为空（传 key 或 draft steps）",
    "cloud.sync_unsupported": "暂不支持 {product} 同步（可选: {options}）",
    "cloud.sync_no_accounts": "没有启用中的云账号——先在云账号管理里添加",
    "ai.setting_not_found": "配置不存在",
    "ai.scenario_not_found": "场景不存在",
    "ai.model_not_found": "模型连接不存在",
    "ai.api_key_required": "请填写 API Key（新建必须填；编辑时留空则用已保存的 Key 测试）",
    "ai.tool_exec_failed": "工具执行失败: {detail}",
    "ai.tool_not_found": "工具不存在：{tool}",
    "ai.scenario_no_model":
        "{label}：场景已停用或未配置可用模型，请在 设置 → AI 模型 中启用场景并配置模型连接",
}
check("C 新条目共 55 条全部登记", len(NEW_ZH) == 55 and all(k in MSG for k in NEW_ZH),
      str([k for k in NEW_ZH if k not in MSG]))
zh_bad = {k: MSG[k]["zh"] for k in NEW_ZH if MSG[k]["zh"] != NEW_ZH[k]}
check("C zh 原文逐字锁定（55 条零漂移）", not zh_bad, str(zh_bad)[:200])
en_bad = [k for k in NEW_ZH if CJK.search(MSG[k]["en"])]
check("C en 译文无汉字（55 条）", not en_bad, str(en_bad))
check("C STATUS_TEXT 双语一致（zh=模块常量原文）",
      all(t(f"int.status.{st}", "zh") == v for st, v in
          (("connected", "已连通"), ("unverified", "未验证"), ("error", "异常"), ("disabled", "已停用")))
      and t("int.status.connected", "en") == "Connected")

# ---- C 端到端：integrations registry 按头出文 ----
d = _req("GET", "/api/integrations/registry", headers=EN).json()
rows = d["rows"]
check("C registry en：6 事件源行 status_text/detail 无汉字",
      len(rows) == 6 and all(r["category"] == "event" for r in rows)
      and not [s for r in rows for s in (r["status_text"], r["detail"], r["category_label"],
                                        r["last_data_label"]) if CJK.search(s)],
      str([(r["status_text"], r["detail"][:30]) for r in rows[:2]]))
check("C registry en status_labels", d["status_labels"]["connected"] == "Connected"
      and d["status_labels"]["unverified"] == "Unverified", str(d["status_labels"]))
d = _req("GET", "/api/integrations/registry", headers=ZH).json()
row = next(r for r in d["rows"] if r["type"] == "gitlab")
check("C registry zh 原文逐字（event.none 文案）",
      row["detail"] == "链路配置正确（token 已配置、接入点就绪）但尚无外部事件到达，"
                       "无法自证端到端连通——请从 GitLab 发一条测试事件，到达后此处显示「已连通」",
      row["detail"])
check("C registry zh status_text 原文", row["status_text"] == "未验证"
      and d["status_labels"]["connected"] == "已连通")

# ---- C 端到端：clouds sync 两条 ----
r = _req("POST", "/api/clouds/sync?product=bogus", headers=EN)
check("C sync 暂不支持 en", r.status_code == 400
      and r.json()["detail"].startswith("Syncing bogus is not supported"), r.text[:160])
r = _req("POST", "/api/clouds/sync?product=bogus", headers=ZH)
check("C sync 暂不支持 zh 原文", r.status_code == 400
      and r.json()["detail"].startswith("暂不支持 bogus 同步"), r.text[:160])
r = _req("POST", "/api/clouds/sync", headers=EN)
check("C sync 无账号 en", r.status_code == 400
      and r.json()["detail"] == "No enabled cloud accounts — add one in Cloud Accounts first.",
      r.text[:160])
r = _req("POST", "/api/clouds/sync", headers=ZH)
check("C sync 无账号 zh 原文", r.status_code == 400
      and r.json()["detail"] == "没有启用中的云账号——先在云账号管理里添加", r.text[:160])

# ---- C 端到端：ai.py 场景/配置/连接/工具 ----
r = _req("PUT", "/api/ai/scenarios/ghost", headers=EN, json={"enabled": True})
check("C 场景不存在 en", r.status_code == 404
      and r.json()["detail"] == "Scenario does not exist.", r.text[:120])
r = _req("PUT", "/api/ai/scenarios/ghost", headers=ZH, json={"enabled": True})
check("C 场景不存在 zh 原文", r.status_code == 404 and r.json()["detail"] == "场景不存在", r.text[:120])
r = _req("PUT", "/api/ai/scenarios/ask", headers=EN, json={"setting_id": 9999})
check("C 模型连接不存在 en", r.status_code == 400
      and r.json()["detail"] == "Model connection does not exist.", r.text[:120])
r = _req("PUT", "/api/ai/scenarios/ask", headers=ZH, json={"setting_id": 9999})
check("C 模型连接不存在 zh 原文", r.status_code == 400
      and r.json()["detail"] == "模型连接不存在", r.text[:120])
r = _req("PUT", "/api/ai/settings/9999", headers=EN, json={"model": "x"})
check("C 配置不存在 en", r.status_code == 404
      and r.json()["detail"] == "Configuration does not exist.", r.text[:120])
r = _req("PUT", "/api/ai/settings/9999", headers=ZH, json={"model": "x"})
check("C 配置不存在 zh 原文", r.status_code == 404 and r.json()["detail"] == "配置不存在", r.text[:120])
r = _req("POST", "/api/ai/test", headers=EN, json={"base_url": "http://x", "model": "m"})
check("C API Key 必填 en", r.status_code == 400
      and r.json()["detail"].startswith("Enter an API Key"), r.text[:160])
r = _req("POST", "/api/ai/test", headers=ZH, json={"base_url": "http://x", "model": "m"})
check("C API Key 必填 zh 原文", r.status_code == 400
      and r.json()["detail"] == "请填写 API Key（新建必须填；编辑时留空则用已保存的 Key 测试）",
      r.text[:160])
# 工具不存在 404（ai.py 的 KeyError 兜底分支；正常未知工具走 tools_read ToolError 透传——
# 工具族 ToolError 文案属工具服务层，另批处理）。monkeypatch 触发 KeyError 分支验证 MSG 出文。
import app.services.copilot.tools_read as tools_read_mod  # noqa: E402

_tr_exec_orig = tools_read_mod.execute


def _tr_keyerror(db, name, params, user=None):
    raise KeyError(name)

tools_read_mod.execute = _tr_keyerror
try:
    r = _req("POST", "/api/ai/tools/execute", headers=EN, json={"tool": "ghost_tool", "params": {}})
    check("C 工具不存在 en", r.status_code == 404
          and r.json()["detail"] == "Tool not found: ghost_tool", r.text[:120])
    r = _req("POST", "/api/ai/tools/execute", headers=ZH, json={"tool": "ghost_tool", "params": {}})
    check("C 工具不存在 zh 原文", r.status_code == 404
          and r.json()["detail"] == "工具不存在：ghost_tool", r.text[:120])
finally:
    tools_read_mod.execute = _tr_exec_orig

# ---- C 端到端：workflows 参数校验（topology_refine 必填 feedback，未到执行步即 400） ----
r = _req("POST", "/api/agent/workflows/topology_refine/run", headers=EN, json={"params": {}})
check("C wf 参数校验 en（wrapper+条目）", r.status_code == 400
      and r.json()["detail"].startswith("Parameter validation failed:")
      and "feedback is required" in r.json()["detail"], r.text[:200])
r = _req("POST", "/api/agent/workflows/topology_refine/run", headers=ZH, json={"params": {}})
check("C wf 参数校验 zh 原文", r.status_code == 400
      and r.json()["detail"] == "参数校验不过：feedback 必填（用户反馈原文）", r.text[:200])
r = _req("POST", "/api/agent/workflows/custom/preview", headers=EN, json={"steps": []})
check("C wf steps 为空 en", r.status_code == 400
      and r.json()["detail"] == "steps is empty (pass key or draft steps).", r.text[:120])
r = _req("POST", "/api/agent/workflows/custom/preview", headers=ZH, json={"steps": []})
check("C wf steps 为空 zh 原文", r.status_code == 400
      and r.json()["detail"] == "steps 为空（传 key 或 draft steps）", r.text[:120])

# ---- C 端到端：change_gate rollback / dismiss / skip（临时库工单，零执行） ----
it_pending = json.dumps([{"idx": 0, "title": "x", "tool": "noop", "params": {},
                          "status": "pending", "ack_required": False}])
it_done = json.dumps([{"idx": 0, "title": "x", "tool": "noop", "params": {},
                       "status": "done", "ack_required": False}])
it_skipped = json.dumps([{"idx": 0, "title": "x", "tool": "noop", "params": {},
                          "status": "skipped", "ack_required": False}])
t_pend = AgentChangeTicket(title="pend", change_list_json=it_pending, status="pending")
t_doneitem = AgentChangeTicket(title="doneitem", change_list_json=it_done, status="pending")
t_skipped = AgentChangeTicket(title="skipped", change_list_json=it_skipped, status="pending")
t_cancelable = AgentChangeTicket(title="done-status", change_list_json=it_pending, status="done")
t_gone = AgentChangeTicket(title="dismissed", change_list_json=it_pending, status="dismissed")
db.add_all([t_pend, t_doneitem, t_skipped, t_cancelable, t_gone])
db.commit()


def _gate(url, hdr, body=None):
    return _req("POST", url, headers=hdr, json=body or {})


r = _gate(f"/api/agent/changes/{t_pend.id}/rollback", EN)
check("C rollback 无已执行项 en", r.status_code == 400
      and r.json()["detail"] == "The ticket has no executed items — nothing to roll back.",
      r.text[:160])
r = _gate(f"/api/agent/changes/{t_pend.id}/rollback", ZH)
check("C rollback 无已执行项 zh 原文", r.status_code == 400
      and r.json()["detail"] == "工单没有已执行项，无需回滚", r.text[:160])
r = _gate(f"/api/agent/changes/{t_doneitem.id}/dismiss", EN)
check("C dismiss 有已执行项 en", r.status_code == 403
      and r.json()["detail"].startswith("The ticket already has executed items"), r.text[:160])
r = _gate(f"/api/agent/changes/{t_doneitem.id}/dismiss", ZH)
check("C dismiss 有已执行项 zh 原文", r.status_code == 403
      and r.json()["detail"] == "工单已有执行项，须先一键回滚再取消", r.text[:160])
r = _gate(f"/api/agent/changes/{t_cancelable.id}/dismiss", EN)
check("C dismiss 状态不允许 en", r.status_code == 403
      and r.json()["detail"] == "Ticket status is done, so it cannot be dismissed.", r.text[:160])
r = _gate(f"/api/agent/changes/{t_cancelable.id}/dismiss", ZH)
check("C dismiss 状态不允许 zh 原文", r.status_code == 403
      and r.json()["detail"] == "工单状态为 done，不允许取消", r.text[:160])
r = _gate(f"/api/agent/changes/{t_pend.id}/skip", EN, {"idx": 99})
check("C skip 项不存在 en", r.status_code == 400
      and r.json()["detail"] == "Change item not found: 99", r.text[:160])
r = _gate(f"/api/agent/changes/{t_pend.id}/skip", ZH, {"idx": 99})
check("C skip 项不存在 zh 原文", r.status_code == 400
      and r.json()["detail"] == "变更项不存在: 99", r.text[:160])
r = _gate(f"/api/agent/changes/{t_skipped.id}/skip", EN, {"idx": 0})
check("C skip 幂等 note en", r.status_code == 200
      and r.json()["note"] == "This item is already skipped.", r.text[:160])
r = _gate(f"/api/agent/changes/{t_skipped.id}/skip", ZH, {"idx": 0})
check("C skip 幂等 note zh 原文", r.status_code == 200
      and r.json()["note"] == "该项已是跳过态", r.text[:160])
r = _gate(f"/api/agent/changes/{t_pend.id}/skip", EN, {"idx": 0, "restore": True})
check("C restore 非跳过态 en", r.status_code == 400
      and r.json()["detail"] == "Only skipped items can be restored.", r.text[:160])
r = _gate(f"/api/agent/changes/{t_pend.id}/skip", ZH, {"idx": 0, "restore": True})
check("C restore 非跳过态 zh 原文", r.status_code == 400
      and r.json()["detail"] == "仅跳过态（skipped）可恢复", r.text[:160])
r = _gate(f"/api/agent/changes/{t_doneitem.id}/skip", EN, {"idx": 0})
check("C skip 非 pending 项 en", r.status_code == 400
      and r.json()["detail"] == "Only pending items can be skipped (current status: done).",
      r.text[:160])
r = _gate(f"/api/agent/changes/{t_doneitem.id}/skip", ZH, {"idx": 0})
check("C skip 非 pending 项 zh 原文", r.status_code == 400
      and r.json()["detail"] == "仅待确认（pending）项可跳过，当前 done", r.text[:160])
r = _gate(f"/api/agent/changes/{t_gone.id}/skip", EN, {"idx": 0})
check("C skip 工单状态不允许调整 en", r.status_code == 403
      and r.json()["detail"] == "Ticket status is dismissed, so its change items cannot be adjusted.",
      r.text[:160])
r = _gate(f"/api/agent/changes/{t_gone.id}/skip", ZH, {"idx": 0})
check("C skip 工单状态不允许调整 zh 原文", r.status_code == 403
      and r.json()["detail"] == "工单状态为 dismissed，不允许调整变更项", r.text[:160])
r = _gate("/api/agent/changes/999999/rollback", EN)
check("C 工单不存在 en", r.status_code == 404
      and r.json()["detail"] == "Change ticket does not exist.", r.text[:120])
r = _gate("/api/agent/changes/999999/dismiss", ZH)
check("C 工单不存在 zh 原文", r.status_code == 404 and r.json()["detail"] == "变更工单不存在",
      r.text[:120])
# 防御式文案（API 契约外兜底）：直接服务层验证
from app.services.copilot import change_gate  # noqa: E402

try:
    change_gate.execute_ticket(db, t_pend.id, STUB_USER, items=["x"], lang="en")
    check("C items 必须是 idx 数组", False)
except Exception as e:
    check("C items 必须是 idx 数组 en",
          getattr(e, "detail", "") == "items must be an array of change-item idx values.",
          str(getattr(e, "detail", e)))
try:
    change_gate.execute_ticket(db, t_pend.id, STUB_USER, items=["x"])
    check("C items 必须是 idx 数组", False)
except Exception as e:
    check("C items 必须是 idx 数组 zh 原文",
          getattr(e, "detail", "") == "items 必须是变更项 idx 数组",
          str(getattr(e, "detail", e)))
try:
    change_gate.skip_item(db, t_pend.id, STUB_USER, idx="x")
    check("C idx 必须是序号", False)
except Exception as e:
    check("C idx 必须是序号 zh 原文",
          getattr(e, "detail", "") == "idx 必须是变更项序号", str(getattr(e, "detail", e)))


# ===== D-2 report API 面（setting 存在 + inspection 场景停用 → require_model 引导） =====
from app.models.ai_scenario import AiScenarioModel  # noqa: E402

db.add(InspectionRun(run_date=date.today(), status="ok"))
db.add(AiScenarioModel(scenario="inspection", enabled=False))
db.commit()
r = _req("POST", "/api/inspections/report/generate", headers=EN, json={"notify": False})
check("D report API 502 en", r.status_code == 502
      and r.json()["detail"] == t("ai.scenario_no_model", "en", label="Inspection Center"),
      r.text[:200])
r = _req("POST", "/api/inspections/report/generate", headers=ZH, json={"notify": False})
check("D report API 502 zh 原文", r.status_code == 502
      and r.json()["detail"] == t("ai.scenario_no_model", "zh", label="巡检中心"), r.text[:200])

db.close()

print(f"\ni18n LLM 语言跟随 + 双语尾巴 mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
