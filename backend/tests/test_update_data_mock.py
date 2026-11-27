#!/usr/bin/env python3
"""update-data 版本化升级 SQL 执行器回归测试（PostgreSQL）。

覆盖：新库基线、存量库基线、按版本顺序执行、幂等重跑、方言选择（pg 优先、
sqlite 文件不执行）、校验和拦截、单文件事务回滚、失败中止后重试。
全部使用一次性 PostgreSQL 测试库（QIANSHU_TEST_PG_URL），不碰真实数据。

运行：cd /opt/qianshu/backend && QIANSHU_TEST_PG_URL=... python3 tests/test_update_data_mock.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 独立脚本与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.core.update_data import (  # noqa: E402
    UpdateDataError, apply_pending_updates, list_versions, status)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def write_version(root, version, files):
    d = os.path.join(root, version)
    os.makedirs(d, exist_ok=True)
    for name, sql in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(sql)


def rows(engine, sql, params=None):
    with engine.connect() as c:
        return c.execute(text(sql), params or {}).fetchall()


def tables(engine):
    return set(inspect(engine).get_table_names())


def main():
    tmp = tempfile.mkdtemp(prefix="qianshu-update-data-")
    root = os.path.join(tmp, "update-data")
    os.makedirs(root)

    # ---- 1. 新库：所有版本只登记 baseline，绝不执行 SQL ----
    write_version(root, "0.1.0", {"001_should_not_run.sql":
                                  "INSERT INTO no_such_table VALUES (1);"})
    write_version(root, "0.1.1", {"001_should_not_run.sql":
                                  "INSERT INTO no_such_table VALUES (1);"})
    e1 = create_engine(test_database_url("udfresh"))
    r = apply_pending_updates(e1, fresh=True, root=Path(root))
    check("新库不执行历史 SQL", r["applied"] == [] and r["baselined"] == ["0.1.0", "0.1.1"], r)
    hist = rows(e1, "SELECT version, filename, status FROM schema_update_log "
                    "ORDER BY version")
    check("新库登记 baseline 记录", len(hist) == 2 and all(x[1] == "__baseline__"
                                                       and x[2] == "baseline" for x in hist), hist)

    # ---- 2. 存量库首次接入：<=0.1.0 基线，0.1.1 执行 ----
    e2 = create_engine(test_database_url("udlegacy"))
    with e2.begin() as c:
        c.exec_driver_sql("CREATE TABLE assets (id INTEGER PRIMARY KEY)")
    root = os.path.join(tmp, "update-data-legacy")
    os.makedirs(root)
    write_version(root, "0.1.0", {"001_should_not_run.sql":
                                  "INSERT INTO no_such_table VALUES (1);"})
    write_version(root, "0.1.1", {"001_create_demo.sql": """
CREATE TABLE demo (id SERIAL PRIMARY KEY, name VARCHAR(32));
INSERT INTO demo (name) VALUES ('first');
"""})
    r = apply_pending_updates(e2, fresh=False, root=Path(root))
    check("存量库基线 <=0.1.0", r["baselined"] == ["0.1.0"], r)
    check("存量库应用 0.1.1", r["applied"] == ["0.1.1/001_create_demo.sql"], r)
    check("SQL 实际生效", rows(e2, "SELECT name FROM demo") == [("first",)])

    # ---- 3. 幂等重跑 ----
    r = apply_pending_updates(e2, fresh=False, root=Path(root))
    check("重跑不再执行", r["applied"] == [] and r["files"] == 0, r)
    check("重跑不重复插数据", rows(e2, "SELECT count(*) FROM demo")[0][0] == 1)

    # ---- 4. 已执行文件被改动：校验和不一致，拒绝重跑 ----
    with open(os.path.join(root, "0.1.1", "001_create_demo.sql"), "a",
              encoding="utf-8") as f:
        f.write("\nINSERT INTO demo (name) VALUES ('changed');\n")
    r = apply_pending_updates(e2, fresh=False, root=Path(root))
    check("校验和拦截已执行文件", "0.1.1/001_create_demo.sql" in r["skipped"], r)
    check("校验和不一致不重复插数据",
          rows(e2, "SELECT count(*) FROM demo")[0][0] == 1)

    # ---- 5. 方言选择：pg 专版优先；sqlite 专版绝不执行 ----
    write_version(root, "0.1.2", {
        "001_dialect.sql": "INSERT INTO demo (name) VALUES ('generic');",
        "001_dialect.pg.sql": "INSERT INTO demo (name) VALUES ('pg');",
        "001_dialect.sqlite.sql": "INSERT INTO demo (name) VALUES ('sqlite');",
    })
    r = apply_pending_updates(e2, fresh=False, root=Path(root))
    names = [x[0] for x in rows(
        e2, "SELECT name FROM demo WHERE name IN ('generic','pg','sqlite') ORDER BY name")]
    check("pg 方言文件优先执行", names == ["pg"], names)
    check("方言目录只记一条", r["applied"] == ["0.1.2/001_dialect.pg.sql"], r)

    # ---- 6. 单文件事务：DDL+DML 失败整体回滚，后续文件不执行 ----
    write_version(root, "0.1.3", {
        "001_ok.sql": "CREATE TABLE tx_demo (id INTEGER);",
        "002_bad.sql": """
