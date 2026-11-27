"""版本化升级 SQL（update-data）执行器。

升级口径：代码包携带 `update-data/<版本>/` 目录，
后端启动时在 legacy `run_migrations` 之后按版本顺序执行尚未应用的 SQL。目标：

- **按版本同步**：目录名即语义化版本（如 `0.1.1`），只在包内的版本范围内执行；
- **只执行一次**：`schema_update_log` 记录每个版本的每个文件（含校验和）；
- **单文件事务**：SQL 与执行记录在同一事务提交，失败整体回滚并以异常中止启动，
  让安装器健康检查失败后自动回滚，不带着半套 schema 对外服务；
- **新库基线**：全新数据库由 `create_all` 建出当前完整 schema，历史版本 SQL
  只登记为 baseline、不执行（避免重复加列/重复造数）；
- **老库基线**：update-data 首次引入前的存量库（有业务表、无历史表）把
  `LEGACY_BASELINE_VERSION` 及更早版本登记为 baseline，之后的版本正常执行；
- **方言覆盖**：同名 SQL 可提供 `name.pg.sql` 方言版本，有则优先，避免在通用
  SQL 里写数据库专有语法。SQLite 已废除，`*.sqlite.sql` 一律不再执行。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("qianshu.update_data")

# update-data 首次引入前最后一个发布版本。存量库没有历史表时，<= 该版本的
# 目录视为“已由 legacy migrate 处理过”，只登记 baseline；更新的版本照常执行。
LEGACY_BASELINE_VERSION = "0.1.0"

HISTORY_TABLE = "schema_update_log"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")
_FILE_RE = re.compile(
    r"^(?P<stem>.+?)(?:\.(?P<dialect>pg|postgres|postgresql|sqlite))?\.sql$", re.I)
_DIALECT_ALIASES = {"pg": "postgresql", "postgres": "postgresql",
                    "postgresql": "postgresql"}

_HISTORY_DDL = f"""
CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
    version     VARCHAR(64)  NOT NULL,
    filename    VARCHAR(255) NOT NULL,
    checksum    VARCHAR(64)  NOT NULL DEFAULT '',
    applied_at  VARCHAR(40)  NOT NULL,
    duration_ms INTEGER      NOT NULL DEFAULT 0,
    status      VARCHAR(16)  NOT NULL DEFAULT 'applied',
    error       TEXT,
    PRIMARY KEY (version, filename)
)
"""


class UpdateDataError(RuntimeError):
    """升级 SQL 执行失败/配置不合法——启动必须中止，交给安装器回滚。"""


def update_data_dir() -> Path:
    """update-data 根目录：环境变量优先，默认项目根（backend 的上级）。"""
    env = os.environ.get("UPDATE_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):   # PyInstaller：随可执行文件旁分发
        return Path(sys.executable).resolve().parent / "update-data"
    return Path(__file__).resolve().parents[3] / "update-data"


def _version_key(name: str):
    """目录名 -> 可排序键。正式版排在预发布版之后。"""
    m = _VERSION_RE.match(name)
    if not m:
        return None
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    pre = m.group(4) or ""
    return (major, minor, patch, 1 if pre else 0, pre)


def list_versions(root: Path | None = None) -> list[tuple[str, Path]]:
    """扫描 update-data 下合法版本目录，按语义化版本升序返回 [(name, path)]。"""
    base = root or update_data_dir()
    if not base.is_dir():
        return []
    out = []
    for p in base.iterdir():
        if p.is_dir() and _version_key(p.name):
            out.append((p.name, p))
    out.sort(key=lambda x: _version_key(x[0]))
    return out


def _selected_files(version_dir: Path, dialect: str) -> list[tuple[str, Path]]:
    """选出版本目录要执行的 SQL：同名 stem 方言版优先于通用版，按 stem 排序。"""
    grouped: dict[str, dict[str, Path]] = {}
    for p in version_dir.iterdir():
        if not p.is_file():
            continue
        m = _FILE_RE.match(p.name)
        if not m:
            continue
        raw_dialect = (m.group("dialect") or "").lower()
        if raw_dialect == "sqlite":
            # SQLite 已废除：.sqlite.sql 绝不能回落成通用 SQL 在 PG 上执行
            logger.warning("update-data: 忽略 SQLite 方言文件（SQLite 已废除）: %s", p.name)
            continue
        if raw_dialect and raw_dialect not in _DIALECT_ALIASES:
            continue
        stem = m.group("stem")
        d = _DIALECT_ALIASES.get(raw_dialect, "")
        grouped.setdefault(stem, {})[d] = p
    out = []
    for stem in sorted(grouped):
        choices = grouped[stem]
        p = choices.get(dialect) or choices.get("")
        if p is not None:
            out.append((p.name, p))
    return out


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_history(conn) -> None:
    conn.exec_driver_sql(_HISTORY_DDL)


def _load_history(conn) -> dict[tuple[str, str], dict]:
    """返回 {(version, filename): {checksum, status}}。"""
    rows = conn.execute(text(
        f"SELECT version, filename, checksum, status FROM {HISTORY_TABLE}"
    )).fetchall()
    return {(str(r[0]), str(r[1])): {"checksum": str(r[2] or ""),
                                      "status": str(r[3] or "")}
            for r in rows}


def _record(conn, version: str, filename: str, checksum: str,
            duration_ms: int = 0, status: str = "applied", error: str = "") -> None:
    conn.execute(text(
        f"INSERT INTO {HISTORY_TABLE} "
        "(version, filename, checksum, applied_at, duration_ms, status, error) "
        "VALUES (:version, :filename, :checksum, :applied_at, :duration_ms, :status, :error)"
    ), {
        "version": version, "filename": filename, "checksum": checksum,
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_ms": duration_ms, "status": status, "error": error,
    })


def _execute_file(engine: Engine, sql_path: Path, version: str, filename: str,
                  digest: str) -> int:
    """SQL 与历史记录在同一个事务里执行；失败整体回滚。返回耗时毫秒。

    PostgreSQL 的 DDL 可事务化；psycopg2 支持一次 execute 多语句。
    SQL 内不要写 BEGIN/COMMIT（事务由本函数管理）。
    """
    script = sql_path.read_text(encoding="utf-8-sig")
    started = time.monotonic()
    with engine.begin() as conn:
        conn.exec_driver_sql(script)
        ms = int((time.monotonic() - started) * 1000)
        _record(conn, version, filename, digest, duration_ms=ms)
    return ms


def apply_pending_updates(engine: Engine, fresh: bool = False,
                          root: Path | None = None) -> dict:
    """执行待应用升级 SQL。返回统计 dict；任何 SQL 失败抛 UpdateDataError。

    fresh=True（数据库在 create_all 前尚不存在）：所有版本只登记 baseline。
    老库且历史表为空：<= LEGACY_BASELINE_VERSION 的版本登记 baseline，其余执行。
    """
    base = root or update_data_dir()
    versions = list_versions(base)
    stats = {"dir": str(base), "fresh": bool(fresh), "baselined": [],
             "applied": [], "skipped": [], "files": 0}
    if not versions:
        logger.info("update-data: 未发现版本目录（%s），跳过", base)
        return stats

    dialect = engine.url.get_backend_name()
    with engine.begin() as conn:
        _ensure_history(conn)
    with engine.connect() as conn:
        history = _load_history(conn)

    # 新库：create_all 已是当前完整 schema，历史版本只登记 baseline。
    if fresh:
        with engine.begin() as conn:
            for version, _p in versions:
                if (version, "__baseline__") not in history:
                    _record(conn, version, "__baseline__", "", status="baseline")
                    stats["baselined"].append(version)
        logger.info("update-data: 新库按当前 schema 建库，%d 个历史版本登记 baseline",
                    len(stats["baselined"]))
        return stats

    # 老库首次接入 update-data：把已发布的旧版本登记 baseline，避免重放。
    if not history:
        cutoff = _version_key(LEGACY_BASELINE_VERSION)
        with engine.begin() as conn:
            for version, _p in versions:
                if _version_key(version) <= cutoff:
                    _record(conn, version, "__baseline__", "", status="baseline")
                    stats["baselined"].append(version)
        if stats["baselined"]:
            logger.info("update-data: 存量库基线登记 %s", ", ".join(stats["baselined"]))
        with engine.connect() as conn:
            history = _load_history(conn)

    for version, vdir in versions:
        if (version, "__baseline__") in history:
            stats["skipped"].append(version)
            continue
        files = _selected_files(vdir, dialect)
        if not files:
            logger.info("update-data: 版本 %s 无 %s/通用 SQL，跳过", version, dialect)
            stats["skipped"].append(version)
            continue
        for filename, path in files:
            key = (version, filename)
            digest = _checksum(path)
            done = history.get(key)
            if done is not None:
                if (done["checksum"] or "") != digest:
                    logger.error("update-data: %s/%s 已执行但文件内容变更"
                                 "（校验和不一致），拒绝重复执行；请发布新版本目录修正",
                                 version, filename)
                    stats["skipped"].append(f"{version}/{filename}")
                continue
            try:
                ms = _execute_file(engine, path, version, filename, digest)
            except Exception as e:
                logger.exception("update-data: 执行失败 %s/%s", version, filename)
                raise UpdateDataError(
                    f"update-data 升级 SQL 失败：{version}/{filename}: {e}") from e
            history[key] = {"checksum": digest, "status": "applied"}
            stats["applied"].append(f"{version}/{filename}")
            stats["files"] += 1
            logger.info("update-data: 已应用 %s/%s（%d ms）", version, filename, ms)

    if stats["applied"]:
        logger.info("update-data: 本次应用 %d 个 SQL 文件", stats["files"])
    else:
        logger.info("update-data: 无待应用 SQL（已是最新）")
    return stats


def status(engine: Engine, root: Path | None = None) -> dict:
    """运维排查用：每个版本/文件是否已应用（严格只读，不建表、不改数据）。"""
    base = root or update_data_dir()
    dialect = engine.url.get_backend_name()
    history_exists = inspect(engine).has_table(HISTORY_TABLE)
    history = {}
    if history_exists:
        with engine.connect() as conn:
            history = _load_history(conn)
    out = []
    for version, vdir in list_versions(base):
        files = []
        for filename, path in _selected_files(vdir, dialect):
            entry = history.get((version, filename))
            files.append({
                "filename": filename,
                "applied": entry is not None,
                "checksum": _checksum(path),
            })
        if (version, "__baseline__") in history:
            vstatus = "baseline"
        elif files and all(f["applied"] for f in files):
            vstatus = "applied"
        else:
            vstatus = "pending"
        out.append({"version": version, "status": vstatus, "files": files})
    return {"dir": str(base), "dialect": dialect, "versions": out,
            "history_exists": history_exists}


if __name__ == "__main__":   # 运维自查：cd backend && python3 -m app.core.update_data
    import json

    from app.core.db import engine as _engine

    print(json.dumps(status(_engine), ensure_ascii=False, indent=2))
