#!/usr/bin/env python3
"""凭证体系收口隔离冒烟：一次性 PostgreSQL 全链路验证。

运行：cd /opt/qianshu/backend && python3 tests/test_credential_consolidation_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu 库、不用真实主密钥、不调任何云 API、不起后台任务（ASGI 直连不进 lifespan）。

验证项（对应收口清单）：
  A 旧表迁入幂等 + 回滚预案真跑：洗入带来源标记（migrated_from/legacy_credential_id）；
    重复执行幂等；按文档回滚 SQL 删洗入行后旧表数据零丢失，再迁移可重放；
  B 解析链读台账：迁移后旧表清空仍从台账解析（旧表空时行为不变）；
    旧表兜底档保留（旧行为向后兼容：台账空时仍读旧表）；全链失效抛 CredentialError；
  C 凭证消费方回归：tenant.account_id 台账优先（ctx > 台账 > 旧表兜底 > 默认）；
    bootstrap /api/bootstrap/status 只有台账凭证时 has_credential=true；
  D Settings 页签处置配套 API：GET /credentials 只读摘要带 migrated 标记（AK 打码）；
    写入/揭示/测试/凭证挂分组全部 410 弃用（云账号管理=唯一凭证入口）。
"""
import json
import os
import sys
import tempfile
import types

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-cred-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("cred")
os.environ["MASTER_KEY"] = "ef" * 32          # 64 hex 测试主密钥
for _k in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
           "TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_cloud_accounts_from_legacy  # noqa: E402
from app.core.security import decrypt_secret, encrypt_secret  # noqa: E402
from app.main import app  # noqa: E402,F401  导入全量模型（与 startup create_all 同口径）
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.credential import CloudCredential  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _make_req():
    """ASGI 直连请求壳（不触发 lifespan/startup；沿用 test_p2_structure_mock 套路）。"""
    import asyncio

    import httpx

    import app.core.deps as deps

    fake = types.SimpleNamespace(id=7, username="credsmoke", role="admin", is_active=True)
    app.dependency_overrides[deps.get_current_user] = lambda: fake

    def _req(method, url, **kw):
        async def _go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver") as c:
                return await c.request(method, url, **kw)
        return asyncio.run(_go())

    return _req


def seed_legacy(db: Session) -> None:
    """两行旧凭证：默认行 + 普通行（AK 明文/密文两种历史形态）。"""
    db.add(CloudCredential(
        name="legacy-main", vendor="aliyun",
        access_key_id="FAKE_LEGACY_AK_0001",
        secret_cipher=encrypt_secret("FAKE_LEGACY_SK_0001"),
        remark="主账号", is_default=True, enabled=True,
    ))
    db.add(CloudCredential(
        name="legacy-dr", vendor="aliyun",
        access_key_id=encrypt_secret("FAKE_LEGACY_AK_0002"),
        secret_cipher=encrypt_secret("FAKE_LEGACY_SK_0002"),
        remark="", is_default=False, enabled=True,
    ))
    db.commit()


def _migrated_rows(db: Session):
    out = []
    for a in db.query(CloudAccount).all():
        try:
            extra = json.loads(a.extra_json or "{}")
        except Exception:
            extra = {}
        if extra.get("migrated_from") == "cloud_credentials":
            out.append((a, extra))
    return out


# ================= A. 迁入幂等 + 回滚预案 =================
def test_migration() -> None:
    print("A. 旧表迁入幂等 + 回滚预案")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed_legacy(db)

    r1 = migrate_cloud_accounts_from_legacy(engine)
    check("A1 首轮洗入 2 行", r1["migrated"] == 2 and r1["errors"] == 0, str(r1))
    mrows = _migrated_rows(db)
    ids = {e.get("legacy_credential_id") for _, e in mrows}
    check("A2 来源标记齐（migrated_from + legacy id 映射）",
          len(mrows) == 2 and ids == {1, 2}, str(ids))

    r2 = migrate_cloud_accounts_from_legacy(engine)
    check("A3 重复执行幂等（0 新增）", r2["migrated"] == 0 and r2["skipped"] == 2, str(r2))

    # 回滚预案（migrate_cloud_accounts_from_legacy 文档口径）：删洗入行，旧表零改写
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM cloud_accounts WHERE extra_json LIKE '%\"migrated_from\": \"cloud_credentials\"%'"))
    with SessionLocal() as db2:
        check("A4 回滚后台账无洗入行", _migrated_rows(db2) == [])
        check("A5 回滚后旧表数据零丢失",
              db2.query(CloudCredential).count() == 2
              and decrypt_secret(
                  db2.query(CloudCredential).filter_by(name="legacy-main")
                  .first().secret_cipher) == "FAKE_LEGACY_SK_0001")

    r3 = migrate_cloud_accounts_from_legacy(engine)
    check("A6 回滚后可重放洗入", r3["migrated"] == 2 and r3["errors"] == 0, str(r3))
    db.close()


