#!/usr/bin/env python3
"""env_store 双写隔离 mock 测试：.env + env_kv 双写、库优先读、重建不丢。

运行：cd /opt/qianshu/backend && python3 tests/test_env_store_dual_mock.py
隔离性：DATABASE_URL=一次性 PostgreSQL、env_store.ENV_PATH=临时 .env、临时 BACKUP 目录；
不碰真实 qianshu.db / backend/.env，不重启任何服务，不碰 admin。

验证项：
  A 双写：set_keys 同时落 .env 与 env_kv 表（行数/值正确）
  B 幂等：同值二写不增行；改值行更新
  C 库优先：.env 删除（=容器重建）后 get_key 仍返回库值
  D 密钥拒写：SECRET/MASTER_KEY/TOKEN 类键只落 .env 不入库
  E 启动对账：.env-only 键入册（存量收编）；库值回填 .env；冲突按"谁后改谁生效"
    （.env 较新→手改入册，手改不静默失效）；二跑零动作
  F 库故障不丢 .env：库表缺失时 set_keys 仍写 .env 且如实报 False；表恢复后对账补库
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-envstore-")
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


def main():
    import importlib

    import app.models as models_pkg
    import pkgutil

    for _m in pkgutil.iter_modules(models_pkg.__path__):
        if not _m.name.startswith("_"):
            importlib.import_module(f"app.models.{_m.name}")
    from app.core.db import Base, SessionLocal, engine
    from app.models.env_kv import EnvKv

    Base.metadata.create_all(bind=engine)

    import app.services.env_store as env_store

    env_store.ENV_PATH = os.path.join(_TMPDIR, ".env")
    env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}

    def db_rows():
        db = SessionLocal()
        try:
            return {k: v for k, v in db.query(EnvKv.key, EnvKv.value).all()}
        finally:
            db.close()

    # ---------- A 双写 ----------
    print("== A 双写 ==")
    ok = env_store.set_keys({"AAA_KEY_OK": "v1", "PRODUCT_CARDS": '[["x","X","Y","#fff"]]'})
    check("A set_keys 返回 True", ok is True)
    env_txt = open(env_store.ENV_PATH).read()
    check("A .env 落盘", "AAA_KEY_OK=v1" in env_txt and "PRODUCT_CARDS=" in env_txt)
    rows = db_rows()
    check("A env_kv 落库", rows.get("AAA_KEY_OK") == "v1" and rows.get("PRODUCT_CARDS", "").startswith("[["),
          str(rows))

    # ---------- B 幂等 ----------
    print("== B 幂等 ==")
    env_store.set_keys({"AAA_KEY_OK": "v1"})
    rows = db_rows()
    check("B 同值二写不增行", rows.get("AAA_KEY_OK") == "v1" and
          sum(1 for k in rows if k == "AAA_KEY_OK") == 1, str(rows))
    env_store.set_keys({"AAA_KEY_OK": "v2"})
    check("B 改值行更新", db_rows().get("AAA_KEY_OK") == "v2")

    # ---------- C 库优先（.env 丢=容器重建） ----------
    print("== C 库优先 ==")
    os.remove(env_store.ENV_PATH)
    env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}
    env_store._db_data(force=True)
    check("C .env 丢失后 get_key 仍返回库值", env_store.get_key("AAA_KEY_OK") == "v2")

    # ---------- D 密钥拒写 ----------
    print("== D 密钥拒写 ==")
    env_store.set_keys({"SECRET_KEY": "s", "MASTER_KEY": "m", "COPILOTD_BRIDGE_TOKEN": "t",
                        "NORMAL_FLAG": "1"})
    rows = db_rows()
    check("D 密钥不入库", all(k not in rows for k in ("SECRET_KEY", "MASTER_KEY", "COPILOTD_BRIDGE_TOKEN")),
          str(rows))
    env_txt = open(env_store.ENV_PATH).read()
    check("D 密钥仍落 .env", "SECRET_KEY=s" in env_txt and "COPILOTD_BRIDGE_TOKEN=t" in env_txt)
    check("D 非密钥照常入库", rows.get("NORMAL_FLAG") == "1")

    # ---------- E 启动对账 ----------
    print("== E 启动对账 ==")
    # E1 入册：手工/存量 .env 键收编入库
    with open(env_store.ENV_PATH, "a") as f:
        f.write("LEGACY_ONLY=old\nSECRET_KEY=manual-edit\n")
    env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}
    r = env_store.sync_with_db()
    rows = db_rows()
    check("E1 存量 .env 键入册", rows.get("LEGACY_ONLY") == "old" and r["seeded"] >= 1, str(r))
    check("E1 密钥不入册", "SECRET_KEY" not in rows, str(rows))
    # E2 回填：.env 丢后对账恢复（值=库）
    os.remove(env_store.ENV_PATH)
    env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}
    r = env_store.sync_with_db()
    env_txt = open(env_store.ENV_PATH).read()
    check("E2 库值回填 .env", "AAA_KEY_OK=v2" in env_txt and r["restored"] >= 1, f"{r} {env_txt!r}")
    # E3 冲突按"谁后改谁生效"：.env 较新 → 手改入册（手改不丢，留痕）
    time.sleep(0.05)
    with open(env_store.ENV_PATH, "a") as f:
        f.write("AAA_KEY_OK=manual-edit\n")
    env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}
    r = env_store.sync_with_db()
    check("E3 手改入册优先（.env 较新）", env_store.get_key("AAA_KEY_OK") == "manual-edit" and
          "AAA_KEY_OK=manual-edit" in open(env_store.ENV_PATH).read() and r["adopted"] >= 1, str(r))
    # E4 二跑零动作（幂等）
    env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}
    r = env_store.sync_with_db()
    check("E4 二跑零动作", r["seeded"] == 0 and r["restored"] == 0, str(r))

    # ---------- F 库故障不丢 .env ----------
    print("== F 库故障 ==")
    EnvKv.__table__.drop(bind=engine)
    ok = env_store.set_keys({"BBB_KEY_OK": "v3"})
    check("F 库故障如实报 False", ok is False)
    check("F .env 照常持久", "BBB_KEY_OK=v3" in open(env_store.ENV_PATH).read())
    r = env_store.sync_with_db()
    check("F 对账跳过不炸", r["skipped"].startswith("db_unavailable"), str(r))
    Base.metadata.create_all(bind=engine)   # 表恢复
    env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}
    r = env_store.sync_with_db()
    check("F 表恢复后对账补库", db_rows().get("BBB_KEY_OK") == "v3" and r["seeded"] >= 1, str(r))

    # ---------- G 备份轮转（限制含密钥 .env.bak 累积） ----------
    print("== G 备份轮转 ==")
    import glob
    d = os.path.dirname(env_store.ENV_PATH)
    prefix = os.path.basename(env_store.ENV_PATH) + ".bak-"
    for f in glob.glob(env_store.ENV_PATH + ".bak-*"):
        os.remove(f)
    for i in range(1, 9):
        open(os.path.join(d, f"{prefix}2020010100000{i}"), "w").write("X=1\n")
    env_store.set_keys({"AAA_KEY_OK": "v4"})
    left = sorted(os.path.basename(f) for f in glob.glob(env_store.ENV_PATH + ".bak-*"))
    check("G 只保留最近 5 份备份", len(left) == 5, str(left))
    check("G 删除最旧 4 份", not any(n.endswith(f"0000{i}") for i in range(1, 5) for n in left), str(left))
    check("G 本次写前备份保留", any(n.endswith("000008") for n in left), str(left))

    print()
    if FAILED:
        print(f"FAIL ({len(FAILED)}): {FAILED}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
