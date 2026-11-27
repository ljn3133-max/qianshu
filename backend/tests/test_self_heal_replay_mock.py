#!/usr/bin/env python3
"""P5 回放集：脱敏真实问题离线重放诊断/确定性计划。

验证点：
  - 每个案例只落目录内 recipe，风险级别与回放预期一致；
  - R4/任意命令/凭证不会出现在计划中；
  - 计划哈希与不可变计划内容一致；
  - 凭证类问题只给人工协助，不生成自动步骤。

运行：cd backend && QIANSHU_TEST_PG_URL=... python3 tests/test_self_heal_replay_mock.py
"""
import importlib
import json
import os
import pkgutil
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

os.environ["DATABASE_URL"] = test_database_url("selfheal_replay")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["SELF_HEAL_ENABLED"] = "true"
os.environ["SELF_HEAL_MODE"] = "approved_r3"
os.environ["SELF_HEAL_LLM_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_self_heal_indexes  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.issue import IssueCase  # noqa: E402
from app.services.self_heal import catalog, planner, validator  # noqa: E402
from app.services.self_heal.fingerprint import sha256_text  # noqa: E402

Base.metadata.create_all(engine)
migrate_self_heal_indexes(engine)

FIXTURE = Path(__file__).parent / "fixtures" / "self_heal_replay.json"
PASS, FAIL = [], []
FORBIDDEN = ("rm -rf", "drop table", "reboot", "shutdown", ".env",
             "wecom", "dingtalk", "curl ", "bash -c")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))


data = json.loads(FIXTURE.read_text(encoding="utf-8"))
check("F1 回放集非空", len(data.get("cases") or []) >= 6, str(len(data.get("cases") or [])))

db = SessionLocal()
try:
    for idx, case in enumerate(data["cases"], start=1):
        spec = dict(case["issue"])
        expected = case["expected"]
        now = datetime.now()
        issue = IssueCase(
            fingerprint=sha256_text(f"replay:{idx}:{case['name']}"),
            source=spec["source"], account_id=int(spec.get("account_id") or 0),
            run_id=int(spec.get("run_id") or 0),
            capability=spec.get("capability") or "", severity=spec["severity"],
            status=spec.get("status") or "open", title=spec["title"],
            reason_code=spec.get("reason_code") or "",
            error_class=spec.get("error_class") or "",
            summary_md=spec.get("summary_md") or "",
            source_ref_json=spec.get("source_ref_json") or "{}",
            evidence_json=spec.get("evidence_json") or "{}",
            occurrences=1, attempts=0, max_attempts=3,
            next_action="diagnose", first_seen=now, last_seen=now,
        )
        db.add(issue)
        db.commit()
        db.refresh(issue)
        # 回放只比较“会生成什么动作”，绝不在离线测试里真的执行 R0/R1。
        with patch("app.services.self_heal.executor.start_plan", return_value=True):
            row = planner.create_plan(db, issue, user_id=0, use_llm=False,
                                      reason=f"回放：{case['name']}")
        plan = json.loads(row.plan_json or "{}")
        recipes = [s.get("recipe") for s in plan.get("steps") or []]
        prefix = f"R{idx}"
        check(f"{prefix}1 风险级别与回放预期一致",
              row.risk == expected["risk"], f"{row.risk} != {expected['risk']}")
        check(f"{prefix}2 动作序列与预期一致",
              recipes == expected["recipes"], str(recipes))
        check(f"{prefix}3 人工协助标记一致",
              bool(plan.get("need_user")) == bool(expected["need_user"]),
              str(plan.get("need_user")))
        check(f"{prefix}4 计划只含目录内动作",
              all(catalog.get(name) is not None for name in recipes), str(recipes))
        check(f"{prefix}5 不含 R4/自由命令/外部通道",
              all(str(name).upper() != "R4" for name in recipes)
              and not any(word in (row.plan_json or "").lower() for word in FORBIDDEN))
        check(f"{prefix}6 计划哈希可复核",
              row.plan_hash == validator.plan_hash(plan, issue_id=issue.id,
                                                   version=row.version),
              f"{row.plan_hash} / {row.version}")
        check(f"{prefix}7 来源证据中的合成口令未进入计划",
              "synthetic-secret" not in (row.plan_json or ""))
finally:
    db.close()

print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
    sys.exit(1)
