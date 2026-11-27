#!/usr/bin/env python3
"""场景矩阵行为对齐隔离 mock 测试：ask/diagnosis/report 的 LLM 调用
按场景矩阵绑定走指定模型连接，不允许退回全局 get_active_setting。

运行：cd /opt/qianshu/backend && python3 tests/test_scenario_model_binding_mock.py
隔离性：MASTER_KEY/DATABASE_URL 在 import app.* 之前改写为临时值——不碰真实 qianshu.db、
零网络（LLM 全程 mock httpx.post，捕获 endpoint/Authorization 证明走了哪条连接）、
不发任何通知、copilot loop.py 不触碰（copilot 在 resolve_model + chat_once 出口层验证）。

验证项：
  A 四场景各绑不同连接 → 调用走对应连接（HTTP 边界：endpoint=绑定连接 base_url +
    Bearer=绑定连接 key；llm_call_logs 留痕同连接）：
    ask→conn-B / diagnosis→conn-C / inspection→conn-D / copilot→conn-A。
  B 未绑定兜底：解绑 ask → 调用回默认连接 conn-A（与旧 get_active_setting 同一行，
    resolve_model 既有兜底语义不变）。
  C 场景停用 → require_model 拒（LLMError=API 502 引导类）+ 三个消费面 run_* 同拒、零 LLM 调用。
  D 旧 get_active_setting 调用方零破坏：签名/返回 ORM/首选启用连接不变；
    chat(AiSetting) 旧直调路径照走；三消费面源码锁定走 require_model。
"""
import os
import sys
import tempfile
from datetime import date
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-scenbind-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("scenbind")
os.environ["MASTER_KEY"] = "ab" * 32          # 64 hex 测试主密钥
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"           # 零数据源=零探测
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["PUBLIC_BASE_URL"] = ""
os.environ["CAPTCHA_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx as _real_httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.ai_scenario import AiScenarioModel  # noqa: E402
from app.models.ai_setting import AiSetting  # noqa: E402
from app.models.inspection import InspectionItem, InspectionRun  # noqa: E402
from app.models.llm_call_log import LlmCallLog  # noqa: E402

from app.main import app  # noqa: E402,F401  （导入即注册全部模型，create_all 全量建表）

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---- 建表（隔离自建的 /tmp 临时库）----
Base.metadata.create_all(engine)
db = SessionLocal()

# 四条模型连接（都启用）：兜底=第一个启用=conn-A；四场景各绑一条
for label, host, key, model in (("conn-A", "a.example", "sk-A", "model-a"),
                                ("conn-B", "b.example", "sk-B", "model-b"),
                                ("conn-C", "c.example", "sk-C", "model-c"),
                                ("conn-D", "d.example", "sk-D", "model-d")):
    db.add(AiSetting(provider_label=label, base_url=f"https://{host}/v1",
                     api_key_cipher=encrypt_secret(key), model=model, enabled=True))
db.commit()
IDS = {s.provider_label: s.id for s in db.query(AiSetting).all()}
IDA, IDB, IDC, IDD = IDS["conn-A"], IDS["conn-B"], IDS["conn-C"], IDS["conn-D"]

# 场景矩阵：ask→B / diagnosis→C / inspection→D / copilot→A（四场景四连接）
for sc, sid in (("ask", IDB), ("diagnosis", IDC), ("inspection", IDD), ("copilot", IDA)):
    db.add(AiScenarioModel(scenario=sc, setting_id=sid, enabled=True))
db.commit()

# 巡检日报前置：一条 ok 的巡检 run + 一条事实
_run = InspectionRun(run_date=date.today(), status="ok")
db.add(_run)
db.commit()
db.add(InspectionItem(run_id=_run.id, category="cost", level="warning",
                      title="测试事实", detail="mock 事实明细"))
db.commit()

# ---- mock LLM 出口：捕获 endpoint/Authorization，按脚本回内容 ----
CAPTURED: list = []          # [{url, auth, messages}]
SCRIPT: list = []            # 每次调用回的 content（按序弹出）


class _Resp:
    def __init__(self, content: str):
        self.status_code = 200
        self._content = content
        self.text = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def _fake_post(url, json=None, headers=None, timeout=None, verify=None):
    content = SCRIPT.pop(0) if SCRIPT else "{}"
    CAPTURED.append({"url": url, "auth": (headers or {}).get("Authorization"),
                     "messages": json.get("messages")})
    return _Resp(content)


import app.services.llm as llm_mod  # noqa: E402
import app.services.copilot.llm as copilot_llm_mod  # noqa: E402

llm_mod.httpx = SimpleNamespace(post=_fake_post, HTTPError=_real_httpx.HTTPError)
copilot_llm_mod.httpx = SimpleNamespace(post=_fake_post, HTTPError=_real_httpx.HTTPError)

from app.services.ai_scenarios import require_model, resolve_model  # noqa: E402
from app.services.ask import run_ask  # noqa: E402
from app.services.diagnosis import run_diagnosis  # noqa: E402
from app.services.llm import LLMError, chat, get_active_setting  # noqa: E402
from app.services.report import generate_report  # noqa: E402

ROUTE_JSON = '{"tools": [{"tool": "chat", "params": {}}], "reason": "t"}'
DIAG_ROUTE_JSON = '{"scenario": "link_fault", "reason": "r"}'
DIAG_FINAL_JSON = '{"final": true, "answer": "C", "level": "ok"}'


def _urls() -> list:
    return [c["url"] for c in CAPTURED]


def _auths() -> list:
    return [c["auth"] for c in CAPTURED]


def _log_setting_ids(purpose: str) -> set:
    return {r.setting_id for r in db.query(LlmCallLog).filter(LlmCallLog.purpose == purpose).all()}


# ===== A 四场景各绑不同连接 → 调用走对应连接 =====
print("A 绑定连接生效（ask→B / diagnosis→C / inspection→D / copilot→A）")

# ask → conn-B（路由 + 回答两次 chat 都走 B）
CAPTURED.clear()
SCRIPT.clear()
SCRIPT.append(ROUTE_JSON)
SCRIPT.append("ANSWER-B")
out = run_ask(db, 1, "多少台机器")
check("A ask 两次调用全走绑定 conn-B（endpoint）",
      len(CAPTURED) == 2 and all(u.startswith("https://b.example/") for u in _urls()), str(_urls()))
check("A ask 走绑定 conn-B（Bearer key）",
      all(a == "Bearer sk-B" for a in _auths()), str(_auths()))
check("A ask 返回正常", out["answer"] == "ANSWER-B", str(out)[:120])
check("A ask llm_call_logs 留痕=绑定连接", _log_setting_ids("nl_query") == {IDB},
      str(_log_setting_ids("nl_query")))

# diagnosis → conn-C（分诊 + 决策两次 chat 都走 C）
CAPTURED.clear()
SCRIPT.clear()
SCRIPT.append(DIAG_ROUTE_JSON)
SCRIPT.append(DIAG_FINAL_JSON)
sess = run_diagnosis(db, 1, "网站打不开")
check("A diagnosis 两次调用全走绑定 conn-C（endpoint）",
      len(CAPTURED) == 2 and all(u.startswith("https://c.example/") for u in _urls()), str(_urls()))
check("A diagnosis 走绑定 conn-C（Bearer key）",
      all(a == "Bearer sk-C" for a in _auths()), str(_auths()))
check("A diagnosis 会话正常收敛", sess.get("status") == "ok", str(sess)[:120])
check("A diagnosis llm_call_logs 留痕=绑定连接",
      _log_setting_ids("diagnosis") == {IDC} and _log_setting_ids("diagnosis_route") == {IDC},
      f"{_log_setting_ids('diagnosis')}/{_log_setting_ids('diagnosis_route')}")

# inspection（report.py 日报）→ conn-D
CAPTURED.clear()
SCRIPT.clear()
SCRIPT.append("# 日报\n测试摘要")
res = generate_report(db, notify=False)
check("A inspection 日报调用走绑定 conn-D（endpoint）",
      len(CAPTURED) == 1 and _urls()[0].startswith("https://d.example/"), str(_urls()))
check("A inspection 走绑定 conn-D（Bearer key）",
      _auths() == ["Bearer sk-D"], str(_auths()))
check("A inspection 生成正常", res.get("report_id") is not None, str(res)[:120])
check("A inspection llm_call_logs 留痕=绑定连接", _log_setting_ids("inspection") == {IDD},
      str(_log_setting_ids("inspection")))
from app.models.inspection_report import InspectionReport  # noqa: E402

_rep = db.query(InspectionReport).order_by(InspectionReport.id.desc()).first()
check("A 日报落库如实记绑定连接（provider_label/model）",
      _rep is not None and _rep.provider_label == "conn-D" and _rep.model == "model-d",
      str((_rep.provider_label, _rep.model)) if _rep else "无报告行")

# copilot → conn-A（resolve_model + chat_once 出口层；loop.py 不动）
cfg = resolve_model(db, "copilot")
check("A copilot resolve_model 用绑定 conn-A", cfg is not None and cfg["setting_id"] == IDA,
      str(cfg))
CAPTURED.clear()
SCRIPT.clear()
SCRIPT.append("HI-A")
copilot_llm_mod.chat_once(cfg, [{"role": "user", "content": "hi"}], purpose="copilot", db=db)
check("A copilot 调用走绑定 conn-A（endpoint + key）",
      len(CAPTURED) == 1 and _urls()[0].startswith("https://a.example/")
      and _auths() == ["Bearer sk-A"], f"{_urls()} {_auths()}")

# ===== B 未绑定兜底：默认连接（resolve_model 既有兜底语义不变）=====
print("B 未绑定兜底=默认连接")
db.get(AiScenarioModel, "ask").setting_id = 0
db.commit()
CAPTURED.clear()
SCRIPT.clear()
SCRIPT.append(ROUTE_JSON)
SCRIPT.append("ANSWER-FALLBACK")
out = run_ask(db, 1, "多少台机器")
check("B 解绑后 ask 回默认连接 conn-A（endpoint + key）",
      len(CAPTURED) == 2 and all(u.startswith("https://a.example/") for u in _urls())
      and all(a == "Bearer sk-A" for a in _auths()), f"{_urls()} {_auths()}")
old = get_active_setting(db)
check("B 兜底与旧全局口径同一行（conn-A）",
      old is not None and old.id == IDA and resolve_model(db, "ask")["setting_id"] == IDA,
      f"get_active_setting={getattr(old, 'id', None)}")
# 复原绑定（后续段落回归 A 路径）
db.get(AiScenarioModel, "ask").setting_id = IDB
db.commit()

# ===== C 场景停用 → require_model 拒 + 消费面同拒 =====
print("C 场景停用=真断电")
for sc, fn, label in (
        ("ask", lambda: run_ask(db, 1, "多少台机器"), "AI 分析"),
        ("diagnosis", lambda: run_diagnosis(db, 1, "网站打不开"), "故障诊断"),
        ("inspection", lambda: generate_report(db, notify=False), "巡检中心")):
    db.get(AiScenarioModel, sc).enabled = False
    db.commit()
    try:
        require_model(db, sc)
        check(f"C {sc} 停用 → require_model 拒", False)
    except LLMError as e:
        check(f"C {sc} 停用 → require_model 拒（LLMError 引导含场景名）",
              label in str(e), str(e))
    CAPTURED.clear()
    SCRIPT.clear()
    try:
        fn()
        check(f"C {sc} 停用 → 消费面同拒", False)
    except LLMError:
        check(f"C {sc} 停用 → 消费面同拒（LLMError=API 502 引导类）且零 LLM 调用",
              len(CAPTURED) == 0)
    db.get(AiScenarioModel, sc).enabled = True
    db.commit()

# ===== D 旧 get_active_setting 调用方零破坏 =====
print("D get_active_setting / chat(AiSetting) 旧契约零破坏")
import inspect  # noqa: E402

sig = inspect.signature(get_active_setting)
check("D get_active_setting 签名未动（仅 db）", list(sig.parameters) == ["db"], str(sig))
old = get_active_setting(db)
check("D 仍返回 AiSetting ORM 行（首选启用连接 conn-A）",
      isinstance(old, AiSetting) and old.id == IDA and hasattr(old, "api_key_cipher"),
      f"{type(old).__name__}:{getattr(old, 'id', None)}")
CAPTURED.clear()
SCRIPT.clear()
SCRIPT.append("OLD-PATH")
r = chat(old, [{"role": "user", "content": "hi"}], purpose="test", db=db)
check("D chat(AiSetting) 旧直调路径照走（endpoint + key）",
      r["content"] == "OLD-PATH" and _urls()[0].startswith("https://a.example/")
      and _auths() == ["Bearer sk-A"], f"{_urls()} {_auths()}")

from app.services import ask as ask_mod, diagnosis as diag_mod, report as report_mod  # noqa: E402

import ast  # noqa: E402

for name, fn in (("ask", ask_mod.run_ask), ("diagnosis", diag_mod.run_diagnosis),
                 ("report", report_mod.generate_report)):
    # AST 口径：只看代码引用（Name/Attribute 节点），注释/文档提到旧函数名不算
    tree = ast.parse(inspect.getsource(fn))
    refs = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check(f"D {name} 消费面源码锁定：走 require_model、代码零引用 get_active_setting",
          "require_model" in refs and "get_active_setting" not in refs, str(sorted(refs)[:20]))

db.close()
print(f"\n场景模型绑定 mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
