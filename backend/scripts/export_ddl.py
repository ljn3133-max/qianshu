#!/usr/bin/env python3
"""导出 PostgreSQL DDL（供 DBA 审阅 / 自管 schema 的变更流程）。

主推建表策略 = 应用**连接自动建表**（启动时 Base.metadata.create_all + 幂等
补列/补索引迁移，见 app/core/migrate.py）；本脚本把同一套 ORM 元数据编译成
pg SQL 文本，供 DBA 评审、走工单或离线建库用——两边零漂移（同源元数据）。

用法：
    cd /opt/qianshu/backend
    python3 scripts/export_ddl.py                    # 输出到 stdout
    python3 scripts/export_ddl.py --out /tmp/qianshu_pg.sql

注意：本脚本只生成 DDL 文本，不连接任何数据库。
"""
from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_all_models() -> None:
    """注册全部模型到 Base.metadata（与 main.py 各处 import 等效的全量导入）。"""
    import app.models as models_pkg

    for m in pkgutil.iter_modules(models_pkg.__path__):
        if not m.name.startswith("_"):
            importlib.import_module(f"app.models.{m.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="输出文件（缺省 stdout）")
    args = ap.parse_args()

    _load_all_models()
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.core.db import Base
    from app.core.migrate import EXTRA_INDEXES

    dialect = postgresql.dialect()
    out = [
        f"-- 千枢 PostgreSQL DDL（scripts/export_ddl.py 从 ORM 元数据导出，{__import__('datetime').date.today()}）",
        "-- 主推建表策略是应用连接自动建表；本文件供 DBA 审阅/离线建库，与 create_all 零漂移。",
        "",
    ]
    for table in Base.metadata.sorted_tables:
        out.append(str(CreateTable(table).compile(dialect=dialect)).rstrip() + ";")
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            out.append(str(CreateIndex(idx).compile(dialect=dialect)).rstrip() + ";")
        out.append("")

    out.append("-- 启动迁移补建的查询索引（app/core/migrate.py EXTRA_INDEXES，同源引用）")
    for name, table, cols in EXTRA_INDEXES:
        out.append(f"CREATE INDEX IF NOT EXISTS {name} ON {table}{cols};")
    out.append("")

    sql = "\n".join(out)
    if args.out:
        Path(args.out).write_text(sql, encoding="utf-8")
        print(f"已写出 {args.out}（{len(Base.metadata.tables)} 表）")
    else:
        print(sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
