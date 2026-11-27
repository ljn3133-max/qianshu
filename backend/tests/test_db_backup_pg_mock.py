#!/usr/bin/env python3
"""db_backup PostgreSQL 隔离 mock 测试：pg_dump 封装 + 分派 + 轮转 + 幂等。

运行：QIANSHU_TEST_PG_URL=... python3 tests/test_db_backup_pg_mock.py
隔离性：一次性 PostgreSQL 测试库 + 临时 BACKUP_DIR；pg_dump 调用全部 mock
（subprocess/which 假桩，不碰 qianshu-pg、不产生真实 dump）；不重启任何服务、不碰 admin。

验证项：
  A 分派：backup_db 固定走 PostgreSQL（SQLite 已废除，不再有 .db.gz 热备分支）
  B pg_dump 封装：PATH 形态带 -h/-p/-U/-Fc + PGPASSWORD；容器 exec 形态免口令
  C 兜底：无 pg_dump 无容器 -> skipped 带 hint；pg_dump 非 0 -> error（不留半截档）
  D 轮转：保留最新 N 份，旧档先删（与 pg-backup.sh"保留 14 份"同口径）
  E 幂等：当日已有备份 -> already_backed_up_today 跳过（与 cron/手工双跑不重打）
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-dbbackup-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("t")
os.environ["MASTER_KEY"] = "0" * 64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class FakeProc:
    def __init__(self, rc=0, stderr=b""):
        self.returncode = rc
        self.stderr = stderr


def main():
    import app.services.db_backup as db_backup

    out_dir = Path(_TMPDIR) / "backups"
    # 假的 pg_dump 可执行文件（which 返回它；实际调用被 subprocess 假桩拦截）
    fake_pg_dump = Path(_TMPDIR) / "pg_dump"
    fake_pg_dump.write_text("#!/bin/sh\n")
    fake_pg_dump.chmod(0o755)

    def set_settings(**kw):
        base = dict(DATABASE_URL=os.environ["DATABASE_URL"],
                    BACKUP_DIR=str(out_dir), BACKUP_KEEP_DAYS=14, PG_DUMP_CONTAINER="")
        base.update(kw)
        db_backup.settings = SimpleNamespace(**base)

    def clean_today():
        """场景隔离：删当日档（幂等跳过按'当日已有备份'判，场景间必须清）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        for p in out_dir.glob("qianshu-pg-*.dump"):
            if datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") == today:
                p.unlink()

    # ---------- A 分派 ----------
    print("== A 分派 ==")
    set_settings()
    calls = []

    def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None, capture_output=False, text=False):
        calls.append(list(cmd))
        if cmd and cmd[-1] == "--version":   # _pg_dump_major 探版本
            return SimpleNamespace(stdout="pg_dump (PostgreSQL) 16.15\n", returncode=0)
        if capture_output:            # `podman ps --format`
            return SimpleNamespace(stdout="qianshu-pg\nother\n", returncode=0)
        stdout.write(b"DUMPDATA")
        return FakeProc(0)

    db_backup.shutil.which = lambda n: str(fake_pg_dump) if n == "pg_dump" else None
    db_backup.subprocess.run = fake_run
    r = db_backup.backup_db()
    check("A backup_db 固定走 pg_dump", r.get("engine") == "pg" and "file" in r, str(r))
    check("A 不再产生 SQLite .db.gz 热备", not list(out_dir.glob("*.db.gz")))

    # ---------- B/C pg_dump 封装 + 兜底 ----------
    print("== B/C pg_dump 封装与兜底 ==")
    calls = []

    def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None, capture_output=False, text=False):
        calls.append(list(cmd))
        if cmd and cmd[-1] == "--version":   # _pg_dump_major 探版本
            return SimpleNamespace(stdout="pg_dump (PostgreSQL) 16.15\n", returncode=0)
        if capture_output:            # `podman ps --format`
            return SimpleNamespace(stdout="qianshu-pg\nother\n", returncode=0)
        stdout.write(b"DUMPDATA")
        return FakeProc(0)

    # B1 PATH 形态
    clean_today()
    db_backup.shutil.which = lambda n: str(fake_pg_dump) if n == "pg_dump" else None
    db_backup.subprocess.run = fake_run
    r = db_backup.backup_pg()
    check("B1 PATH pg_dump 成功", r.get("engine") == "pg" and r.get("via") == "pg_dump", str(r))
    pgcmd = next((c for c in calls if c and c[0].endswith("pg_dump") and "-Fc" in c), None)
    check("B1 命令带 -Fc 与测试库名",
          bool(pgcmd and "-Fc" in pgcmd and pgcmd[-1].startswith("qianshu_test_")), str(pgcmd))
    check("B1 -h/-p/-U 齐全", bool(pgcmd and "-h" in pgcmd and "-p" in pgcmd and "-U" in pgcmd), str(pgcmd))

    # B2 容器 exec 形态（无 PATH pg_dump）
    clean_today()
    calls.clear()
    db_backup.shutil.which = lambda n: "/usr/bin/" + n if n == "podman" else None
    r = db_backup.backup_pg()
    check("B2 容器 exec 成功", r.get("engine") == "pg" and r.get("via") == "container-exec", str(r))
    execcmd = next((c for c in calls if "exec" in c), None)
    check("B2 exec 命令 podman exec qianshu-pg pg_dump",
          bool(execcmd and execcmd[0].endswith("podman") and "qianshu-pg" in execcmd), str(execcmd))

    # C1 全不可用
    clean_today()
    db_backup.shutil.which = lambda n: None
    r = db_backup.backup_pg()
    check("C1 无 pg_dump 无容器 -> skipped+hint",
          r.get("skipped") == "pg_dump_unavailable" and "hint" in r, str(r))

    # C2 pg_dump 非 0（不留半截档）
    clean_today()
    calls.clear()
    db_backup.shutil.which = lambda n: str(fake_pg_dump) if n == "pg_dump" else None

    def failing_run(cmd, stdout=None, stderr=None, env=None, timeout=None, capture_output=False, text=False):
        if cmd and cmd[-1] == "--version":
            return SimpleNamespace(stdout="pg_dump (PostgreSQL) 16.15\n", returncode=0)
        stdout.write(b"HALF")
        return FakeProc(2, b"err: boom")

    db_backup.subprocess.run = failing_run
    r = db_backup.backup_pg()
    tmps = list(out_dir.glob("*.dump.tmp"))
    check("C2 失败报 error 且无半截档", r.get("error") == "pg_dump_failed" and not tmps, str(r))

    # ---------- D 轮转 ----------
    print("== D 轮转（保留最新 N 份）==")
    clean_today()
    db_backup.subprocess.run = fake_run
    for d in range(20):
        f = out_dir / f"qianshu-pg-202610{d:02d}-000000.dump"
        f.write_bytes(b"x")
        os.utime(f, (1700000000 + d * 86400, 1700000000 + d * 86400))   # 旧 mtime
    r = db_backup.backup_pg()
    left = sorted(out_dir.glob("qianshu-pg-*.dump"))
    check("D 轮转后保留 14 份", len(left) == 14, f"left={len(left)} r={r}")
    check("D 旧档先删", not (out_dir / "qianshu-pg-20261000-000000.dump").exists(),
          str([p.name for p in left]))

    # ---------- E 幂等（当日已有备份） ----------
    print("== E 幂等 ==")
    clean_today()
    r1 = db_backup.backup_pg()
    r2 = db_backup.backup_pg()
    check("E 一跑成功", "file" in r1, f"r1={r1}")
    check("E 二跑跳过 already_backed_up_today",
          r2.get("skipped") == "already_backed_up_today", f"r2={r2}")
    same_day = [p for p in out_dir.glob("qianshu-pg-*.dump")
                if datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") == datetime.now().strftime("%Y-%m-%d")]
    check("E 当日只留一份", len(same_day) == 1, f"{[p.name for p in same_day]}")

    print()
    if FAILED:
        print(f"FAIL ({len(FAILED)}): {FAILED}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
