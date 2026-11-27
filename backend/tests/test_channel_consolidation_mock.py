#!/usr/bin/env python3
"""通知渠道收口隔离冒烟：一次性 PostgreSQL 全链路验证，绝不真实外发。

运行：cd /opt/qianshu/backend && python3 tests/test_channel_consolidation_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu 库、不用真实主密钥、不起后台任务（ASGI 直连不进 lifespan）；
send_to_channel 全程钉死成计数器（红线：绝不向真实企微/钉钉/Webhook 发消息）。

验证项（对应收口清单）：
  A 单一管理入口：渠道 CRUD 唯一管理面=系统设置（/alerts/channels 写路由保留且全链真跑+审计）；
    集成中心零写调用（Integrations.vue 无 /alerts/channels 写口、无渠道表单对话框）；
    集成中心 API 只读（无写路由 + 非写方法 405）。
  B 测试按钮语义：「发送测试消息」全站唯一=Settings 渠道行（用户主动点击+二次确认才发）；
    /alerts/channels/{id}/test 真发一次（send_to_channel 计数=1）；
    集成中心「检查连通」只读零发送（计数=0）；文案钉死语义（发送口明示真发/检查口明示只读）。
  C 命名统一：全站「通知渠道」/「Notification Channels」（zh/en i18n + 审计 ACTION_LABELS），
    无「告警渠道」/「Alert Channels」残留。
  D 回归 integrations、alerts 不破：registry 渠道行照旧（状态展示）+ check-channel 200；
    alerts 渠道/规则/记录路由与列表可用。
"""
import os
import re
import sys
import tempfile
import types

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-ch-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("ch")
os.environ["MASTER_KEY"] = "ab" * 32          # 64 hex 测试主密钥

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402,F401  导入全量模型（与 startup create_all 同口径）
from app.models.audit import AuditLog  # noqa: E402
from app.models.alert_channel import AlertChannel  # noqa: E402

FE = "/opt/qianshu/frontend/src"
PASS, FAIL = [], []
SENDS = []          # send_to_channel 计数器（绝不真发）


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _forbid_send(row, title, content, **kw):
    """红线：绝不对外发消息——只计数。"""
    SENDS.append((getattr(row, "name", "?"), title))


# send_to_channel 两个绑定面都钉死（notify 模块 + alerts.py 的 from-import）
import app.services.notify as _notify  # noqa: E402
import app.api.v1.alerts as _alerts_mod  # noqa: E402
_notify.send_to_channel = _forbid_send
_alerts_mod.send_to_channel = _forbid_send


