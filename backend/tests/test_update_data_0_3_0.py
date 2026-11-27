#!/usr/bin/env python3
"""update-data/0.3.0 真实迁移回归测试（PostgreSQL）。

在一次性 PostgreSQL 测试库上按 **0.2.0 的真实列集合**造旧库，然后执行仓库里真实的
`update-data/0.3.0/` SQL，校验：列/默认值、索引、种子行、成本异动账号回填、
历史记录、幂等重跑、方言纯度（SQLite 方言文件已废除）。

不碰任何真实数据库、不读 .env、不打印凭证。

运行：cd /opt/qianshu/backend && QIANSHU_TEST_PG_URL=... python3 tests/test_update_data_0_3_0.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.core.update_data import apply_pending_updates  # noqa: E402

try:  # 独立脚本与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

VERSION = "0.3.0"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


# 0.2.0 的列集合（从 0.2.0 快照 create_all 实测导出，只保留迁移涉及的表与列）
LEGACY_DDL = """
CREATE TABLE cloud_accounts (
    id INTEGER PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    vendor VARCHAR(16) NOT NULL,
    ak_cipher TEXT NOT NULL,
    secret_cipher TEXT NOT NULL,
    region VARCHAR(64) NOT NULL,
    extra_json TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    status VARCHAR(16) NOT NULL,
    last_test_at DATETIME,
    alert_seeded_at DATETIME,
    last_sync_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE TABLE alert_records (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    rule_id INTEGER NOT NULL,
    rule_name VARCHAR(128) NOT NULL,
    source VARCHAR(8) NOT NULL,
    target VARCHAR(192) NOT NULL,
    target_name VARCHAR(192) NOT NULL,
    level VARCHAR(16) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    breach_value FLOAT,
    fired_at DATETIME NOT NULL,
    last_eval_at DATETIME NOT NULL,
    recovered_at DATETIME,
    impact_level VARCHAR(16) NOT NULL,
    impact_desc TEXT NOT NULL,
    notified_channels VARCHAR(512) NOT NULL,
    silenced BOOLEAN NOT NULL,
    silence_reason VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE TABLE cost_anomalies (
    id INTEGER PRIMARY KEY,
    detect_date DATE NOT NULL,
    type VARCHAR(32) NOT NULL,
    level VARCHAR(16) NOT NULL,
    target_key VARCHAR(255) NOT NULL,
    target_name VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    value FLOAT NOT NULL,
    baseline FLOAT NOT NULL,
    ratio FLOAT NOT NULL,
    created_at DATETIME NOT NULL,
    last_seen DATE,
    ack_status VARCHAR(16) NOT NULL DEFAULT 'open',
    ack_note TEXT NOT NULL DEFAULT '',
    ack_by VARCHAR(64) NOT NULL DEFAULT '',
    ack_by_id INTEGER NOT NULL DEFAULT 0,
    ack_at DATETIME
);
CREATE TABLE expiry_ignores (
    id INTEGER PRIMARY KEY,
    target_key VARCHAR(191) NOT NULL,
    note TEXT NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_by_id INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
);
CREATE TABLE assets (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    product VARCHAR(16) NOT NULL,
    instance_id VARCHAR(128) NOT NULL,
    name VARCHAR(255) NOT NULL,
    region VARCHAR(32) NOT NULL,
    zone VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    tier VARCHAR(16) NOT NULL,
    instance_type VARCHAR(64) NOT NULL,
    charge_type VARCHAR(32) NOT NULL,
    expire_time DATETIME,
    tags TEXT,
    extra TEXT,
    raw_meta TEXT,
    synced_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
"""

LEGACY_ROWS = """
INSERT INTO cloud_accounts (id, name, vendor, ak_cipher, secret_cipher, region,
    extra_json, enabled, status, created_at, updated_at)
VALUES (1, '生产阿里云', 'aliyun', 'x', 'y', 'cn-hangzhou', '{$}', $T, 'ok',
        '2026-10-01 00:00:00', '2026-10-01 00:00:00');
INSERT INTO alert_records (id, account_id, rule_id, rule_name, source, target,
    target_name, level, title, content, status, fired_at, last_eval_at,
    impact_level, impact_desc, notified_channels, silenced, silence_reason, created_at)
VALUES (1, 1, 10, 'CPU 使用率', 'metric', 'i-abc', 'vm-a', 'critical', 't', 'c',
        'firing', '2026-11-10 00:00:00', '2026-11-10 00:00:00', 'high', '',
        '', $F, '', '2026-11-10 00:00:00');
INSERT INTO cost_anomalies (id, detect_date, type, level, target_key, target_name,
    title, content, value, baseline, ratio, created_at)
VALUES (1, '2026-11-01', 'cost_spike', 'warning', 'i-abc', 'vm-a', 't', 'c',
        10.0, 2.0, 5.0, '2026-11-01 00:00:00');
INSERT INTO cost_anomalies (id, detect_date, type, level, target_key, target_name,
    title, content, value, baseline, ratio, created_at)
VALUES (2, '2026-11-01', 'cost_spike', 'warning', '__total__', '总账', 't', 'c',
        99.0, 50.0, 2.0, '2026-11-01 00:00:00');
INSERT INTO expiry_ignores (id, target_key, note, created_by, created_at)
VALUES (1, 'cert:example.com', '旧忽略', 'admin', '2026-10-01 00:00:00');
INSERT INTO assets (id, account_id, product, instance_id, name, region, zone,
    status, tier, instance_type, charge_type, created_at, updated_at)
VALUES (1, 7, 'ecs', 'i-abc', 'vm-a', 'cn-hangzhou', 'a', 'Running', 'core',
        'ecs.g6', 'PrePaid', '2026-10-01 00:00:00', '2026-10-01 00:00:00');
"""


def rows(engine, sql):
    with engine.connect() as c:
        return c.execute(text(sql)).fetchall()


def cols(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def main():
    repo_update_data = Path(__file__).resolve().parents[2] / "update-data"
    src = repo_update_data / VERSION
    check("0.3.0 SQL 目录存在", src.is_dir(), str(src))

    # ---- 方言纯度（不需要数据库）：只允许 pg 专版或通用 SQL ----
    sql_files = sorted(p.name for p in src.glob("*.sql"))
    check("不再携带 SQLite 方言文件",
          not any(n.endswith(".sqlite.sql") for n in sql_files), str(sql_files))
    for name in sql_files:
        body = (src / name).read_text(encoding="utf-8")
        if name.endswith(".pg.sql"):
            bad = [t for t in ("DATETIME", "INSERT OR IGNORE", "DEFAULT 1")
                   if t in body]
            check(f"{name} 无 SQLite 专有语法", not bad, str(bad))

    tmp = tempfile.mkdtemp(prefix="qianshu-ud030-")
    try:
        # 只放 0.3.0，避免受仓库里后续版本影响
        root = Path(tmp) / "update-data"
        root.mkdir()
        shutil.copytree(src, root / VERSION)

        engine = create_engine(test_database_url("ud030"))
        with engine.begin() as c:
            c.exec_driver_sql(LEGACY_DDL.replace("DATETIME", "TIMESTAMP"))
            c.exec_driver_sql(LEGACY_ROWS.replace("$T", "true")
                              .replace("$F", "false").replace("{$}", "{}"))

        check("迁移前无 priority 列", "priority" not in cols(engine, "cloud_accounts"))
        check("迁移前无 monitor_module_settings 表",
              not inspect(engine).has_table("monitor_module_settings"))

        # ---- 执行真实 0.3.0 ----
        r = apply_pending_updates(engine, fresh=False, root=root)
        check("6 个 SQL 全部应用", r["files"] == 6, r)

        # ---- cloud_accounts ----
        ca = cols(engine, "cloud_accounts")
        for col in ("priority", "notify_overview", "notify_inapp", "notify_push",
                    "repeat_days", "last_metrics_at", "last_alert_eval_at",
                    "last_host_check_at", "last_cost_at"):
            check(f"cloud_accounts.{col} 已加列", col in ca)
        row = rows(engine, "SELECT priority, notify_overview, notify_inapp, "
                           "notify_push, repeat_days FROM cloud_accounts WHERE id=1")[0]
        check("存量账号默认 high 且三去向全开、重复继承",
              tuple(row) == ("high", True, True, True, None), str(tuple(row)))

        # ---- alert_records ----
        ar = cols(engine, "alert_records")
        for col in ("module", "ack_status", "ack_note", "ack_by", "ack_by_id",
                    "ack_at", "last_notified_at", "notify_count", "next_notify_at"):
            check(f"alert_records.{col} 已加列", col in ar)
        row = rows(engine, "SELECT module, ack_status, ack_note, ack_by, "
                           "ack_by_id, notify_count FROM alert_records WHERE id=1")[0]
        check("存量告警默认归 alert 模块且未确认",
              tuple(row) == ("alert", "none", "", "", 0, 0), str(tuple(row)))
        idx = {r[0] for r in rows(engine,
                                 "SELECT indexname FROM pg_indexes WHERE tablename IN "
                                 "('alert_records','cost_anomalies')")}
        check("next_notify_at 索引已建",
              {"ix_alert_records_next_notify_at", "ix_cost_anomalies_next_notify_at"} <= idx,
              str(idx))

        # ---- cost_anomalies 回填 ----
        got = dict(rows(engine, "SELECT target_key, account_id FROM cost_anomalies"))
        check("成本异动按实例 ID 回填账号", got.get("i-abc") == 7, str(got))
        check("全局异动保持 account_id=0", got.get("__total__") == 0, str(got))
        rc = rows(engine, "SELECT notify_count, next_notify_at, last_notified_at "
                          "FROM cost_anomalies WHERE id=1")[0]
        check("存量异动通知状态零值", tuple(rc) == (0, None, None), str(tuple(rc)))

        # ---- expiry_ignores ----
        check("expiry_ignores.until_at 已加列且历史忽略保持永久",
              "until_at" in cols(engine, "expiry_ignores") and
              rows(engine, "SELECT until_at FROM expiry_ignores WHERE id=1")[0][0] is None)

        # ---- 模块种子 ----
        mods = rows(engine, "SELECT module, enabled, overview, inapp, push "
                            "FROM monitor_module_settings")
        check("模块开关种子 12 行", len(mods) == 12, str(sorted(r[0] for r in mods)))
        check("模块种子默认全开",
              all(all(v is True for v in r[1:]) for r in mods), str(mods))

        # ---- 历史与幂等 ----
        hist = rows(engine, "SELECT version, filename, status FROM schema_update_log "
                            "ORDER BY filename")
        check("历史记录 6 条 0.3.0 applied",
              len(hist) == 6 and all(h[0] == VERSION and h[2] == "applied" for h in hist),
              str(hist))
        r2 = apply_pending_updates(engine, fresh=False, root=root)
        check("重复启动不重放", r2["files"] == 0 and r2["applied"] == [], r2)
        check("重复执行后种子仍 12 行",
              rows(engine, "SELECT COUNT(*) FROM monitor_module_settings")[0][0] == 12)

        # ---- 事务性：坏 SQL 必须整体回滚 ----
        root_bad = Path(tmp) / "bad"
        root_bad.mkdir()
        bad_dir = root_bad / VERSION
        bad_dir.mkdir()
        (bad_dir / "001_ok.sql").write_text(
            "CREATE TABLE probe_tx (id INTEGER PRIMARY KEY);\n", encoding="utf-8")
        (bad_dir / "002_bad.sql").write_text(
            "CREATE TABLE probe_tx2 (id INTEGER);\n"
            "ALTER TABLE no_such_table ADD COLUMN x INTEGER;\n", encoding="utf-8")
        e2 = create_engine(test_database_url("ud030tx"))
        with e2.begin() as c:
            c.exec_driver_sql("CREATE TABLE assets (id INTEGER PRIMARY KEY)")
        try:
            apply_pending_updates(e2, fresh=False, root=root_bad)
            failed = False
        except Exception:
            failed = True
        check("坏 SQL 中止启动", failed)
        check("坏文件整体回滚（probe_tx2 未建）",
              not inspect(e2).has_table("probe_tx2"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("失败：" + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
