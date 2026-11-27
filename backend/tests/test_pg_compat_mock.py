#!/usr/bin/env python3
"""PostgreSQL 唯一数据库 · 兼容层隔离回归。

运行：QIANSHU_TEST_PG_URL=... python3 tests/test_pg_compat_mock.py
隔离性：辅助器自建一次性 PostgreSQL 库，不碰 qianshu 业务库、不连任何网络、不重启 8810。

验证项：
  A URL 规整：postgres:// 别名归一、postgresql:// 自动补驱动、显式驱动原样；
    SQLite 连接串启动即拒绝（已废除，不存在静默建空库路径）。
  B PG 引擎：create_all + run_migrations 幂等两跑无异常、补索引落齐。
  C 补列助手：旧式缺列表 -> _add_column 补列带默认值、幂等二跑 False；
    _ddl_fragment 的 pg 方言转换（DATETIME->TIMESTAMP、布尔默认 0/1->false/true）。
"""
import importlib
import os
import pkgutil
import sys
from types import SimpleNamespace

try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

os.environ["DATABASE_URL"] = test_database_url("compat")
os.environ["MASTER_KEY"] = "0" * 64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def main():
    print("== A URL 规整（不连库，纯函数） ==")
    from app.core.db import normalize_database_url

    v = normalize_database_url("postgres://u:p@h:5432/db")
    check("A postgres:// 别名归一到 postgresql+驱动", v.startswith("postgresql+"), v)
    v = normalize_database_url("postgresql://u:p@h:5432/db")
    check("A postgresql:// 自动补驱动", v.startswith("postgresql+psycopg2://")
          or v.startswith("postgresql+pg8000://"), v)
    check("A 显式 pg8000 驱动原样",
          normalize_database_url("postgresql+pg8000://u:p@h/db") == "postgresql+pg8000://u:p@h/db")
    try:
        normalize_database_url("sqlite:////tmp/x.db")
        rejected = False
    except RuntimeError as exc:
        rejected = "SQLite" in str(exc) or "sqlite" in str(exc)
    check("A SQLite 连接串启动即拒绝", rejected)

    print("== B PG 引擎与迁移 ==")
    from sqlalchemy import inspect as sa_inspect

    import app.models as models_pkg
    for _m in pkgutil.iter_modules(models_pkg.__path__):
        if not _m.name.startswith("_"):
            importlib.import_module(f"app.models.{_m.name}")   # 全量模型注册
    from app.core.db import Base, engine
    from app.core.migrate import EXTRA_INDEXES, run_migrations

    check("B engine 是 postgresql", engine.url.get_backend_name() == "postgresql")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    run_migrations(engine)   # 幂等二跑
    insp = sa_inspect(engine)
    n_idx = sum(len(insp.get_indexes(t)) for t in insp.get_table_names())
    check("B create_all+迁移两跑幂等无异常", True)
    check("B 补索引落齐", n_idx >= len(EXTRA_INDEXES), f"idx={n_idx}")

    print("== C 补列助手 ==")
    from sqlalchemy import text

    from app.core.migrate import _add_column, _cols, _ddl_fragment, _has_table

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE legacy_t (id INTEGER PRIMARY KEY, name VARCHAR(32))"))
        conn.execute(text("INSERT INTO legacy_t (id, name) VALUES (1, 'a')"))
    check("C _has_table/_cols 语义", _has_table(engine, "legacy_t")
          and _cols(engine, "legacy_t") == ["id", "name"])
    added = _add_column(engine, "legacy_t", "flag", "BOOLEAN", default="0")
    check("C 缺列补列成功", added is True)
    with engine.connect() as conn:
        val = conn.execute(text("SELECT flag FROM legacy_t WHERE id=1")).scalar()
    check("C 旧行自动填默认值", val is False, repr(val))
    check("C 幂等二跑 False", _add_column(engine, "legacy_t", "flag", "BOOLEAN") is False)
    check("C 表不存在时安全 False", _add_column(engine, "no_such_t", "x", "TEXT") is False)

    class FakeEngine:
        def __init__(self, backend):
            self.url = SimpleNamespace(get_backend_name=lambda: backend)

    t, d = _ddl_fragment(FakeEngine("postgresql"), "DATETIME", "")
    check("C pg DATETIME->TIMESTAMP", t == "TIMESTAMP", t)
    t, d = _ddl_fragment(FakeEngine("postgresql"), "BOOLEAN NOT NULL", "0")
    check("C pg 布尔默认 0->false", d == "false" and t == "BOOLEAN NOT NULL", f"{t}/{d}")
    t, d = _ddl_fragment(FakeEngine("postgresql"), "BOOLEAN", "1")
    check("C pg 布尔默认 1->true", d == "true", d)
    t, d = _ddl_fragment(FakeEngine("postgresql"), "TEXT", "''")
    check("C pg 文本默认值原样", t == "TEXT" and d == "''")

    print()
    if FAILED:
        print(f"兼容层回归: 未过 {len(FAILED)} 项: {FAILED}")
        return 1
    print("兼容层回归: 全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
