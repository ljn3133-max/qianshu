"""数据库引擎与会话（PostgreSQL）。

DATABASE_URL 必须是 PostgreSQL（SQLite 已废除，config 启动时即校验）。
驱动选型：psycopg2-binary 自带 libpq 的 manylinux 轮子，离线 vendor/ 一把装、
SQLAlchemy 生态一等公民；pg8000 纯 Python 作缺驱动时的兜底（URL 显式
postgresql+pg8000:// 也可直连）。
"""
import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

logger = logging.getLogger("qianshu.db")


def normalize_database_url(url: str) -> str:
    """URL 规整（不改语义，只补兼容）：

    - 非 PostgreSQL 连接串直接拒绝（SQLite 已废除，禁止静默建空库）；
    - postgres:// 历史别名 -> postgresql://（SQLAlchemy 1.4+ 只认后者）；
    - 无驱动后缀时自动选驱动：psycopg2 优先、pg8000 兜底。
    """
    url = (url or "").strip()
    if not url.startswith(("postgresql://", "postgresql+", "postgres://")):
        raise RuntimeError(
            "DATABASE_URL 必须是 PostgreSQL 连接串（SQLite 已废除）："
            f"当前协议 {url.split(':', 1)[0] or '空'}")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        for driver in ("psycopg2", "pg8000"):
            try:
                __import__(driver)
                return f"postgresql+{driver}://" + url[len("postgresql://"):]
            except ImportError:
                continue
        raise RuntimeError(
            "DATABASE_URL 指向 PostgreSQL 但缺驱动：pip install psycopg2-binary（或 pg8000）")
    return url


_DATABASE_URL = normalize_database_url(settings.DATABASE_URL)

engine = create_engine(
    _DATABASE_URL,
    pool_pre_ping=True,
    # 多 worker × 24 后台循环共享，池按进程独立
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_timeout=settings.DB_POOL_TIMEOUT,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