def _make_req():
    """ASGI 直连请求壳（不触发 lifespan/startup；沿用 test_credential_consolidation_mock 套路）。"""
    import asyncio

    import httpx

    import app.core.deps as deps

    fake = types.SimpleNamespace(id=7, username="chsmoke", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: fake

    def _req(method, url, **kw):
        async def _go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(_go())

    return _req


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _audit_actions(db: Session) -> list:
    return [a.action for a in db.query(AuditLog).all()]


# ================= A. 单一管理入口（CRUD 单口 + 集成中心只读） =================
def test_single_entry_api() -> None:
    print("A. 单一管理入口（API 层：/alerts/channels CRUD + 集成中心只读）")
    Base.metadata.create_all(bind=engine)
    _req = _make_req()
    try:
        r = _req("POST", "/api/alerts/channels", json={
            "name": "ops-bot", "type": "wecom_bot",
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TESTKEY",
            "at_mobiles": [], "enabled": True})
        check("A1 渠道新增（唯一管理面 API 保留）", r.status_code == 200, r.text[:200])
        ch_id = r.json()["id"]
        check("A2 webhook 脱敏输出", "TESTKEY" not in r.text and "qyapi.weixin.qq.com" in r.text, r.text[:200])

        r = _req("PUT", f"/api/alerts/channels/{ch_id}", json={"enabled": False, "name": "ops-bot2"})
        check("A3 渠道修改", r.status_code == 200 and r.json()["enabled"] is False, r.text[:200])
        r = _req("GET", "/api/alerts/channels")
        check("A4 渠道列表", r.status_code == 200 and len(r.json()) == 1, r.text[:200])

        with SessionLocal() as db:
            acts = _audit_actions(db)
        check("A5 CRUD 三类审计动作落库",
              {"alerts.channel.create", "alerts.channel.update"} <= set(acts), str(acts))

        # 集成中心 API 只读：非写方法 405 + 无渠道写路由
        r1 = _req("PUT", "/api/integrations/check-channel/1", json={})
        r2 = _req("DELETE", "/api/integrations/check-channel/1")
        r3 = _req("PUT", "/api/integrations/registry", json={})
        check("A6 集成中心接口拒绝写方法（405）",
              [r1.status_code, r2.status_code, r3.status_code] == [405, 405, 405],
              str([r1.status_code, r2.status_code, r3.status_code]))

        paths = app.openapi()["paths"]
        integ = {p: m for p, m in paths.items() if p.startswith("/api/integrations")}
        write = {p: [k for k in m if k.lower() in ("post", "put", "delete", "patch")]
                 for p, m in integ.items()}
        bad = {p: v for p, v in write.items()
               if set(v) - {"post"} or (set(v) & {"post"} and "check-channel" not in p and "/test/" not in p)}
        check("A7 /integrations 无渠道写路由（仅只读探测 POST）", not bad, str(bad))

        r = _req("DELETE", f"/api/alerts/channels/{ch_id}")
        check("A8 渠道删除（唯一管理面 API 保留）", r.status_code == 200 and r.json()["ok"] is True,
              r.text[:200])
        with SessionLocal() as db:
            acts = _audit_actions(db)
            left = db.query(AlertChannel).count()
        check("A9 删除审计 + 渠道清零", "alerts.channel.delete" in acts and left == 0, str(acts))
    finally:
        app.dependency_overrides.clear()


def test_single_entry_source() -> None:
    print("A'. 单一管理入口（前端源码层：写调用只在 Settings）")
    views = {}
    for sub in ("views", "api"):
        base = os.path.join(FE, sub)
        for fn in os.listdir(base):
            if fn.endswith((".vue", ".js")):
                views[fn] = _read(os.path.join(base, fn))

    crud_importers = [fn for fn, s in views.items()
                      if re.search(r"import\s*\{[^}]*(createChannel|updateChannel|deleteChannel)", s, re.S)]
    check("A10 渠道 CRUD 调用方唯一=Settings.vue",
          crud_importers == ["Settings.vue"], str(crud_importers))

    write_hits = []
    for fn, s in views.items():
        if fn == "alerts.js":
            continue   # API 封装模块本身（Settings/巡检经它调用）
        for m in re.finditer(r"http\.(post|put|delete)\(\s*[`'\"]([^`'\"]+)", s):
            if m.group(2).startswith("/alerts/channels"):
                write_hits.append(f"{fn}:{m.group(1)}{m.group(2)}")
    check("A11 视图层无 /alerts/channels 直写", not write_hits, str(write_hits))

    integ = views["Integrations.vue"]
    check("A12 集成中心无渠道表单对话框/写状态",
          "chDialog" not in integ and "saveChannel" not in integ and "openChannelEdit" not in integ,
          "残留渠道写入口")
    check("A13 集成中心保留只读展示+跳转",
          "check-channel" in integ and "goChannelSettings" in integ
          and "/settings?tab=channels" in integ, "缺只读检查或跳转")


# ================= B. 测试按钮语义（仅 Settings 一处可发） =================
def test_send_semantics() -> None:
    print("B. 测试按钮语义（发送口唯一=Settings；集成中心只读零发送）")
    _req = _make_req()
    try:
        ch_id = _req("POST", "/api/alerts/channels", json={
            "name": "sem-bot", "type": "wecom_bot",
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K2",
            "at_mobiles": [], "enabled": True}).json()["id"]
        SENDS.clear()

        r = _req("POST", f"/api/alerts/channels/{ch_id}/test")
        j = r.json()
        check("B1 发送测试消息=真实推送一次（send_to_channel 计数 1）",
              r.status_code == 200 and len(SENDS) == 1, f"{r.status_code} sends={len(SENDS)}")
        check("B2 返回文案明示已发送", j.get("ok") is True and "已发送" in j.get("message", ""), str(j))

        with SessionLocal() as db:
            acts = _audit_actions(db)
        check("B3 审计动作=发送渠道测试消息", "alerts.channel.test" in acts, str(acts))

        r = _req("POST", f"/api/integrations/check-channel/{ch_id}")
        check("B4 检查连通=只读零发送", r.status_code == 200 and len(SENDS) == 1,
              f"{r.status_code} sends={len(SENDS)}")
        check("B5 检查连通返回判定与发送记录证据",
              r.json().get("verdict") in ("connected", "unverified", "disabled", "error")
              and "evidence" in r.json(), r.text[:200])

        _req("DELETE", f"/api/alerts/channels/{ch_id}")
    finally:
        app.dependency_overrides.clear()

    print("B'. 测试按钮语义（前端源码层：文案钉死）")
    zh = {p: _read(os.path.join(FE, "i18n/locales/zh-CN", p))
          for p in ("settings.js", "integrations.js", "inspections.js")}
    en = {p: _read(os.path.join(FE, "i18n/locales/en-US", p))
          for p in ("settings.js", "integrations.js", "inspections.js")}

    views = {fn: _read(os.path.join(FE, "views", fn))
             for fn in os.listdir(os.path.join(FE, "views")) if fn.endswith(".vue")}
    send_btn_users = [fn for fn, s in views.items() if "settings.btn.send_test" in s]
    check("B6「发送测试消息」按钮全站唯一=Settings 渠道行",
          send_btn_users == ["Settings.vue"], str(send_btn_users))

    check("B7 发送口文案明示真发（zh 逐字口径）",
          "将向该渠道真实发送一条测试消息" in zh["settings.js"]
          and "really send one test message" in en["settings.js"], "确认文案缺真发明示")
    check("B8 检查口文案明示只读（zh/en）",
          "检查连通（只读）" in zh["integrations.js"]
          and "read-only" in en["integrations.js"], "检查连通未明示只读")

    integ = views["Integrations.vue"]
    check("B9 集成中心零发送触发（无 testChannel/发送口/渠道写口）",
          "testChannel" not in integ and "send_test" not in integ
          and "/alerts/channels" not in integ, "集成中心仍可触发发送或写渠道")

    # 发送类按钮文案全类明示真发（含巡检测试推送——保留的真发口也必须明示）
    check("B10 巡检测试推送文案明示真发（zh/en）",
          "真实发送一条测试消息" in zh["inspections.js"]
          and "Really sends one test message" in en["inspections.js"], "巡检真发口文案未明示")


# ================= C. 命名统一（通知渠道） =================
def test_naming() -> None:
    print("C. 命名统一（全站「通知渠道」）")
    zh_files = ["settings.js", "integrations.js", "inspections.js", "oncall.js", "alerts.js"]
    en_files = zh_files
    zh = {p: _read(os.path.join(FE, "i18n/locales/zh-CN", p)) for p in zh_files}
    en = {p: _read(os.path.join(FE, "i18n/locales/en-US", p)) for p in en_files}

    bad_zh = [p for p, s in zh.items() if "告警渠道" in s]
    bad_en = [p for p, s in en.items() if re.search(r"Alert Channels?|alert channel", s)]
    check("C1 i18n 无「告警渠道」/「Alert Channels」残留",
          not bad_zh and not bad_en, f"zh={bad_zh} en={bad_en}")

    check("C2 页签名统一「通知渠道」/「Notification Channels」",
          "'settings.tab.channels': '通知渠道'" in zh["settings.js"]
          and "'settings.tab.channels': 'Notification Channels'" in en["settings.js"],
          "页签名未统一")

    check("C3 规则表单列名本就是「通知渠道」（不动回归）",
          "'alerts.form.channels': '通知渠道'" in zh["alerts.js"]
          and "'alerts.form.channels': 'Notification channels'" in en["alerts.js"],
          "alerts 列名被误改")

    from app.services.audit import ACTION_LABELS

    ch_labels = {k: v for k, v in ACTION_LABELS.items() if k.startswith("alerts.channel.")}
    check("C4 审计 ACTION_LABELS 渠道动作统一「通知渠道」+ 真发语义",
          all("通知渠道" in v for k, v in ch_labels.items() if k != "alerts.channel.test")
          and "真实推送" in ch_labels.get("alerts.channel.test", ""),
          str(ch_labels))

    from app.services.audit import humanize

    h = humanize("alerts.channel.test", resource_type="alert_channel", resource_id="9")
    check("C5 审计 humanize 视图同步", "发送渠道测试消息" in h["summary"], str(h))


# ================= D. 回归 integrations、alerts 不破 =================
def test_regression() -> None:
    print("D. 回归（integrations / alerts）")
    _req = _make_req()
    try:
        ch_id = _req("POST", "/api/alerts/channels", json={
            "name": "reg-bot", "type": "dingtalk_bot",
            "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=R",
            "at_mobiles": ["13800000000"], "enabled": True}).json()["id"]

        r = _req("GET", "/api/integrations/registry")
        rows = r.json().get("rows", r.json() if isinstance(r.json(), list) else [])
        ch_rows = [x for x in rows if x.get("category") == "channel"]
        check("D1 registry 渠道行照旧（状态展示保留）",
              r.status_code == 200 and len(ch_rows) >= 1, r.text[:200])
        by = {x["id"]: x for x in ch_rows}
        check("D2 渠道行带真实状态与 ref_id",
              f"channel:{ch_id}" in by and by[f"channel:{ch_id}"]["status"] in
              ("unverified", "connected", "error", "disabled"), str(by.get(f"channel:{ch_id}")))

        r = _req("POST", f"/api/integrations/check-channel/{ch_id}")
        check("D3 check-channel 200 不破", r.status_code == 200, r.text[:200])

        for path in ("/api/alerts/rules", "/api/alerts/records", "/api/alerts/channels",
                     "/api/alerts/eval/status"):
            rr = _req("GET", path)
            check(f"D4 回归 {path}", rr.status_code == 200, f"{rr.status_code} {rr.text[:120]}")

        _req("DELETE", f"/api/alerts/channels/{ch_id}")
    finally:
        app.dependency_overrides.clear()


def main() -> int:
    test_single_entry_api()
    test_single_entry_source()
    test_send_semantics()
    test_naming()
    test_regression()
    print(f"\n渠道收口冒烟结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
