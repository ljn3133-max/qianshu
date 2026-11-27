"""数据库每日备份。

画像/基线/案例/业务映射/节假日表全在业务库里——这些是系统的"记忆"，
没有任何备份等于记忆随时可能清零（磁盘故障/误删/坏块）。

入口 backup_db() 固定走 PostgreSQL（services/db_backup.py 接线到每日调度，
app/main.py）：pg_dump -Fc 封装。pg_dump 两级兜底：PATH 上的 pg_dump（镜像内置
postgresql-client，源码形态用 /usr/pgsql-15/bin）-> podman/docker exec 进 pg 容器
（宿主机+容器库形态，与 deploy/bin/pg-backup.sh 同法）；都不可用时如实报 skipped，
可用宿主机 cron 跑 pg-backup.sh 兜底（命名/保留口径一致，互不冲突）。
输出目录 BACKUP_DIR（默认 /opt/qianshu/backups；compose 形态配 /backups 卷）。
与 deploy/bin/pg-backup.sh 的口径：同一目录、同名前缀 qianshu-pg-*、保留 14 天——
两套调度并存时各留一份 dump，滚动清理互认，不会互删当日档。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("qianshu.db_backup")

_DEFAULT_DIR = "/opt/qianshu/backups"
# pg 容器名自动探测序（PG_DUMP_CONTAINER 可显式指定）
_PG_CONTAINER_CANDIDATES = ("qianshu-pg", "qianshu-postgres", "postgres")


def _backup_dir() -> Path:
    return Path(getattr(settings, "BACKUP_DIR", "") or _DEFAULT_DIR)


def _rotate(paths: list, keep: int) -> int:
    """滚动保留最新 keep 份（文档口径"保留 14 份"；旧档先删）。返回删除数。"""
    removed = 0
    for p in sorted(paths, key=lambda x: x.stat().st_mtime):
        if len(paths) - removed <= keep:
            break
        p.unlink(missing_ok=True)
        removed += 1
    return removed


def _pg_dump_major(exe: str) -> int | None:
    """pg_dump 主版本号（"pg_dump (PostgreSQL) 16.4 ..." -> 16）。"""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+)\.", out.stdout or "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _server_major() -> int | None:
    """数据库服务端主版本；查询失败返回 None（不影响后续兜底判断）。"""
    try:
        from app.core.db import engine

        with engine.connect() as conn:
            num = conn.exec_driver_sql("SHOW server_version_num").scalar()
        return int(num) // 10000
    except Exception:
        return None


def _find_pg_dump(server_major: int | None) -> str | None:
    """定位与服务端主版本一致的 pg_dump：PATH -> PGDG 的 /usr/pgsql-*/bin。

    pg_dump 主版本低于服务端会直接拒绝导出，因此已知服务端版本时只接受同主版本。
    """
    candidates = []
    found = shutil.which("pg_dump")
    if found:
        candidates.append(found)
    candidates.extend(str(p) for p in sorted(
        Path("/usr").glob("pgsql-*/bin/pg_dump"), reverse=True))
    fallback = None
    for exe in dict.fromkeys(candidates):
        if not os.access(exe, os.X_OK):
            continue
        if _pg_dump_major(exe) == server_major:
            return exe
        fallback = fallback or exe
    return None if server_major else fallback


def _pg_dump_cmd() -> tuple[list, dict, str] | None:
    """定位 pg_dump 执行方式：(命令, 环境, 方式) 或 None=不可用。

    ① PATH / PGDG 目录中的 pg_dump（裸机/容器客户端形态）：带 -h/-p/-U + PGPASSWORD；
    ② podman/docker exec 进 pg 容器（容器库形态，与 pg-backup.sh 同法：
       容器内 local socket 信任，免口令、版本与服务端一致）。
    """
    from sqlalchemy.engine import make_url

    u = make_url(settings.DATABASE_URL)
    user, db = u.username or "qianshu", u.database or "qianshu"
    exe = _find_pg_dump(_server_major())
    if exe:
        cmd = [exe, "-h", u.host or "127.0.0.1", "-p", str(u.port or 5432),
               "-U", user, "-Fc", db]
        env = dict(os.environ)
        if u.password:
            env["PGPASSWORD"] = u.password
        return cmd, env, "pg_dump"
    names = ([settings.PG_DUMP_CONTAINER] if getattr(settings, "PG_DUMP_CONTAINER", "")
             else list(_PG_CONTAINER_CANDIDATES))
    for engine in ("podman", "docker"):
        if not shutil.which(engine):
            continue
        try:
            out = subprocess.run([engine, "ps", "--format", "{{.Names}}"],
                                 capture_output=True, text=True, timeout=10)
            running = set(out.stdout.split())
        except Exception:
            continue
        for name in names:
            if name in running:
                return ([engine, "exec", name, "pg_dump", "-U", user, "-Fc", db],
                        dict(os.environ), "container-exec")
    return None


def backup_pg(keep_days: int | None = None) -> dict:
    """备份 DATABASE_URL 指向的 PostgreSQL（pg_dump -Fc 封装）。

    幂等：当日已有完整备份（本调度或 pg-backup.sh cron 任一所致）
    即跳过——两套调度并存不双跑 pg_dump、不双份占盘；要强制重打用 pg-backup.sh。
    """
    keep_days = int(keep_days or getattr(settings, "BACKUP_KEEP_DAYS", 14))
    out_dir = _backup_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    for existing in sorted(out_dir.glob("qianshu-pg-*.dump")):
        if datetime.fromtimestamp(existing.stat().st_mtime).strftime("%Y-%m-%d") == today:
            return {"skipped": "already_backed_up_today", "file": existing.name}
    located = _pg_dump_cmd()
    if located is None:
        return {"skipped": "pg_dump_unavailable",
                "hint": "无与数据库主版本一致的 pg_dump，且无 pg 容器可 exec；"
                        "源码形态安装 PGDG postgresql15，compose 形态由镜像内置客户端"}
    cmd, env, via = located

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = out_dir / f"qianshu-pg-{stamp}.dump.tmp"
    dump = out_dir / f"qianshu-pg-{stamp}.dump"
    try:
        with open(tmp, "wb") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env, timeout=1800)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            err = proc.stderr.decode("utf-8", "replace")[:200]
            logger.warning("pg_dump 失败（rc=%d）: %s", proc.returncode, err)
            return {"error": "pg_dump_failed", "detail": err}
        os.replace(tmp, dump)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        logger.warning("pg_dump 异常: %s", str(e)[:200])
        return {"error": "pg_dump_failed", "detail": str(e)[:200]}
    size_mb = round(os.path.getsize(dump) / 1048576, 2)

    removed = _rotate(sorted(out_dir.glob("qianshu-pg-*.dump")), keep_days)
    logger.info("pg 备份完成: %s（%sMB），清理 %d 份旧档", dump.name, size_mb, removed)
    return {"file": dump.name, "size_mb": size_mb, "removed": removed, "engine": "pg",
            "via": via}


def backup_db(keep_days: int | None = None) -> dict:
    """每日调度入口：PostgreSQL pg_dump（SQLite 已废除）。"""
    return backup_pg(keep_days)
