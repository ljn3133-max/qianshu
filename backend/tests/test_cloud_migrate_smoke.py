#!/usr/bin/env python3
"""多云凭证迁移隔离冒烟：一次性 PostgreSQL 验证旧凭证洗入幂等 + resolve 优先级。

运行：cd /opt/qianshu/backend && python3.9 tests/test_cloud_migrate_smoke.py
隔离性：MASTER_KEY/DATABASE_URL 在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不用真实主密钥、不调任何云 API（腾讯云只测无密钥守卫路径）。

验证项：
  A 洗入：旧凭证行（AK 明文/密文两种形态）-> CloudAccount，字段可逆；
  B 幂等：重复执行不产生新行；重名跳过；extra_json 记 legacy id 映射；
  C resolve 优先级：ctx > 云账号表 > 旧凭证表 > env，逐档回退；
  D 多账号遍历：account_credential_contexts 每账号一个上下文 / 无账号回落单通道；
  E 腾讯云 Provider：注册表/支持产品/无密钥 sync 守卫（零网络）。
"""
import json
import os
import sys
import tempfile

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-smoke-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("smoke")
os.environ["MASTER_KEY"] = "ab" * 32          # 64 hex 测试主密钥
os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_cloud_accounts_from_legacy  # noqa: E402
from app.core.security import decrypt_secret, encrypt_secret  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.credential import CloudCredential  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def seed_legacy(db: Session) -> None:
    """三行旧凭证：AK 明文 / AK 密文 / 停用行。"""
    db.add(CloudCredential(
        name="prod-main", vendor="aliyun",
        access_key_id="FAKE_PLAIN_AK_0001",                    # AK 明文
        secret_cipher=encrypt_secret("FAKE_PLAIN_SK_0001"),
        remark="主账号", is_default=True, enabled=True,
    ))
    db.add(CloudCredential(
        name="dr-backup", vendor="aliyun",
        access_key_id=encrypt_secret("FAKE_CIPHER_AK_0002"),     # AK 已是密文
        secret_cipher=encrypt_secret("FAKE_CIPHER_SK_0002"),
        is_default=False, enabled=True,
    ))
    db.add(CloudCredential(
        name="old-off", vendor="aliyun",
        access_key_id="FAKE_DISABLED_AK_0003", secret_cipher=encrypt_secret("SKOFF0003"),
        is_default=False, enabled=False,
    ))
    db.commit()


