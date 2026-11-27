"""测试专用 PostgreSQL 库工厂（SQLite 已废除，测试不再有 sqlite 兜底）。

用法（必须在 `import app.*` 之前调用，DATABASE_URL 在导入时读取）::

    os.environ["DATABASE_URL"] = test_database_url("mycase")

环境变量 ``QIANSHU_TEST_PG_URL`` 指向一台**可丢弃**的 PostgreSQL 管理连接
（必须是 PostgreSQL；库名随意，只用于 CREATE/DROP DATABASE，不会读写该库的业务
表）。每次调用创建一个唯一命名的新库 ``qianshu_test_<tag>_<pid>_<hex>``，
进程退出时自动 DROP，测试之间互不干扰、可并行跑。

未设置该变量时直接报错退出（不让测试静默跑空）。本机一次性环境::

    docker run --rm -d --name qianshu-testpg -p 127.0.0.1:55432:5432 \\
      -e POSTGRES_USER=qianshutest -e POSTGRES_PASSWORD=qianshutestpw \\
      -e POSTGRES_DB=postgres postgres:16
    QIANSHU_TEST_PG_URL=postgresql://qianshutest:qianshutestpw@127.0.0.1:55432/postgres \\
      python3 tests/test_xxx_mock.py

CI 由 postgres:16 service 提供同样的管理连接。
"""
from __future__ import annotations

import atexit
import os
import re
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

_CREATED = []
_COUNTER = 0

_HELP = (
    "测试只支持真实 PostgreSQL（SQLite 已废除）。\n"
    "请设置 QIANSHU_TEST_PG_URL 指向一台可丢弃的 PostgreSQL 管理连接，例如：\n"
    "  QIANSHU_TEST_PG_URL=postgresql://user:pass@127.0.0.1:55432/postgres\n"
    "本机一次性实例：\n"
    "  docker run --rm -d --name qianshu-testpg -p 127.0.0.1:55432:5432 \\\n"
    "    -e POSTGRES_USER=qianshutest -e POSTGRES_PASSWORD=qianshutestpw \\\n"
    "    -e POSTGRES_DB=postgres postgres:16"
)


def _admin_url() -> str:
    url = os.environ.get("QIANSHU_TEST_PG_URL", "").strip()
    if not url:
        raise RuntimeError(_HELP)
    scheme = url.split("://", 1)[0].lower()
    if not scheme.startswith(("postgresql", "postgres")):
        raise RuntimeError(
            "QIANSHU_TEST_PG_URL 必须是 PostgreSQL 连接串（SQLite 已废除）："
            f"当前协议 {scheme}")
    return url


def _psycopg2_url(url: str) -> str:
    """去掉 SQLAlchemy 驱动后缀（postgresql+psycopg2:// -> postgresql://）。"""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    if scheme == "postgres":
        scheme = "postgresql"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _slug(tag: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (tag or "case").lower()).strip("_")
    return (s or "case")[:20]


def _drop(name: str) -> None:
    import psycopg2

    try:
        conn = psycopg2.connect(_psycopg2_url(_admin_url()), connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid<>pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.close()
    except Exception as exc:  # 清理失败不掩盖测试结果，只提示残留库名
        print(f"WARN: 测试库 {name} 清理失败：{exc}", file=sys.stderr)


def _dispose_engines() -> None:
    """先归还连接池再 DROP DATABASE。

    进程退出时引擎池由解释器稍后回收；若先 DROP 库，池内连接回滚会刷
    "server closed the connection unexpectedly" 噪声日志。这里主动 dispose。
    """
    import gc

    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, close_all_sessions

    # 单个 Session 处于半关闭事务时 close_all_sessions() 会整体抛错，逐个兜底
    try:
        close_all_sessions()
    except Exception:
        pass
    for obj in gc.get_objects():
        try:
            if isinstance(obj, Session):
                obj.close()
        except Exception:
            pass

    n = 0
    for obj in gc.get_objects():
        try:
            if isinstance(obj, Engine):
                obj.dispose()
                n += 1
        except Exception:
            pass
    if os.environ.get("PG_TEST_DEBUG"):
        print(f"DEBUG dispose engines: {n}", file=sys.stderr)


@atexit.register
def _drop_all() -> None:
    if not _CREATED:
        return
    # 先给测试自建的非 daemon 线程一点收尾时间（如 task_engine），
    # 否则 DROP 库会让仍在提交的线程打印错误；最多等 3 秒，不阻塞退出。
    import threading
    import time

    deadline = time.time() + 3
    for t in threading.enumerate():
        if t is threading.current_thread() or t.daemon:
            continue
        t.join(max(0.0, deadline - time.time()))
    try:
        _dispose_engines()
    except Exception as exc:  # 清理失败也必须继续 DROP，避免临时库堆积
        print(f"WARN: 测试引擎释放失败：{exc}", file=sys.stderr)
    while _CREATED:
        _drop(_CREATED.pop())


def test_database_url(tag: str = "case") -> str:
    """新建一个唯一命名的空 PostgreSQL 库，返回可用作 DATABASE_URL 的连接串。

    进程退出时自动清理；同一个进程可多次调用，每次得到独立的新库。
    """
    global _COUNTER

    import psycopg2

    admin = _psycopg2_url(_admin_url())
    _COUNTER += 1
    name = "qianshu_test_%s_%d_%d_%s" % (
        _slug(tag), os.getpid(), _COUNTER, uuid.uuid4().hex[:6])
    try:
        conn = psycopg2.connect(admin, connect_timeout=10)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        conn.close()
    except Exception as exc:
        raise RuntimeError(
            f"无法创建测试库 {name}（{exc}）。QIANSHU_TEST_PG_URL 的账号需要 "
            "CREATE DATABASE 权限，且服务端可连接。") from exc
    _CREATED.append(name)

    parts = urlsplit(admin)
    scheme = "postgresql"
    return urlunsplit((scheme, parts.netloc, "/" + name, parts.query, ""))


def reset_pk_sequences(engine) -> None:
    """把表 id 序列校准到 MAX(id)。

    PG 的 SERIAL 序列不会因显式 id 插入而前进（SQLite AUTOINCREMENT 会自动推进）。
    测试造数时显式指定 id 后，若后续还要 ORM 自增插入，先调用本函数。
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name='id' "
            "AND column_default LIKE 'nextval%'")).fetchall()
        for (t,) in rows:
            conn.exec_driver_sql(
                f'SELECT setval(pg_get_serial_sequence(\'"{t}"\', \'id\'), '
                f'COALESCE((SELECT MAX(id) FROM "{t}"), 1), '
                f'(SELECT MAX(id) FROM "{t}") IS NOT NULL)')


def drop_test_database(url: str) -> None:
    """立即清理某个 test_database_url() 返回的库（正常由 atexit 兜底）。"""
    name = urlsplit(url).path.lstrip("/")
    if name in _CREATED:
        _CREATED.remove(name)
        _drop(name)
