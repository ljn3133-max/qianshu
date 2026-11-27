"""LLM 统一客户端。OpenAI 兼容协议（/chat/completions），不绑厂商。

所有调用走 chat()：解密 key -> 请求 -> 落 llm_call_logs（token 用量/耗时/成败）。
上层模块（NL 查询、巡检、诊断）只依赖这里，不直接碰 httpx。
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.ai_setting import AiSetting
from app.models.llm_call_log import LlmCallLog

logger = logging.getLogger("qianshu.llm")

_TIMEOUT_S = 300          # 大模型生成较慢（长输出场景更久），宽松超时
_ERR_MAX = 500            # 错误信息入库截断长度


class LLMError(RuntimeError):
    """LLM 调用失败（网络/鉴权/限流/响应异常），message 面向用户可直接展示。"""


def get_active_setting(db: Session) -> Optional[AiSetting]:
    """当前启用的配置（全表唯一 enabled=True）。"""
    return db.query(AiSetting).filter(AiSetting.enabled == True).first()  # noqa: E712


def _endpoint(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("base_url 未配置")
    if not url.startswith(("http://", "https://")):
        raise LLMError("base_url 必须以 http:// 或 https:// 开头")
    return f"{url}/chat/completions"


# ---- LLM 回答语言跟随界面 ---------------------------------------------------
# 开关 LLM_ANSWER_LANG_FOLLOW=UI（默认）：请求语言 en 时在系统提示词尾部追加一行
# `Respond in {language}.`；zh 不追加（提示词本就中文口径，旧行为逐字不变）；
# 开关为其他值（zh/fixed）=固定中文（兼容旧行为，一律不注入）。
# 调用方（ask/diagnosis/copilot/…)把 lang 从 Accept-Language 显式穿透进来；
# copilot/llm.py 复用 with_lang_directive 同一口径。

def _answer_lang_name(lang: str) -> str:
    """需要注入语言指令时返回目标语言名（English），否则 ""。"""
    if not (lang or "").lower().startswith("en"):
        return ""
    try:
        from app.core.config import settings
        follow = str(settings.LLM_ANSWER_LANG_FOLLOW or "UI").strip().upper() == "UI"
    except Exception:   # noqa: BLE001  配置缺失按默认 UI 跟随
        follow = True
    return "English" if follow else ""


def with_lang_directive(messages: list, lang: str = "zh") -> list:
    """系统提示词尾部追加语言指令一行（Respond in {language}.）。

    返回新 list（不改调用方原 messages——重试/多轮路径防重复追加）；
    无需注入（zh / 开关关）原样返回；无 system 消息时在 prompt 尾补一条 system 承载指令。
    """
    language = _answer_lang_name(lang)
    if not language:
        return messages
    line = f"Respond in {language}."
    out = [dict(m) for m in messages or []]
    for m in out:
        if m.get("role") == "system":
            m["content"] = f"{m.get('content') or ''}\n\n{line}"
            return out
    out.append({"role": "system", "content": line})
    return out


def chat(
    setting: AiSetting,
    messages: list,
    purpose: str = "test",
    db: Optional[Session] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    extra_payload: Optional[dict] = None,
    lang: str = "zh",
) -> dict:
    """调用一次对话补全。返回 {content, usage, duration_ms}；失败抛 LLMError。

    usage 可能缺失（部分网关不返回），此时各 token 记 0。
    lang：请求界面语言（zh/en）——en 且开关=UI 时系统提示词尾部注入
    `Respond in English.`（LLM 回答语言跟随界面）；缺省 zh 零变化。
    """
    messages = with_lang_directive(messages, lang)
    endpoint = _endpoint(setting.base_url)
    api_key = decrypt_secret(setting.api_key_cipher)
    payload = {
        "model": setting.model,
        "messages": messages,
        "temperature": setting.temperature if temperature is None else temperature,
    }
    mt = setting.max_tokens if max_tokens is None else max_tokens
    if mt:
        payload["max_tokens"] = mt
    if extra_payload:
        payload.update(extra_payload)

    started = time.monotonic()
    status = "ok"
    error = ""
    content = ""
    usage = {}
    try:
        resp = httpx.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT_S,
            # verify_ssl 默认 True；自建网关的自签证书可显式关闭校验
            verify=bool(getattr(setting, "verify_ssl", True)),
        )
        if resp.status_code != 200:
            # 常见业务错误体 {"error": {"message": ...}}，尽量透出
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                detail = resp.text[:200]
            raise LLMError(f"HTTP {resp.status_code}: {detail or resp.text[:200]}")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"响应结构异常: {str(data)[:300]}")
        usage = data.get("usage") or {}
    except LLMError as e:
        status = "error"
        error = str(e)
        raise
    except httpx.HTTPError as e:
        status = "error"
        error = f"网络错误: {str(e)[:200]}"
        raise LLMError(error) from e
    except Exception as e:  # 解析等意外
        status = "error"
        error = f"{type(e).__name__}: {str(e)[:200]}"
        raise LLMError(error) from e
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        if db is not None:
            try:
                db.add(LlmCallLog(
                    setting_id=setting.id,
                    provider_label=setting.provider_label,
                    model=setting.model,
                    purpose=purpose,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    total_tokens=int(usage.get("total_tokens") or 0),
                    duration_ms=duration_ms,
                    status=status,
                    error=error[:_ERR_MAX],
                    call_date=date.today(),
                ))
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("llm_call_logs 落库失败（不影响调用结果）")

    return {
        "content": content,
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        "duration_ms": duration_ms,
    }