def main() -> int:
    Base.metadata.create_all(bind=engine)   # 只建本冒烟导入到的模型表（临时库）
    db = SessionLocal()

    # ---------- A/B 洗入与幂等 ----------
    print("A. 旧凭证洗入")
    seed_legacy(db)
    r1 = migrate_cloud_accounts_from_legacy(engine)
    check("A1 首轮洗入 3 行", r1["migrated"] == 3 and r1["errors"] == 0, str(r1))

    rows = db.query(CloudAccount).order_by(CloudAccount.id).all()
    check("A2 行数=3", len(rows) == 3)
    by_name = {a.name: a for a in rows}
    a = by_name.get("prod-main")
    check("A3 AK 明文 -> 密文可逆", a is not None
          and decrypt_secret(a.ak_cipher) == "FAKE_PLAIN_AK_0001"
          and decrypt_secret(a.secret_cipher) == "FAKE_PLAIN_SK_0001")
    b = by_name.get("dr-backup")
    check("A4 AK 密文 -> 不双重加密", b is not None
          and decrypt_secret(b.ak_cipher) == "FAKE_CIPHER_AK_0002"
          and decrypt_secret(b.secret_cipher) == "FAKE_CIPHER_SK_0002")
    c = by_name.get("old-off")
    check("A5 停用/默认标志保留", c is not None and c.enabled is False and a.enabled is True)
    check("A6 vendor/legacy id 映射", a is not None and a.vendor == "aliyun"
          and json.loads(a.extra_json or "{}").get("legacy_credential_id") == 1)

    print("B. 幂等")
    r2 = migrate_cloud_accounts_from_legacy(engine)
    check("B1 重复执行 0 新增", r2["migrated"] == 0 and r2["skipped"] == 3, str(r2))
    check("B2 行数不变", db.query(CloudAccount).count() == 3)

    db.add(CloudAccount(name="manual-keep", vendor="aliyun",
                        ak_cipher=encrypt_secret("X"), secret_cipher=encrypt_secret("Y")))
    db.add(CloudCredential(name="manual-keep", vendor="aliyun",
                           access_key_id="AKDUP", secret_cipher=encrypt_secret("SKDUP"),
                           enabled=True))
    db.commit()
    r3 = migrate_cloud_accounts_from_legacy(engine)
    check("B3 重名跳过（不覆盖人工行）", r3["migrated"] == 0 and r3["skipped"] == 4, str(r3))
    check("B4 人工行未被覆盖", decrypt_secret(
        db.query(CloudAccount).filter(CloudAccount.name == "manual-keep")
        .first().ak_cipher) == "X")

    # ---------- C resolve 优先级 ----------
    print("C. resolve 优先级")
    from app.collector.credential import (
        CredentialError, account_credential, account_credential_contexts,
        resolve_aliyun_credential,
    )

    # 洗入后（3 个 CloudAccount + 3 个旧凭证 + 无 env）：
    ak, sk, src = resolve_aliyun_credential(db)
    check("C1 云账号表优先于旧凭证表", src.startswith("cloud_account:") and ak == "FAKE_PLAIN_AK_0001",
          f"{src} {ak}")

    with account_credential("CTXAK", "CTXSK", 999, source="ctx:test") as _:
        ak, sk, src = resolve_aliyun_credential(db)
        check("C2 上下文最高优先", (ak, sk, src) == ("CTXAK", "CTXSK", "ctx:test"), f"{src}")

    # 禁用云账号表 -> 落旧凭证表（默认行优先）
    db.query(CloudAccount).update({CloudAccount.enabled: False})
    db.commit()
    ak, sk, src = resolve_aliyun_credential(db)
    check("C3 云账号表失效 -> 旧凭证表", src == "db:prod-main" and ak == "FAKE_PLAIN_AK_0001",
          f"{src} {ak}")

    # 旧凭证表也禁用 -> 落 env
    db.query(CloudCredential).update({CloudCredential.enabled: False})
    db.commit()
    os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = "ENVAk"
    os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = "ENVSk"
    ak, sk, src = resolve_aliyun_credential(db)
    check("C4 旧凭证表失效 -> env", (ak, sk, src) == ("ENVAk", "ENVSk", "env"), f"{src}")

    # 全部缺失 -> CredentialError
    os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
    os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
    try:
        resolve_aliyun_credential(db)
        check("C5 全链失效抛 CredentialError", False)
    except CredentialError:
        check("C5 全链失效抛 CredentialError", True)

    # ---------- D 多账号遍历 ----------
    print("D. 多账号遍历")
    from contextlib import nullcontext

    db.query(CloudAccount).delete()
    db.commit()
    db.expunge_all()   # 清身份映射，避免 delete 后重用 PK 的 SAWarning
    db.add(CloudAccount(name="acc-1", vendor="aliyun", enabled=True,
                        ak_cipher=encrypt_secret("AK1"), secret_cipher=encrypt_secret("S1")))
    db.add(CloudAccount(name="acc-2", vendor="aliyun", enabled=True,
                        ak_cipher=encrypt_secret("AK2"), secret_cipher=encrypt_secret("S2")))
    db.add(CloudAccount(name="acc-off", vendor="aliyun", enabled=False,
                        ak_cipher=encrypt_secret("AK3"), secret_cipher=encrypt_secret("S3")))
    db.add(CloudAccount(name="acc-tencent", vendor="tencent", enabled=True,
                        ak_cipher=encrypt_secret("AK4"), secret_cipher=encrypt_secret("S4")))
    db.commit()

    from app.collector.credential import active_account_id

    ctxs = account_credential_contexts(db)
    seen = []
    for ctx in ctxs:
        with ctx:
            ak, sk, src = resolve_aliyun_credential(db)
            seen.append((active_account_id(), ak, src))
    check("D1 仅启用 aliyun 各一轮（2 个）", len(ctxs) == 2
          and [s[1] for s in seen] == ["AK1", "AK2"], str(seen))
    check("D2 上下文带账号 id 且 source 标账号", len(seen) == 2
          and seen[0][0] != seen[1][0]
          and seen[0][2] == "cloud_account:acc-1"
          and seen[1][2] == "cloud_account:acc-2", str(seen))

    db.query(CloudAccount).delete()
    db.commit()
    ctxs = account_credential_contexts(db)
    check("D3 无云账号回落单通道", len(ctxs) == 1 and isinstance(ctxs[0], type(nullcontext())))
    with ctxs[0]:
        check("D4 回落通道不带账号上下文", active_account_id() is None)

    # ---------- E 腾讯云 Provider（零网络） ----------
    print("E. 腾讯云 Provider")
    from app.services.cloud.provider import PROVIDERS, RESOURCE_TYPES, get_provider

    tp = get_provider("tencent")
    check("E1 注册表可取腾讯云", tp.name == "tencent" and "tencent" in PROVIDERS)
    # 产品清单随 SDK 补全增长，使用包含式断言避免陈旧断言假红
    check("E2 支持产品含 CVM 云主机", "cvm" in tp.supported_resource_types
          and RESOURCE_TYPES.get("cvm") == "CVM 云主机"
          and "tc_postgres" in tp.supported_resource_types, str(tp.supported_resource_types))

    no_key = CloudAccount(name="tc-nokey", vendor="tencent", enabled=True)
    res = tp.sync(db, no_key)
    check("E3 无密钥 sync 走守卫（零网络）", res and all(x.get("ok") is False for x in res)
          and "AK/SK" in res[0].get("error", ""), str(res))

    Base.metadata.create_all(bind=engine)   # Asset 表
    check("E4 list_resources 直查资产表", tp.list_resources(db, "cvm") == []
          and tp.list_resources(db, "rds") == [])   # 非本厂商支持类型返回空

    db.close()
    print(f"\n冒烟结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