# ================= B. 解析链读台账 =================
def test_resolve_chain() -> None:
    print("B. 解析链读台账（旧表空时行为不变）")
    from app.collector.credential import CredentialError, resolve_aliyun_credential

    db = SessionLocal()
    ak, sk, src = resolve_aliyun_credential(db)
    check("B1 迁移后默认从台账取（默认凭证=最小 id）",
          src.startswith("cloud_account:") and ak == "FAKE_LEGACY_AK_0001"
          and sk == "FAKE_LEGACY_SK_0001", f"{src} {ak}")

    # 旧表清空（迁移完成后的常态）：仍从台账解析——旧表空时行为不变
    db.query(CloudCredential).delete()
    db.commit()
    ak, sk, src = resolve_aliyun_credential(db)
    check("B2 旧表清空仍读台账", src.startswith("cloud_account:")
          and ak == "FAKE_LEGACY_AK_0001", f"{src} {ak}")

    # 台账全停用 + 旧表空 + 无 env -> CredentialError（无隐藏旧表依赖）
    db.query(CloudAccount).update({CloudAccount.enabled: False})
    db.commit()
    try:
        resolve_aliyun_credential(db)
        check("B3 台账空+旧表空+无 env 抛 CredentialError", False)
    except CredentialError:
        check("B3 台账空+旧表空+无 env 抛 CredentialError", True)

    # 旧行为向后兼容：台账无可用时旧表兜底档仍可读
    db.add(CloudCredential(
        name="legacy-fallback", vendor="aliyun",
        access_key_id="FAKE_FALLBACK_AK", secret_cipher=encrypt_secret("FAKE_FALLBACK_SK"),
        is_default=True, enabled=True,
    ))
    db.commit()
    ak, sk, src = resolve_aliyun_credential(db)
    check("B4 旧表兜底档保留（向后兼容）",
          src == "db:legacy-fallback" and ak == "FAKE_FALLBACK_AK", f"{src} {ak}")
    db.close()


# ================= C. 凭证消费方回归 =================
def test_consumers() -> None:
    print("C. 凭证消费方回归（account_id / bootstrap）")
    from app.services import tenant

    db = SessionLocal()
    # 台账两行（旧行 B 轮全停用）：先清掉 B 轮遗留，重建干净场景
    db.query(CloudCredential).delete()
    db.query(CloudAccount).delete()
    db.commit()
    db.expunge_all()
    db.add(CloudAccount(id=3, name="acc-first", vendor="aliyun", enabled=True,
                        ak_cipher=encrypt_secret("AK3"), secret_cipher=encrypt_secret("S3")))
    db.add(CloudAccount(id=8, name="acc-second", vendor="aliyun", enabled=True,
                        ak_cipher=encrypt_secret("AK8"), secret_cipher=encrypt_secret("S8")))
    db.commit()

    check("C1 account_id 台账优先（启用 aliyun 最小 id）", tenant.account_id(db) == 3)

    from app.collector.credential import account_credential

    with account_credential("AKX", "SKX", 42, source="ctx:x"):
        check("C2 账号上下文最高优先", tenant.account_id(db) == 42)

    db.query(CloudAccount).update({CloudAccount.enabled: False})
    db.commit()
    check("C3 台账空回默认（旧行为不变）", tenant.account_id(db) == tenant.DEFAULT_ACCOUNT_ID)

    # bootstrap：只有台账凭证（旧表空=新装常态）时 has_credential 必须为真
    _req = _make_req()
    try:
        r = _req("GET", "/api/bootstrap/status")
        j = r.json()
        check("C4 bootstrap 台账全停用+旧表空+无 env -> has_credential=false",
              r.status_code == 200 and j.get("has_credential") is False, str(j))
        # 上面断言的是"全停用台账=无凭证"；启用一行再验真
        db.query(CloudAccount).filter_by(id=3).update({CloudAccount.enabled: True})
        db.commit()
        j2 = _req("GET", "/api/bootstrap/status").json()
        check("C5 bootstrap 台账启用带密钥 -> has_credential=true",
              j2.get("has_credential") is True and j2.get("credential_source") == "db",
              str(j2))
        db.query(CloudCredential).delete()
        db.add(CloudCredential(name="legacy-only", vendor="aliyun",
                               access_key_id="AKOLD", secret_cipher=encrypt_secret("SKOLD"),
                               enabled=True))
        db.query(CloudAccount).update({CloudAccount.enabled: False})
        db.commit()
        j3 = _req("GET", "/api/bootstrap/status").json()
        check("C6 bootstrap 旧表兜底档保留（向后兼容）",
              j3.get("has_credential") is True, str(j3))
    finally:
        app.dependency_overrides.clear()
    db.close()


