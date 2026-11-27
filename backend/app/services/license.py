"""License（on-prem 授权文件校验）：Ed25519 非对称签名，产品内只放公钥。

形态：license.json（明文字段 + signature），字段：
  customer / issued / expires / max_accounts / features
策略：无 license=社区模式（全功能+温和提示）；过期=站内信告警+仍可用（不锁死客户生产）；
max_accounts 硬限（建号时校验）。

签名使用 Ed25519，避免在公开代码中内嵌 HMAC 密钥导致企业授权被伪造：
  - 产品只内嵌**公钥**（PUBLIC_KEYS_B64，可公开、可多把轮换）；
  - 私钥只存在于厂商侧的 /opt/qianshu/secrets/license_private.pem（0600，不入库、不入包）；
  - 签发由厂商侧离线工具完成（签发工具不随公开仓库提供）；验签只认私钥签出的签名。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.core.config import settings

logger = logging.getLogger("qianshu.license")

# 公钥可公开；数组支持轮换（新私钥签发后把新公钥追加进来，旧 license 继续有效）。
PUBLIC_KEYS_B64 = [
    "/5aMi0xsCe53pedtFiWciWvA6+cKHPWzP6PqZGIK0ac=",
]

_SIG_FIELD = "signature"


def _canonical(body: dict) -> bytes:
    """签名/验签共用的规范化 JSON：键排序、紧凑分隔符、保留非 ASCII。

    两边必须完全一致，否则合法 license 会被误判为签名失败。
    """
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def sign_license(payload: dict, private_key_pem: bytes) -> dict:
    """签发（仅供厂商侧脚本调用）：私钥由调用方从仓库外读取，产品内没有私钥。"""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    body = dict(payload)
    body[_SIG_FIELD] = base64.b64encode(key.sign(_canonical(body))).decode()
    return body


def verify_license(data: dict) -> bool:
    """任一已登记公钥验签通过即有效。"""
    sig = data.pop(_SIG_FIELD, "")
    if not sig:
        return False
    try:
        raw_sig = base64.b64decode(sig, validate=True)
    except (ValueError, TypeError):
        return False
    raw = _canonical(data)
    for key_b64 in PUBLIC_KEYS_B64:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64)).verify(raw_sig, raw)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def load_license() -> dict:
    """→ {mode: community/enterprise, customer, expires, max_accounts, valid}"""
    path = settings.LICENSE_FILE
    if not path or not os.path.exists(path):
        return {"mode": "community", "valid": True,
                "note": "社区模式：全功能可用；企业授权（账号上限/支持服务）见 docs"}
    try:
        data = json.load(open(path))
        if not verify_license(dict(data)):
            return {"mode": "invalid", "valid": False, "note": "license 签名校验失败"}
        exp = data.get("expires") or "2099-12-31"
        expired = datetime.strptime(exp, "%Y-%m-%d").date() < datetime.now().date()
        return {"mode": "enterprise", "valid": not expired, "customer": data.get("customer", ""),
                "expires": exp, "max_accounts": data.get("max_accounts", 99),
                "features": data.get("features", []),
                "note": "已过期（服务仍可用，请续期）" if expired else "企业授权有效"}
    except Exception as e:
        return {"mode": "invalid", "valid": False, "note": f"license 解析失败: {str(e)[:80]}"}


def check_account_limit(db) -> dict:
    """建号前校验 license 账号上限。"""
    lic = load_license()
    if lic["mode"] != "enterprise" or not lic.get("valid"):
        return {"ok": True}   # 社区/过期不锁功能
    from app.models.account import Account

    n = db.query(Account).count()
    cap = lic.get("max_accounts", 99)
    if n >= cap:
        return {"ok": False, "message": f"license 账号上限 {cap}（当前 {n}），请升级授权"}
    return {"ok": True}
