#!/usr/bin/env python3
"""PostgreSQL 隔离端到端冒烟：建表、迁移、CRUD、并发写和数据搬运。

用法：
    QIANSHU_PG_URL=postgresql://postgres:PASS@127.0.0.1:5433/qianshu_e2e \
    QIANSHU_PG_E2E_CONFIRM=qianshu_e2e \
        python3 tests/test_pg_e2e_smoke.py

目标必须是本机空库，库名为 qianshu_e2e 或 qianshu_e2e_*；确认变量必须与库名
完全一致。首次运行写入所有权标记，之后只允许复用带相同标记的库。脚本会清空
模型表，不设置 QIANSHU_PG_URL 时跳过；任何校验失败都在连接或写入前退出。

验证项：
  A 连接与建表：create_all + run_migrations 幂等两跑；表/索引落齐。
  B 核心 CRUD 与类型往返：User（布尔/时间）、Asset、AlertRecord（浮点/时间）。
  C pg 专属补列：BOOLEAN DEFAULT 0/1、DATETIME->TIMESTAMP 的方言转换在真 pg 生效。
  D link_edges 整表重建迁移：旧行保留+新列默认+索引+序列校准（新插入不撞主键）。
  E ILIKE 口径：用户关键词搜索大小写不敏感（PG ILIKE）。
  F 并发写冒烟：8 线程混写 audit_logs，无死锁、行数一致。
  H 初始管理员：空 users 自动建号 + must_change_password 强制改密标志。
"""
import importlib
import os
import pkgutil
import re
import sys
import threading
from datetime import datetime
from urllib.parse import unquote, urlsplit

_PG_URL = os.environ.get("QIANSHU_PG_URL", "").strip()
if not _PG_URL:
    print("SKIP: 未设 QIANSHU_PG_URL（无 pg 环境），真实端到端见 docs/active/deployment.md §10")
    sys.exit(0)

try:
    _PG_PARSED = urlsplit(_PG_URL)
    _PG_HOST = _PG_PARSED.hostname or ""
    _PG_DB = unquote(_PG_PARSED.path.lstrip("/"))
except ValueError as exc:
    raise SystemExit(f"拒绝执行：QIANSHU_PG_URL 无法解析（{exc}）")
if _PG_PARSED.scheme not in {"postgresql", "postgres"} or "/" not in _PG_PARSED.path:
    raise SystemExit("拒绝执行：QIANSHU_PG_URL 必须指向 PostgreSQL 的具体数据库")
if _PG_HOST not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("拒绝执行：清库测试只连接本机 PostgreSQL")
if not re.fullmatch(r"qianshu_e2e(?:_[a-z0-9_]+)?", _PG_DB):
    raise SystemExit("拒绝执行：目标库名必须为 qianshu_e2e 或 qianshu_e2e_*")
if os.environ.get("QIANSHU_PG_E2E_CONFIRM", "") != _PG_DB:
    raise SystemExit("拒绝执行：QIANSHU_PG_E2E_CONFIRM 必须与目标库名完全一致")

os.environ["DATABASE_URL"] = _PG_URL
os.environ["MASTER_KEY"] = "0" * 64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def _claim_empty_scratch_db(engine, inspect, text):
    """只允许首次空库或本脚本已标记的库；标记表不属于模型，drop_all 不会删除。"""
    marker = "_qianshu_pg_e2e_owner"
    tables = set(inspect(engine).get_table_names())
    if not tables:
        with engine.begin() as conn:
            conn.execute(text(
                f'CREATE TABLE "{marker}" (database_name VARCHAR(128) PRIMARY KEY)'))
            conn.execute(text(f'INSERT INTO "{marker}" VALUES (:name)'), {"name": _PG_DB})
        return
    if marker not in tables:
        raise RuntimeError("拒绝执行：目标库不是空库，且没有本测试的所有权标记")
    with engine.connect() as conn:
        owner = conn.execute(text(f'SELECT database_name FROM "{marker}"')).scalar()
    if owner != _PG_DB:
        raise RuntimeError("拒绝执行：目标库的所有权标记与当前库名不一致")