# ================= D. Settings API 页签处置配套 =================
def test_settings_api() -> None:
    print("D. Settings 凭证 API（只读摘要 + 写入口 410）")
    db = SessionLocal()
    db.query(CloudCredential).delete()
    db.query(CloudAccount).delete()
    db.commit()
    db.expunge_all()
    seed_legacy(db)
    migrate_cloud_accounts_from_legacy(engine)

    _req = _make_req()
    try:
        r = _req("GET", "/api/credentials")
        rows = r.json()
        check("D1 GET /credentials 只读摘要", r.status_code == 200 and len(rows) == 2,
              r.text[:300])
        by_name = {x["name"]: x for x in rows}
        check("D2 migrated 标记与打码",
              by_name.get("legacy-main", {}).get("migrated") is True
              and by_name.get("legacy-dr", {}).get("migrated") is True
              and "FAKE_LEGACY_AK" not in r.text and "****" in r.text, r.text[:300])

        # 重名未并入行：migrated=false（页签如实展示漏网行）
        db.add(CloudAccount(name="clash-cred", vendor="aliyun",
                            ak_cipher=encrypt_secret("X"), secret_cipher=encrypt_secret("Y")))
        db.add(CloudCredential(name="clash-cred", vendor="aliyun",
                               access_key_id="AKCLASH", secret_cipher=encrypt_secret("SKCLASH"),
                               enabled=True))
        db.commit()
        r_mig = migrate_cloud_accounts_from_legacy(engine)   # 重名 -> 跳过不洗
        rows2 = _req("GET", "/api/credentials").json()
        clash = [x for x in rows2 if x["name"] == "clash-cred"]
        check("D3 重名未并入行如实标记 migrated=false",
              r_mig["migrated"] == 0 and len(clash) == 1
              and clash[0]["migrated"] is False, f"{r_mig} {clash}")

        # 写入口全部 410（云账号管理=唯一凭证入口）
        r401 = _req("POST", "/api/credentials", json={
            "name": "new", "access_key_id": "AKNEW12345", "secret": "sknew12345"})
        r402 = _req("PUT", "/api/credentials/1", json={"remark": "x"})
        r403 = _req("DELETE", "/api/credentials/1")
        r404 = _req("GET", "/api/credentials/1/secret")
        r405 = _req("POST", "/api/credentials/1/test")
        r406 = _req("PUT", "/api/settings/accounts/1/credential")
        codes = [r.status_code for r in (r401, r402, r403, r404, r405, r406)]
        check("D4 六个写入口全部 410 弃用", codes == [410] * 6, str(codes))
        check("D5 410 带弃用指引", "云账号管理" in r401.text, r401.text[:200])

        # 410 后旧表零写入
        with SessionLocal() as db2:
            check("D6 弃用后旧表零写入（冻结只读）",
                  db2.query(CloudCredential).count() == 3)   # seed 2 + 重名 1
    finally:
        app.dependency_overrides.clear()
    db.close()


def main() -> int:
    test_migration()
    test_resolve_chain()
    test_consumers()
    test_settings_api()
    print(f"\n收口冒烟结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
