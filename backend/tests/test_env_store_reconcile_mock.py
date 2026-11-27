#!/usr/bin/env python3
"""配置写入路径单一化隔离 mock 测试：手改不静默失效 + 冲突解决语义 + 文档口径。

运行：cd /opt/qianshu/backend && python3 tests/test_env_store_reconcile_mock.py
隔离性：DATABASE_URL=一次性 PostgreSQL、env_store.ENV_PATH=临时 .env、chdir 临时目录（防
Settings 读到真 backend/.env）；不碰真实 qianshu.db / backend/.env，不重启任何服务，
不碰 admin，不向任何通知渠道发消息（留痕只落 in_app_messages）。

验证项：
  A 手改入册（.env 较新）：手改 .env → sync（=重启对账）后值生效（get_key/库/文件三面一致）
    + 站内信留痕「检测到手改 X 已入册」
  B 被库值覆盖（库较新）：.env 旧值被库值回填 + 留痕「被库值覆盖」
  C 幂等：二跑零动作（seeded/restored/adopted/env_seeded 全 0）
  D 密钥拒写名单不动：SECRET_KEY 手改不入册、不被回填
  E 环境变量注入收编：非空注入未入册键 → 入册（手改/装机引导不丢）+ 留痕
  F 环境变量注入双轨提示：已入册键异值 → 不动值 + 留痕「不一致」
  G 空注入让位库值：显式空串注入不盖库值（compose `${VAR:-}` 惯用法）；拒写名单键不受影响
  H 文档口径 grep 断言：install.sh/部署架构.md/.env.example/集成中心/向导文案已统一口径
  I CLI 真跑：python3 -m app.services.env_store（文档给的手动入册命令）真执行真落库
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-reconcile-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("t")
os.environ["MASTER_KEY"] = "0" * 64
os.chdir(_TMPDIR)   # Settings 的 env_file=".env" 读 cwd/.env——必须隔离到临时目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def main():
    import importlib
    import pkgutil

    import app.models as models_pkg

    for _m in pkgutil.iter_modules(models_pkg.__path__):
        if not _m.name.startswith("_"):
            importlib.import_module(f"app.models.{_m.name}")
    from app.core.db import Base, SessionLocal, engine
    from app.models.env_kv import EnvKv
    from app.models.in_app_message import InAppMessage

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

    def notices():
        db = SessionLocal()
        try:
            return "\n".join(m.content_md or "" for m in db.query(InAppMessage).all())
        finally:
            db.close()

    def reset():
        env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}
        env_store._db_data(force=True)

    # ---------- A 手改入册（.env 较新，手改不丢） ----------
    print("== A 手改入册（.env 较新） ==")
    env_store.set_keys({"KK": "v1"})
    time.sleep(0.05)
    with open(env_store.ENV_PATH, "a") as f:
        f.write("KK=manual-new\n")     # 手改 .env（重复行——解析取末行）
    reset()
    r = env_store.sync_with_db()
    check("A 手改值生效（get_key 库优先读=手改值）", env_store.get_key("KK") == "manual-new", str(r))
    check("A 库已入册手改值", db_rows().get("KK") == "manual-new", str(db_rows()))
    check("A 文件保留手改值", "KK=manual-new" in open(env_store.ENV_PATH).read())
    check("A 留痕含「检测到手改…已入册」",
          "检测到手改 KK" in notices() and "已入册" in notices(), notices()[:200])

    # ---------- B 被库值覆盖（库较新） ----------
    print("== B 被库值覆盖（库较新） ==")
    env_store.set_keys({"KK2": "file-old"})
    time.sleep(0.05)
    db = SessionLocal()
    try:
        row = db.get(EnvKv, "KK2")
        row.value = "db-new"          # 直接改库（=库侧较新写入，onupdate 刷 updated_at）
        db.commit()
    finally:
        db.close()
    past = time.time() - 1000
    os.utime(env_store.ENV_PATH, (past, past))   # .env 老化成旧面
    reset()
    r = env_store.sync_with_db()
    check("B 库值回填 .env", "KK2=db-new" in open(env_store.ENV_PATH).read() and
          r["restored"] >= 1, str(r))
    check("B 手改被覆盖留痕", "手改 KK2" in notices() and "被库值" in notices() and
          "覆盖" in notices(), notices()[-300:])
    check("B 生效值=库值", env_store.get_key("KK2") == "db-new")

    # ---------- C 幂等二跑零动作 ----------
    print("== C 幂等 ==")
    reset()
    r = env_store.sync_with_db()
    check("C 二跑零动作", r["seeded"] == 0 and r["restored"] == 0 and r["adopted"] == 0
          and r["env_seeded"] == 0, str(r))

    # ---------- D 密钥拒写名单不动 ----------
    print("== D 密钥拒写名单不动 ==")
    with open(env_store.ENV_PATH, "a") as f:
        f.write("SECRET_KEY=manual-secret-edit\n")
    reset()
    env_store.sync_with_db()
    check("D 密钥不入册", "SECRET_KEY" not in db_rows(), str(db_rows()))
    check("D 密钥手改值保留（不回填不覆盖）",
          "SECRET_KEY=manual-secret-edit" in open(env_store.ENV_PATH).read())

    # ---------- E 环境变量注入收编（未入册） ----------
    print("== E 环境变量注入收编 ==")
    os.environ["SLS_APP_LOG"] = '["p1","l1"]'
    reset()
    r = env_store.sync_with_db()
    check("E 注入值入册", db_rows().get("SLS_APP_LOG") == '["p1","l1"]' and r["env_seeded"] >= 1, str(r))
    check("E 注入值同步落文件", "SLS_APP_LOG=" in open(env_store.ENV_PATH).read())
    check("E 收编留痕", "环境变量注入" in notices() and "SLS_APP_LOG" in notices())
    del os.environ["SLS_APP_LOG"]

    # ---------- F 环境变量注入双轨提示（已入册异值不动值） ----------
    print("== F 环境变量注入双轨提示 ==")
    env_store.set_keys({"SLS_SLB_TARGETS": '[["a","b","outer"]]'})
    os.environ["SLS_SLB_TARGETS"] = '[["x","y","outer"]]'
    reset()
    r = env_store.sync_with_db()
    check("F 值不动（库为准）", db_rows().get("SLS_SLB_TARGETS") == '[["a","b","outer"]]' and
          env_store.get_key("SLS_SLB_TARGETS") == '[["a","b","outer"]]', str(r))
    check("F 双轨留痕「不一致」", r["env_conflicts"] >= 1 and "不一致" in notices(), str(r))
    del os.environ["SLS_SLB_TARGETS"]

    # ---------- G 空注入让位库值（compose `${VAR:-}` 惯用法） ----------
    print("== G 空注入让位库值 ==")
    env_store.set_keys({"SLS_CDN_TARGET": '["p2","l2"]'})
    os.environ["SLS_CDN_TARGET"] = ""
    from app.core.config import Settings, _overlay_db_over_empty_env

    inst = Settings()
    check("G 现状：空注入盖掉库/文件值", getattr(inst, "SLS_CDN_TARGET", "") == "")
    _overlay_db_over_empty_env(inst)
    check("G 空注入让位库值", getattr(inst, "SLS_CDN_TARGET") == '["p2","l2"]',
          repr(getattr(inst, "SLS_CDN_TARGET", "")))
    os.environ["INIT_ADMIN_PASSWORD"] = ""
    inst2 = Settings()
    _overlay_db_over_empty_env(inst2)
    check("G 拒写名单键（admin 密码）不受影响", getattr(inst2, "INIT_ADMIN_PASSWORD", "") == "")
    del os.environ["SLS_CDN_TARGET"]
    del os.environ["INIT_ADMIN_PASSWORD"]

    # ---------- H 文档口径 grep 断言 ----------
    print("== H 文档口径 ==")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    install = open(os.path.join(root, "install.sh")).read()
    check("H install.sh 不再教「也可手工填写」", "也可手工填写" not in install)
    check("H install.sh 指向 UI 入口+入册命令",
          "安装向导/系统设置" in install and "python3 -m app.services.env_store" in install)
    doc = open(os.path.join(root, "docs", "active", "deployment.md")).read()
    check("H 部署架构旧口径已删", "同名键会在下次启动被库值覆盖" not in doc)
    check("H 部署架构新口径在", "唯一入口" in doc and "谁后改谁生效" in doc and
          "python3 -m app.services.env_store" in doc)
    src = open(os.path.join(root, "backend", "app", "services", "env_store.py")).read()
    check("H env_store 口径更新", "会在下次启动被库值覆盖" not in src and "唯一入口" in src)
    itg = open(os.path.join(root, "backend", "app", "api", "v1", "integrations.py")).read()
    check("H 集成中心 SLS 教程口径", "勿手改 .env" in itg)
    ex = open(os.path.join(root, ".env.example")).read()
    check("H .env.example 口径", "入册" in ex and "唯一入口" in ex)
    zh = open(os.path.join(root, "frontend", "src", "i18n", "locales", "zh-CN", "bootstrap.js")).read()
    check("H 向导文案不再指写 .env", "并写入 .env" not in zh and "写入 .env 的 METRIC" not in zh)

    # ---------- I CLI 真跑（文档给的手动入册命令） ----------
    print("== I CLI 真跑 ==")
    with open(env_store.ENV_PATH, "a") as f:
        f.write("CLI_ONLY=cli-val\n")
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items()
           if k not in ("SLS_APP_LOG", "SLS_SLB_TARGETS", "SLS_CDN_TARGET", "INIT_ADMIN_PASSWORD")}
    env["PYTHONPATH"] = backend_dir
    p = subprocess.run([sys.executable, "-m", "app.services.env_store"],
                       cwd=_TMPDIR, env=env, capture_output=True, text=True, timeout=60)
    check("I CLI 执行成功", p.returncode == 0, p.stderr[-300:])
    check("I CLI 入册手改键", db_rows().get("CLI_ONLY") == "cli-val",
          p.stdout[-300:] + p.stderr[-300:])

    print()
    if FAILED:
        print(f"FAIL ({len(FAILED)}): {FAILED}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
