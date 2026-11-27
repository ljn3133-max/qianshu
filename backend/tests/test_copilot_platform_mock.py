#!/usr/bin/env python3
"""千枢智能体平台管理域 mock 测试：
用户/权限变更工具 + 通用平台接口 + 敏感参数加密 + 回滚。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_platform_mock.py
隔离性：DATABASE_URL/MASTER_KEY 临时值先于 app 导入——不碰真实 qianshu.db；
API 直调函数（不启 8810）；零外部网络。

验证项：
  A create_user 全链：显式密码（fixture reader/Fixture!Pass9）+ 一次性生成密码
    （一次性密码结果回显一次；无论密码来源 must_change_password 恒强制首登改密）；
    快照落库；回滚=删号
  B update_user / reset_user_password：改角色→回滚恢复；重置密码→强制改密+不可逆说明
  C save_permission_policy：allow/deny 覆盖→回滚删除（回角色默认）
  D platform_api_call：黑名单拒（DELETE/wipe/确认门自批）→拒建单；越权拒（viewer 写/operator 用户域）；
    合法路径通（PUT /api/inspections/push-settings 真执行）
  E platform_api_query：GET 合法路径通；黑名单/越权拒（viewer 查用户清单）
  F 敏感参数加密落库：change_list_json 无 AK/密码明文（enc:v1:），执行回读解密，
    ticket_out 展示掩码；sensitive 项强制 ack_required
"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置 ----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-platform-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("platform")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import Base, SessionLocal, engine as db_engine  # noqa: E402

import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.core.security import hash_password, verify_password  # noqa: E402
from app.models.agent_change import AgentChangeSnapshot  # noqa: E402
from app.models.inspection_report import PushSetting  # noqa: E402
from app.models.permission import PermissionPolicy  # noqa: E402
from app.models.user import User  # noqa: E402

from app.services.copilot import change_gate, change_tools, tools_read  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


from app.main import app  # noqa: E402,F401  全量注册模型/路由（建表用）

Base.metadata.create_all(db_engine)
STUB_ADMIN = SimpleNamespace(id=991, username="admin", role="admin", is_active=True)
STUB_OPERATOR = SimpleNamespace(id=992, username="op", role="operator", is_active=True)
STUB_VIEWER = SimpleNamespace(id=993, username="vi", role="viewer", is_active=True)


def _build(items, user=STUB_ADMIN):
    tk = change_tools.build_ticket(SessionLocal(), 0, 0, items, user)
    tk.setdefault("ticket_id", tk.get("id"))
    return tk


def _exec(tid, user=STUB_ADMIN):
    return change_gate.execute_ticket(SessionLocal(), tid, user, ack="我已知晓")


def _rollback(tid, user=STUB_ADMIN):
    return change_gate.rollback_ticket(SessionLocal(), tid, user)


def _result(res: dict) -> dict:
    """handler 返回 {ref, result}；执行结果取内层 result。"""
    return (res.get("results") or [{}])[0].get("result", {}).get("result") or {}


# ---------------- A create_user 全链 ----------------

print("== A create_user（显式密码 + 一次性初始密码） ==")
# A1 显式密码 fixture：创建只读账号 reader 密码 Fixture!Pass9
tk = _build([{"tool": "create_user",
              "params": {"username": "reader", "role": "viewer",
                         "display_name": "只读账号", "password": "Fixture!Pass9"}}])
item = tk["change_list"][0]
check("A1 工单：sensitive+ack_required（密码类强制二次确认）",
      item["sensitive"] is True and item["ack_required"] is True, str(item)[:200])
res = _exec(tk["ticket_id"])
row = SessionLocal().query(User).filter(User.username == "reader").first()
check("A1 显式密码创建成功：角色 viewer + 密码可登录",
      res["status"] == "done" and row is not None and row.role == "viewer"
      and verify_password("Fixture!Pass9", row.password_hash), str(res)[:200])
check("A1 显式密码创建也恒强制首登改密",
      _result(res).get("password_source") == "given"
      and row.must_change_password is True
      and _result(res).get("must_change_password") is True, str(_result(res))[:200])
snap = SessionLocal().query(AgentChangeSnapshot).filter(
    AgentChangeSnapshot.ticket_id == tk["ticket_id"]).first()
before = json.loads(snap.before_blob or "{}")
check("A1 快照：新建 before 空 + 不含 password_hash",
      before == {} and "password_hash" not in (snap.before_blob or "")
      and "Fixture!Pass9" not in (snap.before_blob or ""), str(before)[:100])
rb = _rollback(tk["ticket_id"])
check("A1 回滚=删号",
      rb.get("rolled_back") is True
      and SessionLocal().query(User).filter(User.username == "reader").first() is None,
      str(rb)[:200])

# A2 一次性初始密码（password 留空）
tk2 = _build([{"tool": "create_user", "params": {"username": "reader2", "role": "operator"}}])
res2 = _exec(tk2["ticket_id"])
result2 = _result(res2)
row2 = SessionLocal().query(User).filter(User.username == "reader2").first()
pw = result2.get("initial_password") or ""
check("A2 生成一次性初始密码（回显一次，≥8 位混排）",
      res2["status"] == "done" and len(pw) >= 8 and result2.get("password_source") == "generated"
      and verify_password(pw, row2.password_hash), str(result2)[:200])
check("A2 强制改密标记 must_change_password",
      result2.get("must_change_password") is True and row2.must_change_password is True)
ticket_raw = SessionLocal().get(
    change_tools.AgentChangeTicket, tk2["ticket_id"]).result_json
check("A2 一次性密码不落库明文（result_json 加密存储）",
      pw not in (ticket_raw or "") and "enc:v1:" in (ticket_raw or ""), (ticket_raw or "")[:200])
out = change_tools.ticket_out(
    SessionLocal().get(change_tools.AgentChangeTicket, tk2["ticket_id"]))
check("A2 ticket_out 展示掩码（****，明文不回显）",
      pw not in json.dumps(out, ensure_ascii=False)
      and "****" in json.dumps(out, ensure_ascii=False), json.dumps(out, ensure_ascii=False)[:300])
_rollback(tk2["ticket_id"])

# ---------------- B update_user / reset_user_password ----------------

print("== B update_user / reset_user_password ==")
_db = SessionLocal()
_db.add(User(username="bob", password_hash=hash_password("Bob-pass123"),
             role="viewer", display_name="Bob", must_change_password=False))
_db.commit()
_db.close()
bob = SessionLocal().query(User).filter(User.username == "bob").first()
tk3 = _build([{"tool": "update_user", "params": {"username": "bob", "role": "operator",
                                                 "enabled": False}}])
check("B update_user 工单 sensitive+ack", tk3["change_list"][0]["sensitive"] is True
      and tk3["change_list"][0]["ack_required"] is True)
res3 = _exec(tk3["ticket_id"])
bob = SessionLocal().query(User).filter(User.username == "bob").first()
check("B 执行→角色 operator + 禁用", res3["status"] == "done"
      and bob.role == "operator" and bob.is_active is False, str(res3)[:200])
rb3 = _rollback(tk3["ticket_id"])
bob = SessionLocal().query(User).filter(User.username == "bob").first()
check("B 回滚→恢复 viewer + 启用", rb3.get("rolled_back") is True
      and bob.role == "viewer" and bob.is_active is True, str(rb3)[:200])

tk4 = _build([{"tool": "reset_user_password", "params": {"username": "bob"}}])
res4 = _exec(tk4["ticket_id"])
bob = SessionLocal().query(User).filter(User.username == "bob").first()
pw4 = _result(res4).get("initial_password") or ""
check("B 重置密码→一次性密码可登录 + 强制改密",
      res4["status"] == "done" and verify_password(pw4, bob.password_hash)
      and bob.must_change_password is True, str(res4)[:200])
rb4 = _rollback(tk4["ticket_id"])
check("B 重置回滚=不可逆说明（旧哈希不入快照）",
      rb4.get("rolled_back") is True and "不可逆" in json.dumps(rb4, ensure_ascii=False)
      and verify_password(pw4, SessionLocal().query(User)
                          .filter(User.username == "bob").first().password_hash),
      str(rb4)[:200])

# ---------------- C save_permission_policy ----------------

print("== C save_permission_policy ==")
tk5 = _build([{"tool": "save_permission_policy",
               "params": {"user_id": bob.id, "code": "agent.change", "effect": "deny"}}])
check("C 权限策略工单 sensitive+ack", tk5["change_list"][0]["sensitive"] is True
      and tk5["change_list"][0]["ack_required"] is True)
res5 = _exec(tk5["ticket_id"])
pol = SessionLocal().query(PermissionPolicy).filter(
    PermissionPolicy.user_id == bob.id, PermissionPolicy.code == "agent.change").first()
check("C 执行→deny 覆盖真写入", res5["status"] == "done" and pol is not None
      and pol.effect == "deny", str(res5)[:200])
rb5 = _rollback(tk5["ticket_id"])
check("C 回滚→覆盖删除（回角色默认）",
      rb5.get("rolled_back") is True
      and SessionLocal().query(PermissionPolicy).filter(
          PermissionPolicy.user_id == bob.id).count() == 0, str(rb5)[:200])

# ---------------- D platform_api_call ----------------

print("== D platform_api_call（黑名单/越权/合法） ==")
for tool_params, why in (
        ({"method": "DELETE", "path": "/api/users/99"}, "删除类"),
        ({"method": "POST", "path": "/api/k8mgmt/cluster/wipe"}, "wipe"),
        ({"method": "POST", "path": "/api/k8mgmt/upgrade"}, "升级"),
        ({"method": "POST", "path": "/api/agent/changes/1/confirm"}, "确认门自批"),
        ({"method": "POST", "path": "/api/k8s/clusters"}, "集群创建"),
        ({"method": "POST", "path": "/api/auth/password"}, "认证密码类"),
        ({"method": "POST", "path": "/api/k8mgmt/resources/batch"}, "批量类")):
    try:
        _build([{"tool": "platform_api_call", "params": tool_params}])
        check(f"D 黑名单拒建单（{why}）", False, str(tool_params))
    except change_tools.ChangeToolError as e:
        check(f"D 黑名单拒建单（{why}）", "拒绝执行" in str(e), str(e)[:150])

try:
    _build([{"tool": "platform_api_call",
             "params": {"method": "POST", "path": "/api/users",
                        "body": {"username": "x", "password": "Xpass-123", "role": "viewer"}}}],
           user=STUB_OPERATOR)
    check("D 越权拒建单（operator 用户域写）", False)
except change_tools.ChangeToolError as e:
    check("D 越权拒建单（operator 用户域写）", "越权" in str(e), str(e)[:150])
try:
    _build([{"tool": "platform_api_call",
             "params": {"method": "POST", "path": "/api/alerts/rules",
                        "body": {"name": "x", "source": "metric", "threshold": 1}}}],
           user=STUB_VIEWER)
    check("D 越权拒建单（viewer 写）", False)
except change_tools.ChangeToolError as e:
    check("D 越权拒建单（viewer 写）", "越权" in str(e), str(e)[:150])
try:
    _build([{"tool": "platform_api_call",
             "params": {"method": "POST", "path": "/api/no/such/path", "body": {}}}])
    check("D 未注册路径拒建单", False)
except change_tools.ChangeToolError as e:
    check("D 未注册路径拒建单", "未注册" in str(e), str(e)[:150])

tk6 = _build([{"tool": "platform_api_call",
               "params": {"method": "PUT", "path": "/api/inspections/push-settings",
                          "body": {"enabled": True, "channel_ids": [7]}}}])
check("D 合法路径建单（sensitive+ack）", tk6["change_list"][0]["sensitive"] is True
      and tk6["change_list"][0]["ack_required"] is True)
res6 = _exec(tk6["ticket_id"])
ps = SessionLocal().query(PushSetting).filter(PushSetting.scene == "inspection").first()
check("D 合法路径执行→目标端点真生效（push-settings）",
      res6["status"] == "done" and ps is not None and ps.enabled is True
      and json.loads(ps.channel_ids) == [7], str(res6)[:300])
rb6 = _rollback(tk6["ticket_id"])
check("D 通用调用回滚=留现场说明（无自动回滚）",
      rb6.get("rolled_back") is True and "无自动回滚" in json.dumps(rb6, ensure_ascii=False),
      str(rb6)[:200])

# ---------------- E platform_api_query ----------------

print("== E platform_api_query（只读 GET） ==")
r = tools_read.execute(SessionLocal(), "platform_api_query",
                       {"path": "/api/settings/product-cards"}, user=STUB_VIEWER)
check("E GET 合法路径通（viewer 可读）",
      r.get("ok") is True and isinstance(r.get("data"), list) and r["data"], str(r)[:200])
r = tools_read.execute(SessionLocal(), "platform_api_query",
                       {"path": "/api/users"}, user=STUB_VIEWER)
check("E 越权拒（viewer 查用户清单=admin 专用）", "error" in r and "越权" in r["error"],
      str(r)[:200])
r = tools_read.execute(SessionLocal(), "platform_api_query",
                       {"path": "/api/users"}, user=STUB_ADMIN)
check("E admin 查用户清单通", r.get("ok") is True and isinstance(r.get("data"), list),
      str(r)[:200])
r = tools_read.execute(SessionLocal(), "platform_api_query",
                       {"method": "POST", "path": "/api/alerts/rules"}, user=STUB_ADMIN)
check("E 只读收口（POST 拒）", "error" in r and "只读" in r["error"], str(r)[:200])
r = tools_read.execute(SessionLocal(), "platform_api_query",
                       {"path": "/api/credentials"}, user=STUB_ADMIN)
check("E 黑名单拒（凭证类端点）", "error" in r and "拒绝" in r["error"], str(r)[:200])

# ---------------- F 敏感参数加密落库 ----------------

print("== F 敏感参数加密落库 + 展示掩码 ==")
tk7 = _build([{"tool": "create_cloud_account",
               "params": {"name": "_pf_acct", "vendor": "aliyun",
                          "ak": "AKPLAINTEXT123", "sk": "SKPLAINTEXT456"}}])
from app.models.agent_change import AgentChangeTicket  # noqa: E402

raw = SessionLocal().get(AgentChangeTicket, tk7["ticket_id"]).change_list_json
check("F change_list_json 无 AK/SK 明文（enc:v1: 加密）",
      "AKPLAINTEXT123" not in raw and "SKPLAINTEXT456" not in raw
      and "enc:v1:" in raw, raw[:300])
stored_item = json.loads(raw)[0]
check("F 敏感参数回读解密还原",
      change_tools.item_params(stored_item)["ak"] == "AKPLAINTEXT123"
      and change_tools.item_params(stored_item)["sk"] == "SKPLAINTEXT456")
check("F 非敏感参数不加密（name 明文可查）",
      stored_item["params"]["name"] == "_pf_acct")
out = change_tools.ticket_out(SessionLocal().get(AgentChangeTicket, tk7["ticket_id"]))
s_out = json.dumps(out, ensure_ascii=False)
check("F ticket_out 展示掩码（****）", "AKPLAINTEXT123" not in s_out
      and "****" in s_out, s_out[:300])
check("F create_cloud_account sensitive=True 且 ack 强制", tk7["change_list"][0]["sensitive"] is True
      and tk7["change_list"][0]["ack_required"] is True)
res7 = _exec(tk7["ticket_id"])
from app.models.cloud_account import CloudAccount  # noqa: E402

acct = SessionLocal().query(CloudAccount).filter(CloudAccount.name == "_pf_acct").first()
check("F 执行回读解密→账号真创建（凭证入库）",
      res7["status"] == "done" and acct is not None, str(res7)[:200])
rb7 = _rollback(tk7["ticket_id"])
check("F 回滚→账号删除", rb7.get("rolled_back") is True
      and SessionLocal().query(CloudAccount).filter(
          CloudAccount.name == "_pf_acct").first() is None, str(rb7)[:200])

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
