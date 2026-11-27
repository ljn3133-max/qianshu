"""错误指纹与规范化计划哈希。

指纹只保留可稳定复现的字段：来源、账号/Run/能力、原因码、异常类型、最内层消息
前 N 字符、目标范围。内存地址、UUID、时间戳、URL query 中的敏感值全部抹掉；
哈希基于规范 JSON（sort_keys + 紧凑分隔符），跨进程/重启稳定。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_MAX_ERROR_CHARS = 240
_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ISO_RE = re.compile(
    r"\b20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_QUERY_RE = re.compile(r"([?&][^=\s&#]+)=([^&\s#]+)")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(ak|access[_-]?key(?:[_-]?id)?|sk|secret|password|passwd|token|"
    r"authorization|api[_-]?key|cookie)\b\s*[:=]\s*([^\s,;]+)")
_LONG_NUM_RE = re.compile(r"\b\d{8,}\b")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_hash(plan: dict) -> str:
    return sha256_text(canonical_json(plan))


def error_signature(error: Any) -> tuple[str, str]:
    """-> (异常类型, 归一化签名)。保留最内层消息，抹掉变量噪声。"""
    cls, message = "", ""
    if isinstance(error, BaseException):
        cls = type(error).__name__
        message = str(error)
        while getattr(error, "__cause__", None) is not None:
            error = error.__cause__
            message = str(error)
            cls = type(error).__name__
    elif isinstance(error, dict):
        cls = str(error.get("type") or error.get("class") or error.get("code") or "")
        message = str(error.get("message") or error.get("error") or error.get("detail") or "")
    else:
        message = str(error or "")
    message = _QUERY_RE.sub(r"\1=***", message)
    message = _SECRET_KV_RE.sub(r"\1=***", message)
    message = _ADDR_RE.sub("0x***", message)
    message = _UUID_RE.sub("uuid", message)
    message = _ISO_RE.sub("timestamp", message)
    message = _LONG_NUM_RE.sub("number", message)
    message = re.sub(r"\s+", " ", message).strip()
    return cls[:128], message[:_MAX_ERROR_CHARS]


def target_scope(target: Any) -> str:
    """目标范围只保留稳定标识；不接受任意长文本/路径。"""
    if target is None:
        return ""
    if isinstance(target, dict):
        keep = {}
        for key in sorted(target):
            if key in ("region", "source_id", "bill_date", "product", "vendor",
                       "target", "host", "service", "job", "tool"):
                keep[key] = str(target[key])[:120]
        return canonical_json(keep)
    return str(target)[:160]


def make_fingerprint(*, source: str, account_id: int = 0, run_id: int = 0,
                     capability: str = "", reason_code: str = "",
                     error: Any = "", target: Any = None,
                     error_class: str = "") -> str:
    cls, signature = error_signature(error)
    parts = {
        "source": (source or "").strip().lower(),
        "account_id": int(account_id or 0),
        "run_id": int(run_id or 0),
        "capability": (capability or "").strip().lower(),
        "reason_code": (reason_code or "").strip().lower(),
        "error_class": (error_class or cls).strip().lower(),
        "error": signature,
        "target": target_scope(target),
    }
    return sha256_text(canonical_json(parts))
