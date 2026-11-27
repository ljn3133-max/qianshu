"""同步任务使用的凭证解析：
**账号上下文 > 云账号表 cloud_accounts（启用中的 aliyun）> 旧凭证表 cloud_credentials（只读过渡）> 环境变量（只读 RAM key）**。

1. 云账号表是凭证主通道——定时任务/手动采集的默认解析优先取 CloudAccount；
   旧凭证表保留只读过渡（migrate_cloud_accounts_from_legacy 启动时洗入新表，
   旧行不删不改，兜底链仍可读，直到确认全量迁移后下线）；
2. 账号级凭证上下文 account_credential——/clouds/sync 与定时任务多账号轮次注入，
   采集器经 resolve_aliyun_credential 无感取密；
3. 上下文用 ContextVar 承载，避免定时任务多账号轮次并发时线程级全局量串凭证：
   A 轮刚注入、B 轮覆盖会导致采集错账号；
4. account_credential_contexts：定时任务"每个启用 aliyun 账号各采一轮"的遍历入口，
   vendor 默认 aliyun；tencent 走 resolve_tencent_credential。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Optional

from sqlalchemy.orm import Session

from app.models.credential import CloudCredential
from app.core.security import decrypt_secret

logger = logging.getLogger("qianshu.cred")


class CredentialError(RuntimeError):
    pass


# 账号级凭证上下文（CloudProvider 批量同步 / 定时任务多账号轮次注入；None=走默认解析链）。
# ContextVar：协程/线程各自隔离，嵌套 set 后 reset 自动还原。
_ACTIVE: ContextVar[Optional[dict]] = ContextVar("qianshu_active_account_cred", default=None)


@contextmanager
def account_credential(ak: str, sk: str, account_id: int, source: str = ""):
    """把指定云账号凭证注入当前上下文（退出自动还原，支持嵌套）。"""
    token = _ACTIVE.set({"ak": ak, "sk": sk, "account_id": account_id,
                         "source": source or f"account:{account_id}"})
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_account_id() -> Optional[int]:
    """当前上下文归属的云账号 id（资产打标/清扫范围用）；无上下文返回 None。"""
    cur = _ACTIVE.get()
    return cur["account_id"] if cur else None


def account_credential_contexts(db: Session, vendor: str = "aliyun") -> list:
    """定时任务多账号遍历：每个启用中的指定厂商云账号返回一个 account_credential 上下文。

    - 账号无密钥（过渡期旧数据）/解密失败：跳过该账号（无密钥无从按账号采集），告警留痕；
    - 一个可用账号都没有：aliyun 返回 [nullcontext()]——单轮走默认解析链
      （云账号表 > 旧凭证表 > env），兼容未注入账号上下文的调用；
      其他厂商（如 tencent）返回 []——无默认解析链兜底语义，调用方直接跳过。
    """
    from app.models.cloud_account import CloudAccount

    out = []
    for acc in (db.query(CloudAccount)
                .filter(CloudAccount.enabled == True, CloudAccount.vendor == vendor)  # noqa: E712
                .order_by(CloudAccount.id).all()):
        if not (acc.ak_cipher and acc.secret_cipher):
            logger.warning("云账号 %s 无密钥，跳过本轮采集（云账号管理中补录 AK/SK）", acc.name)
            continue
        try:
            ak, sk = decrypt_secret(acc.ak_cipher), decrypt_secret(acc.secret_cipher)
        except Exception as e:
            logger.error("云账号 %s 解密失败，跳过本轮采集: %s", acc.name, e)
            continue
        out.append(account_credential(ak, sk, acc.id, source=f"cloud_account:{acc.name}"))
    if out:
        return out
    # 仅 aliyun 有默认解析链兜底（旧行为）；其他厂商无账号即无事可做
    return [nullcontext()] if vendor == "aliyun" else []


def resolve_aliyun_credential(db: Session) -> tuple[str, str, str]:
    """返回 (access_key_id, secret, source)。

    兜底顺序：ctx > 云账号表 cloud_accounts（启用中的 aliyun）> 旧凭证表 cloud_credentials > env。
    source: ctx / cloud_account:<name> / db:<name> / env
    """
    cur = _ACTIVE.get()
    if cur is not None:
        return cur["ak"], cur["sk"], cur["source"]
    # 1) 新通道：云账号表（启用中的 aliyun 账号，取最早登记的；逐账号采集由
    #    account_credential 上下文注入，见 account_credential_contexts）
    from app.models.cloud_account import CloudAccount

    for acc in (db.query(CloudAccount)
                .filter(CloudAccount.enabled == True, CloudAccount.vendor == "aliyun")  # noqa: E712
                .order_by(CloudAccount.id).all()):
        if not (acc.ak_cipher and acc.secret_cipher):
            continue
        try:
            return (decrypt_secret(acc.ak_cipher), decrypt_secret(acc.secret_cipher),
                    f"cloud_account:{acc.name}")
        except Exception as e:  # 单账号解密失败不阻断：回退下一档并记录
            logger.error("云账号 %s 解密失败，回退下一档: %s", acc.name, e)
    # 2) 旧凭证表（只读过渡：已洗入云账号表，本档仅为迁移窗口兜底）
    row: Optional[CloudCredential] = (
        db.query(CloudCredential)
        .filter(CloudCredential.enabled == True, CloudCredential.vendor == "aliyun")  # noqa: E712
        .order_by(CloudCredential.is_default.desc(), CloudCredential.id)
        .first()
    )
    if row:
        try:
            return row.access_key_id, decrypt_secret(row.secret_cipher), f"db:{row.name}"
        except Exception as e:  # 解密失败不阻断：回退下一档并记录
            logger.error("凭证 %s 解密失败，回退下一档: %s", row.name, e)
    # 3) 环境变量（只读 RAM key，最终兜底）
    ak = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
    sk = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
    if not ak or not sk:
        raise CredentialError("无可用阿里云凭证：请先在云账号管理中添加，或配置环境变量")
    return ak, sk, "env"


def resolve_sls_credential(db: Session) -> tuple[str, str, str]:
    """解析全局 SLS 目标的归属账号；绑定后禁用跨账号回退。

    SLS_ACCOUNT_ID=0 仅用于升级前旧配置，保留原默认解析链。绑定账号被停用、删除、
    解密失败或与显式账号上下文冲突时直接失败，绝不拿另一账号凭证查询目标。
    """
    from app.core.config import settings
    from app.models.cloud_account import CloudAccount

    account_id = int(getattr(settings, "SLS_ACCOUNT_ID", 0) or 0)
    if not account_id:
        return resolve_aliyun_credential(db)
    active_id = active_account_id()
    if active_id and active_id != account_id:
        raise CredentialError(f"SLS 目标属于账号 {account_id}，不能在账号 {active_id} 上下文中查询")
    acc = (db.query(CloudAccount)
           .filter(CloudAccount.id == account_id, CloudAccount.vendor == "aliyun",
                   CloudAccount.enabled == True).first())  # noqa: E712
    if acc is None:
        raise CredentialError(f"SLS 目标绑定的阿里云账号 {account_id} 不存在或已停用")
    if not (acc.ak_cipher and acc.secret_cipher):
        raise CredentialError(f"SLS 目标绑定的阿里云账号 {account_id} 缺少凭证")
    try:
        return (decrypt_secret(acc.ak_cipher), decrypt_secret(acc.secret_cipher),
                f"cloud_account:{acc.name}")
    except Exception as exc:
        raise CredentialError(f"SLS 目标绑定的阿里云账号 {account_id} 凭证解密失败") from exc


def resolve_tencent_credential(db: Session) -> tuple[str, str, str]:
    """腾讯云凭证解析：返回 (secret_id, secret_key, source)。

    兜底顺序（与 resolve_aliyun_credential 同构）：
    ctx > 云账号表 cloud_accounts（启用中的 tencent）> 环境变量（TENCENTCLOUD_*）。
    腾讯云无旧凭证表过渡档（旧凭证表只有 aliyun）。
    """
    cur = _ACTIVE.get()
    if cur is not None:
        return cur["ak"], cur["sk"], cur["source"]
    from app.models.cloud_account import CloudAccount

    for acc in (db.query(CloudAccount)
                .filter(CloudAccount.enabled == True, CloudAccount.vendor == "tencent")  # noqa: E712
                .order_by(CloudAccount.id).all()):
        if not (acc.ak_cipher and acc.secret_cipher):
            continue
        try:
            return (decrypt_secret(acc.ak_cipher), decrypt_secret(acc.secret_cipher),
                    f"cloud_account:{acc.name}")
        except Exception as e:
            logger.error("云账号 %s 解密失败，回退下一档: %s", acc.name, e)
    ak = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    sk = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    if not ak or not sk:
        raise CredentialError("无可用腾讯云凭证：请先在云账号管理中添加，或配置环境变量")
    return ak, sk, "env"
