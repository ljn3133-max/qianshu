"""安全组件：密码哈希、JWT、AES-256-GCM 凭证加密。

master key 约定：64 位 hex 字符串（32 字节），只存在于环境变量/.env，
绝不入库、不入备份。密文格式: base64(nonce + ciphertext + tag)。
"""
import base64
import hashlib
import os
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


# ---------- 密码 ----------
def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ---------- JWT ----------
def create_access_token(user_id: int, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "role": role, "exp": expire, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


# ---------- 个人 API Token（Bearer 与 JWT 并存）----------
API_TOKEN_PREFIX = "qs_"   # 可辨识前缀（JWT 恒以 eyJ 开头，两者不会混淆）


def hash_api_token(plain: str) -> str:
    """API Token 只存哈希（SHA-256）；明文仅创建时回显一次。"""
    return hashlib.sha256(plain.encode()).hexdigest()


def generate_api_token() -> tuple:
    """返回 (明文, 哈希, 前缀展示)。明文调用方只此一次可见，绝不入库。"""
    plain = API_TOKEN_PREFIX + secrets.token_urlsafe(32)   # 32 字节密码学随机
    return plain, hash_api_token(plain), plain[:11]        # 前缀如 qs_ab12cd34


def generate_password() -> str:
    """一次性初始/重置密码：大小写+数字+符号混排，满足 check_password_strength。
    明文只在生成方回显一次，库内只存哈希。copilot change_tools._gen_password 复用此实现。"""
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(10))
    return "Qs-" + core + secrets.choice("!@#")


# ---------- AES-256-GCM（云凭证等敏感字段加密）----------
def _master_key() -> bytes:
    if not settings.MASTER_KEY:
        raise RuntimeError("MASTER_KEY 未配置（需要 64 位 hex，openssl rand -hex 32 生成）")
    return bytes.fromhex(settings.MASTER_KEY)


def encrypt_secret(plaintext: str) -> str:
    key = _master_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), b"qianshu")
    return base64.b64encode(nonce + ct).decode()


def decrypt_secret(cipher_b64: str) -> str:
    key = _master_key()
    raw = base64.b64decode(cipher_b64)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, b"qianshu").decode()

_WEAK_PASSWORDS = {"admin123", "12345678", "password", "qwerty123", "123456789",
                   "admin888", "admin1234", "11111111", "password123"}


def check_password_strength(pwd: str, lang: str = "zh") -> str:
    """密码强度校验：返回错误信息，空串表示通过。
    文案由 app.core.i18n_msg 登记表按 lang 生成，缺省中文。"""
    from app.core.i18n_msg import t

    if len(pwd or "") < 8:
        return t("auth.pwd_too_short", lang)
    if pwd.lower() in _WEAK_PASSWORDS:
        return t("auth.pwd_common", lang)
    if pwd.isdigit() or pwd.isalpha():
        return t("auth.pwd_simple", lang)
    return ""