CREATE TABLE tx_bad (id INTEGER);
INSERT INTO tx_bad VALUES (1);
INSERT INTO no_such_table VALUES (2);
""",
        "003_after.sql": "CREATE TABLE tx_after (id INTEGER);",
    })
    try:
        apply_pending_updates(e2, fresh=False, root=Path(root))
        raised = False
    except UpdateDataError:
        raised = True
    check("失败文件抛 UpdateDataError", raised)
    t = tables(e2)
    check("前序文件已提交", "tx_demo" in t)
    check("失败文件 DDL/DML 整体回滚", "tx_bad" not in t)
    check("失败后不继续执行后续文件", "tx_after" not in t)
    hist = rows(e2, "SELECT filename FROM schema_update_log WHERE version='0.1.3'")
    check("失败文件不写历史", [x[0] for x in hist] == ["001_ok.sql"], hist)

    # ---- 7. 修复后重试：跳过已成功文件，只补失败文件 ----
    with open(os.path.join(root, "0.1.3", "002_bad.sql"), "w", encoding="utf-8") as f:
        f.write("INSERT INTO tx_demo VALUES (1);\n")
    r = apply_pending_updates(e2, fresh=False, root=Path(root))
    check("修复后只补失败文件",
          r["applied"] == ["0.1.3/002_bad.sql", "0.1.3/003_after.sql"], r)
    check("修复后数据正确", rows(e2, "SELECT count(*) FROM tx_demo")[0][0] == 1)

    # ---- 8. 版本目录数字排序（0.1.2 < 0.1.10，不是字符串排序）----
    write_version(root, "0.1.10", {"001_noop.sql": "SELECT 1;"})
    write_version(root, "0.1.9", {"001_noop.sql": "SELECT 1;"})
    order = [v for v, _p in list_versions(Path(root))]
    check("版本按数字排序", order == ["0.1.0", "0.1.1", "0.1.2", "0.1.3",
                                      "0.1.9", "0.1.10"], order)
    check("非版本目录忽略", "_template" not in order)

    # ---- 9. 只读状态（不建历史表）----
    st = status(e2, root=Path(root))
    v11 = [v for v in st["versions"] if v["version"] == "0.1.1"][0]
    v110 = [v for v in st["versions"] if v["version"] == "0.1.10"][0]
    check("状态显示已应用版本", v11["status"] == "applied", v11)
    check("状态显示待应用版本", v110["status"] == "pending", v110)
    e4 = create_engine(test_database_url("udreadonly"))
    st4 = status(e4, root=Path(root))
    check("只读状态不报历史表存在", st4["history_exists"] is False)
    check("只读状态未建历史表", "schema_update_log" not in tables(e4))

    ok = all(x[1] for x in RESULTS)
    print(f"\n{sum(1 for x in RESULTS if x[1])}/{len(RESULTS)} 通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