def main():
    import app.models as models_pkg
    for _m in pkgutil.iter_modules(models_pkg.__path__):
        if not _m.name.startswith("_"):
            importlib.import_module(f"app.models.{_m.name}")
    from sqlalchemy import inspect as sa_inspect, text

    from app.core.db import Base, SessionLocal, engine
    from app.core.migrate import (migrate_link_edges_rule_dim, run_migrations,
                                  _add_column, _cols)

    check("0 连接 PostgreSQL", engine.url.get_backend_name() == "postgresql")

    print("== A 建表与迁移（幂等） ==")
    _claim_empty_scratch_db(engine, sa_inspect, text)
    Base.metadata.drop_all(bind=engine)   # 所有权校验后，只清理本测试的模型表
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    run_migrations(engine)
    insp = sa_inspect(engine)
    check("A 全部模型表建齐", set(Base.metadata.tables) <= set(insp.get_table_names()),
          str(set(Base.metadata.tables) - set(insp.get_table_names())))

    print("== B 核心 CRUD 与类型往返 ==")
    from app.models.alert_record import AlertRecord
    from app.models.asset import Asset
    from app.models.user import User

    db = SessionLocal()
    u = User(username="pg_e2e_user", password_hash="x", display_name="PG", role="admin",
             is_active=True, must_change_password=False, otp_enabled=False)
    a = Asset(product="ecs", instance_id="i-pg-e2e", name="WebServer", region="cn-test",
              status="Running")
    r = AlertRecord(rule_id=42, rule_name="pg-e2e", source="metric", target="i-pg-e2e",
                    level="warning", title="t", status="firing", breach_value=91.5,
                    fired_at=datetime.now())
    db.add_all([u, a, r])
    db.commit()
    u2, a2, r2 = db.get(User, u.id), db.get(Asset, a.id), db.get(AlertRecord, r.id)
    check("B 行写入并读回", u2 is not None and a2 is not None and r2 is not None)
    check("B 布尔真值往返", u2.is_active is True and u2.must_change_password is False)
    check("B 时间/浮点往返",
          isinstance(r2.fired_at, datetime) and abs(r2.breach_value - 91.5) < 1e-6,
          f"{r2.fired_at}/{r2.breach_value}")
    db.close()

    print("== C pg 专属补列方言转换 ==")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS legacy_pg_t"))
        conn.execute(text(
            "CREATE TABLE legacy_pg_t (id SERIAL PRIMARY KEY, name VARCHAR(32))"))
        conn.execute(text("INSERT INTO legacy_pg_t (name) VALUES ('a')"))
    check("C BOOLEAN DEFAULT 0 -> false", _add_column(engine, "legacy_pg_t", "flag",
                                                      "BOOLEAN NOT NULL", default="0"))
    check("C DATETIME -> TIMESTAMP", _add_column(engine, "legacy_pg_t", "seen",
                                                 "DATETIME", default=""))
    with engine.connect() as conn:
        val = conn.execute(text("SELECT flag FROM legacy_pg_t")).scalar()
        typ = conn.execute(text(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_name='legacy_pg_t' AND column_name='seen'")).scalar()
    check("C 旧行填 false（布尔默认转换生效）", val is False, repr(val))
    check("C 列类型 timestamp", typ == "timestamp without time zone", typ)

    print("== D link_edges 整表重建迁移 ==")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS link_edges"))
        conn.execute(text(
            "CREATE TABLE link_edges ("
            " id SERIAL PRIMARY KEY, chain VARCHAR(16), host VARCHAR(128),"
            " upstream_addr VARCHAR(128), ecs_name VARCHAR(128), app_name VARCHAR(128),"
            " req_count INTEGER, cnt_4xx INTEGER, cnt_5xx INTEGER, rt_avg_ms FLOAT,"
            " ecs_cpu FLOAT, app_error_cnt INTEGER, path_summary TEXT,"
            " status VARCHAR(16), note TEXT, confirmed_by VARCHAR(64),"
            " confirmed_at TIMESTAMP, generated_at TIMESTAMP NOT NULL,"
            " CONSTRAINT uq_link_edge UNIQUE (chain, host, upstream_addr))"))
        conn.execute(text(
            "INSERT INTO link_edges (id, chain, host, upstream_addr, ecs_name, app_name,"
            " req_count, cnt_4xx, cnt_5xx, rt_avg_ms, app_error_cnt, path_summary,"
            " status, note, confirmed_by, generated_at) VALUES"
            " (1,'outer','a.example.com','10.0.0.1:80','web','app',100,1,2,12.5,0,'[]',"
            " 'confirmed','老行','alice','2026-11-01 00:00:00')"))
    check("D 迁移返回 True", migrate_link_edges_rule_dim(engine) is True)
    with engine.connect() as conn:
        cols = set(_cols(engine, "link_edges"))
        row = conn.execute(text(
            "SELECT path_pattern, rule_name, rule_source, note FROM link_edges WHERE id=1"
        )).first()
        idx = {r[0] for r in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='link_edges'"))}
    check("D 新三维列就位", {"path_pattern", "rule_name", "rule_source"} <= cols)
    check("D 旧行保留且默认值正确", row == ("", "", "log", "老行"), str(row))
    check("D 索引重建", {"ix_link_edges_chain", "ix_link_edges_host",
                       "ix_link_edges_status"} <= idx, str(sorted(idx)))
    check("D 幂等二跑 False", migrate_link_edges_rule_dim(engine) is False)
    db = SessionLocal()
    from app.models.link_edge import LinkEdge
    ne = LinkEdge(chain="inner", host="b.example.com", upstream_addr="10.0.0.2:80",
                  path_pattern="/api", rule_name="", rule_source="log",
                  ecs_name="", app_name="", req_count=1, cnt_4xx=0, cnt_5xx=0,
                  rt_avg_ms=0.0, app_error_cnt=0, path_summary="", status="draft",
                  note="", generated_at=datetime.now())
    db.add(ne)
    db.commit()
    check("D 序列已校准（新插入 id>1 不撞主键）", ne.id > 1, f"id={ne.id}")
    db.close()

    print("== E ILIKE 大小写口径 ==")
    db = SessionLocal()
    hit = db.query(Asset).filter(Asset.name.ilike("%webserver%")).first()
    check("E 关键词小写命中大写名称", hit is not None and hit.instance_id == "i-pg-e2e")
    db.close()

    print("== F 并发写冒烟（8 线程 × 25 行） ==")
    from app.models.audit import AuditLog

    errs = []

    def _writer(tid):
        s = SessionLocal()
        try:
            for i in range(25):
                s.add(AuditLog(action=f"pg.e2e.{tid}", user_id=0, username="pg-e2e",
                               resource_type="test", resource_id=str(i),
                               detail="{}", created_at=datetime.now()))
            s.commit()
        except Exception as e:
            errs.append(str(e)[:100])
            s.rollback()
        finally:
            s.close()

    ths = [threading.Thread(target=_writer, args=(t,)) for t in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    db = SessionLocal()
    n = db.query(AuditLog).filter(AuditLog.action.like("pg.e2e.%")).count()
    db.close()
    check("F 并发写全部成功", not errs, "; ".join(errs))
    check("F 行数 200 一致", n == 200, f"n={n}")

    print("== H 初始管理员 ==")
    from app.main import _ensure_init_admin
    from app.core.config import settings

    db = SessionLocal()
    db.query(User).delete()
    db.commit()
    db.close()
    _ensure_init_admin()
    db = SessionLocal()
    admin = db.query(User).filter(User.username == settings.INIT_ADMIN_USERNAME).first()
    check("H 空库自动建管理员", admin is not None)
    check("H 首登强制改密", admin is not None and admin.must_change_password is True)
    db.close()

    print()
    if FAILED:
        print(f"pg 端到端: 未过 {len(FAILED)} 项: {FAILED}")
        return 1
    print("pg 端到端: 全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
