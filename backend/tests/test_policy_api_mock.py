#!/usr/bin/env python3
"""监控策略 API 回归：设置页唯一配置面的接口层。

直接调用路由函数（Auth 由 FastAPI 依赖声明保证，这里验证参数/落库/审计/回包），
一次性 PostgreSQL，不碰真实库、不发任何通知。

运行：cd /opt/qianshu/backend && python3 tests/test_policy_api_mock.py
"""
import importlib
import os
import pkgutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.core.db import Base  # noqa: E402
from app.api.v1 import policy as api  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.monitor_policy import MonitorModuleSetting  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import monitor_policy as mp  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="qs-policy-api-"), "t.db")
    engine = create_engine(test_database_url("policyapi"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    mp.ensure_defaults(db)
    return db


REQ = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})


def main():
    db = make_db()
    user = User(id=1, username="admin", role="admin", is_active=True,
                password_hash="x")
    db.add(user)
    acc = CloudAccount(name="vcenter-test", vendor="vcenter", ak_cipher="", secret_cipher="",
                       region="", extra_json="{}", enabled=True, status="ok")
    db.add(acc)
    db.commit()

    # ---- 1) 一屏渲染 ----
    out = api.overview(db=db, _user=user)
    check("概览返回 12 个模块", len(out["modules"]) == 12, str(len(out["modules"])))
    check("概览含数据源与三档默认值",
          len(out["accounts"]) == 1 and set(out["priorities"]) == {"high", "medium", "low"})
    check("账号默认高优先级、去向全开",
          out["accounts"][0]["priority"] == "high"
          and out["accounts"][0]["notify_overview"]
          and out["accounts"][0]["notify_inapp"]
          and out["accounts"][0]["notify_push"])

    # ---- 2) 模块矩阵保存 ----
    body = api.ModuleUpdateIn(modules=[
        api.ModuleUpdate(module="alert", enabled=False, overview=False, inapp=True, push=False),
        # expiry 只支持总览：故意把 inapp/push 传 False，验证不会被写入
        api.ModuleUpdate(module="expiry", enabled=True, overview=False, inapp=False, push=False),
    ])
    r = api.update_modules(body, REQ, db, user)
    row = db.get(MonitorModuleSetting, "alert")
    check("模块总开关落库", row.enabled is False and row.overview is False and row.push is False)
    check("矩阵保存回包为最新值", next(m for m in r["modules"] if m["module"] == "alert")["enabled"] is False)
    # expiry 只支持总览：inapp/push 不该被写
    er = db.get(MonitorModuleSetting, "expiry")
    check("不支持的去向不落假开关", er.overview is False and er.inapp is True and er.push is True)

    # ---- 3) 未知模块拒绝 ----
    try:
        api.update_modules(api.ModuleUpdateIn(modules=[api.ModuleUpdate(module="nope")]),
                           REQ, db, user)
        check("未知模块被拒绝", False)
    except Exception as e:
        check("未知模块被拒绝", getattr(e, "status_code", 0) == 400, str(e))

    # ---- 4) 账号策略：优先级/去向/重申 ----
    r = api.update_account_policy(acc.id, api.AccountPolicyIn(
        priority="low", notify_overview=False, notify_inapp=False, notify_push=False,
        repeat_days=14), REQ, db, user)
    check("账号策略落库", r["priority"] == "low" and not r["notify_overview"])
    check("自定义重申天数生效", r["repeat_days"] == 14 and r["repeat_effective"] == 14,
          str(r.get("repeat_effective")))
    check("节奏文案随优先级变化", "02:00" in r["cadence"], r["cadence"])

    r = api.update_account_policy(acc.id, api.AccountPolicyIn(repeat_days=None), REQ, db, user)
    check("显式 null=跟随优先级（低=30 天）",
          r["repeat_days"] is None and r["repeat_effective"] == 30, str(r.get("repeat_effective")))

    # ---- 5) 批量优先级 ----
    r = api.bulk_priority(api.BulkPriorityIn(ids=[acc.id], priority="medium"), REQ, db, user)
    db.refresh(acc)
    check("批量设优先级", r["updated"] == 1 and acc.priority == "medium")

    # ---- 6) 审计 ----
    actions = {a.action for a in db.query(AuditLog).all()}
    check("模块/账号策略变更都进审计",
          {"policy.module.update", "policy.account.update"} <= actions, str(actions))

    # ---- 7) 逃生门 ----
    from app.core.config import settings
    real = settings.MONITOR_POLICY_ENABLED
    settings.MONITOR_POLICY_ENABLED = False
    try:
        check("逃生门关闭后 gate 全放行",
              mp.can_overview(db, "alert", acc.id) and mp.collect_due(db, "metrics", acc.id))
    finally:
        settings.MONITOR_POLICY_ENABLED = real

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("失败：" + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
