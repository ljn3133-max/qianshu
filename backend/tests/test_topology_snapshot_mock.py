#!/usr/bin/env python3
"""拓扑统一 Provider 全量扫描 + 完整架构快照 mock 回归。

全部使用一次性 PostgreSQL 和 mock，不访问真实云、SLS 或本机业务库。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

TMPDIR = tempfile.mkdtemp(prefix="qianshu-topo-snapshot-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (pathlib.Path(__file__).resolve().parent.parent / "app" / "models").glob("*.py"):
    if _p.stem != "__init__":
        importlib.import_module(f"app.models.{_p.stem}")

from app.core.db import Base, SessionLocal, engine, get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.topology_scan import TopologyScan  # noqa: E402
from app.services.topology import run_scan, scan_snapshot  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  <-- {detail}"))


def _get(app, url):
    import httpx

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            return await client.get(url)
    return asyncio.run(_go())


def migration_check():
    """旧 schema → 002 补列 → 重复执行幂等，全部在另一个临时库。"""
    from sqlalchemy import create_engine, inspect, text
    from app.core.update_data import apply_pending_updates

    root = pathlib.Path(TMPDIR) / "update-data"
    target = root / "0.3.1"
    target.mkdir(parents=True)
    source = pathlib.Path(__file__).resolve().parents[2] / "update-data" / "0.3.1"
    shutil.copy(source / "002_topology_scan_snapshot.pg.sql", target)
    old_db = os.path.join(TMPDIR, "old-schema.db")
    eng = create_engine(test_database_url("topoold"))
    with eng.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE assets (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("""CREATE TABLE topology_scans (
            id INTEGER PRIMARY KEY, scanned_at TIMESTAMP, trigger VARCHAR(16),
            groups INTEGER, links INTEGER, members INTEGER, duration_ms INTEGER,
            diff_json TEXT, synced_json TEXT)""")
    result = apply_pending_updates(eng, fresh=False, root=root)
    check("旧库升级应用 002 快照列",
          any(c["name"] == "arch_json" for c in inspect(eng).get_columns("topology_scans")),
          result)
    with eng.connect() as conn:
        default = conn.execute(text("SELECT arch_json FROM topology_scans LIMIT 1")).fetchone()
    check("旧表无行时读取默认值不报错", default is None)
    again = apply_pending_updates(eng, fresh=False, root=root)
    check("快照迁移重复执行零文件", again["files"] == 0, again)
    eng.dispose()


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ali = CloudAccount(name="mock-ali", vendor="aliyun", enabled=True)
        tc = CloudAccount(name="mock-tc", vendor="tencent", enabled=True)
        db.add_all([ali, tc])
        db.flush()
        db.add_all([
            Asset(account_id=ali.id, product="ecs", instance_id="i-1", name="web",
                  status="running", extra="{}"),
            Asset(account_id=tc.id, product="tc_mongodb", instance_id="m-1", name="mongo",
                  status="running", extra="{}"),
        ])
        db.commit()
        provider_out = {
            "results": [{"account_id": tc.id, "account": tc.name, "vendor": "tencent",
                         "product": "tc_mongodb", "total": 1}],
            "summary": {"accounts": 2, "failed_accounts": [], "errors": []},
        }
        with patch("app.services.cloud.sync_job.sync_accounts", return_value=provider_out) as unified, \
                patch("app.services.link_draft.generate_link_draft", return_value={"created": 0}), \
                patch("app.collector.ecs.sync_ecs", side_effect=AssertionError("old ECS path")) as old_ecs, \
                patch("app.collector.slb.sync_slb", side_effect=AssertionError("old SLB path")) as old_slb, \
                patch("app.collector.rds.sync_rds", side_effect=AssertionError("old RDS path")) as old_rds, \
                patch("app.collector.redis.sync_redis", side_effect=AssertionError("old Redis path")) as old_redis:
            result = run_scan(db, trigger="test", full=True)
        check("full=True 调用统一 Provider 同步", unified.call_count == 1
              and unified.call_args.kwargs.get("force") is True
              and "account_ids" not in unified.call_args.kwargs, unified.call_args)
        check("full=True 不再调用四个旧阿里采集器",
              not any(x.called for x in (old_ecs, old_slb, old_rds, old_redis)))
        check("统一同步结果写入扫描摘要",
              result["synced"]["provider_sync"]["summary"]["accounts"] == 2)

        row = db.get(TopologyScan, result["scan_id"])
        saved = json.loads(row.arch_json)
        check("扫描保存完整 groups/members/links",
              bool(saved["groups"]) and "members" in saved["groups"][0]
              and isinstance(saved["links"], list))
        check("快照含腾讯 tc_mongodb",
              any(m["meta"].get("product") == "tc_mongodb"
                  for g in saved["groups"] for m in g["members"]))
        snap = scan_snapshot(db)
        check("最新快照带扫描 ID 与时间",
              snap["scan_id"] == row.id and snap["scanned_at"] and snap["arch"] == saved)
        check("指定 ID 可读取同一历史快照", scan_snapshot(db, row.id)["arch"] == saved)
        check("不存在的指定 ID 返回 None", scan_snapshot(db, 999999) is None)

        # 快照必须冻结：后续资产变化不得回写历史记录。
        db.add(Asset(account_id=tc.id, product="cvm", instance_id="new-after-scan",
                     name="new-after-scan", status="running", extra="{}"))
        db.commit()
        frozen = json.dumps(scan_snapshot(db, row.id)["arch"], ensure_ascii=False)
        check("历史快照不随实时资产变化", "new-after-scan" not in frozen)

        from app.main import app
        stub = SimpleNamespace(id=1, username="tester", role="admin", is_active=True)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: stub
        response = _get(app, f"/api/topology/scan/snapshot?scan_id={row.id}")
        check("快照 API 返回完整架构", response.status_code == 200
              and response.json()["arch"]["groups"], response.status_code)
        missing = _get(app, "/api/topology/scan/snapshot?scan_id=999999")
        check("指定不存在快照 API 返回 404", missing.status_code == 404, missing.status_code)

        old = TopologyScan(scanned_at=datetime.now(), trigger="legacy", groups=0,
                           links=0, members=0, duration_ms=0, diff_json="{}",
                           synced_json="{}", arch_json="")
        db.add(old)
        db.commit()
        legacy = _get(app, "/api/topology/scan/snapshot")
        check("老记录无快照明确 arch=null 且不冒充实时图", legacy.status_code == 200
              and legacy.json()["arch"] is None and "没有完整架构快照" in legacy.json()["note"])
    finally:
        db.close()
    migration_check()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
