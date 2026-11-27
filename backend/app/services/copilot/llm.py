"""copilot LLM 适配：OpenAI 兼容 chat completions。

- 与 services/llm.py 同协议不同口：这里吃 resolve_model() 的 dict（api_key 已解密）、
  支持 stream=true 流式增量，供 SSE token 事件直出。
- base_url 自动补 /v1 兼容（OpenAI 标准路径）；已带 /v1 或 /chat/completions 的不重复补。
- verify_ssl 透传（自建网关自签证书可关）；temperature/max_tokens 取自场景矩阵（可覆盖）。
- 每次调用落 llm_call_logs（purpose=copilot），口径与 services/llm.py 一致。

安全（红线）：本模块只发起 chat completions 请求，不执行任何 LLM 产出的命令/SQL。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Optional

import httpx

from app.models.llm_call_log import LlmCallLog

logger = logging.getLogger("qianshu.copilot.llm")

_TIMEOUT_S = 300
_ERR_MAX = 500


class LLMError(RuntimeError):
    """LLM 调用失败（网络/鉴权/限流/响应异常），message 面向用户可直接展示。"""


def _endpoint(base_url: str) -> str:
    """chat completions 端点：base_url 自动补 /v1 兼容。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("base_url 未配置")
    if not url.startswith(("http://", "https://")):
        raise LLMError("base_url 必须以 http:// 或 https:// 开头")
    if url.endswith("/chat/completions"):
        return url
    if not url.endswith(("/v1", "/v2")):
        url += "/v1"          # OpenAI 标准路径；api.deepseek.com 等 /v1 同样兼容
    return url + "/chat/completions"


def _payload(cfg: dict, messages: list, stream: bool,
             temperature: Optional[float], max_tokens: Optional[int]) -> dict:
    payload = {
        "model": cfg.get("model") or "",
        "messages": messages,
        "temperature": cfg.get("temperature", 0.2) if temperature is None else temperature,
    }
    mt = cfg.get("max_tokens") if max_tokens is None else max_tokens
    if mt:
        payload["max_tokens"] = mt
    if stream:
        payload["stream"] = True
    return payload


def _record(db, cfg: dict, purpose: str, usage: dict, duration_ms: int,
            status: str, error: str) -> None:
    """llm_call_logs 记账（复用 services/llm.py 口径，purpose=copilot）。"""
    if db is None:
        return
    try:
        db.add(LlmCallLog(
            setting_id=int(cfg.get("setting_id") or 0),
            provider_label=str(cfg.get("label") or ""),
            model=str(cfg.get("model") or ""),
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
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("llm_call_logs 落库失败（不影响调用结果）")


def _est_tokens(texts: list) -> int:
    """usage 缺失时的保守估算（中文约 1.5 字/token，按 3 字符/token 估）。"""
    n = sum(len(t) for t in texts)
    return max(1, n // 3)


def chat_once(cfg: dict, messages: list, *, purpose: str = "copilot",
              db=None, temperature: Optional[float] = None,
              max_tokens: Optional[int] = None, lang: str = "zh") -> dict:
    """非流式 chat completions。返回 {content, usage, duration_ms, model}；失败抛 LLMError。

    lang：请求界面语言（zh/en）——en 且 LLM_ANSWER_LANG_FOLLOW=UI 时系统提示词尾部
    注入 `Respond in English.`（与 services/llm.chat 同口径，复用其 with_lang_directive）。
    """
    from app.services.llm import with_lang_directive

    messages = with_lang_directive(messages, lang)
    endpoint = _endpoint(cfg.get("base_url") or "")
    payload = _payload(cfg, messages, False, temperature, max_tokens)
    started = time.monotonic()
    content, usage, status, error = "", {}, "ok", ""
    try:
        resp = httpx.post(
            endpoint, json=payload,
            headers={"Authorization": f"Bearer {cfg.get('api_key') or ''}"},
            timeout=_TIMEOUT_S,
            verify=bool(cfg.get("verify_ssl", True)),
        )
        if resp.status_code != 200:
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
        if not usage.get("total_tokens"):
            usage = {"prompt_tokens": 0, "completion_tokens": 0,
                     "total_tokens": _est_tokens([_flat(messages), content])}
    except LLMError as e:
        status, error = "error", str(e)
        raise
    except httpx.HTTPError as e:
        status, error = "error", f"网络错误: {str(e)[:200]}"
        raise LLMError(error) from e
    except Exception as e:
        status, error = "error", f"{type(e).__name__}: {str(e)[:200]}"
        raise LLMError(error) from e
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        _record(db, cfg, purpose, usage, duration_ms, status, error)
    return {"content": content, "usage": _norm_usage(usage),
            "duration_ms": duration_ms, "model": cfg.get("model") or ""}


def chat_stream(cfg: dict, messages: list, *, purpose: str = "copilot",
                db=None, out: Optional[dict] = None,
                temperature: Optional[float] = None,
                max_tokens: Optional[int] = None, lang: str = "zh"):
    """流式 chat completions（stream=true）。返回文本增量迭代器。

    逐段 yield 文本增量；迭代完成（或中途被消费方放弃/abort）后 out（可选）被填充：
    {content, usage, duration_ms, model, aborted, error}。
    调用方：for delta in chat_stream(...)。
    失败抛 LLMError（同样落 llm_call_logs，status=error）。
    lang：请求界面语言（zh/en）——en 且 LLM_ANSWER_LANG_FOLLOW=UI 时系统提示词尾部
    注入 `Respond in English.`（与 services/llm.chat 同口径）。
    """
    from app.services.llm import with_lang_directive

    messages = with_lang_directive(messages, lang)
    endpoint = _endpoint(cfg.get("base_url") or "")
    payload = _payload(cfg, messages, True, temperature, max_tokens)
    started = time.monotonic()
    state = {"content": [], "usage": {}, "aborted": False, "status": "ok", "error": ""}
    state["done"] = False

    def _finish() -> None:
        if state["done"]:
            return
        state["done"] = True
        duration_ms = int((time.monotonic() - started) * 1000)
        content = "".join(state["content"])
        usage = state["usage"]
        if not usage.get("total_tokens"):
            usage = {"prompt_tokens": 0, "completion_tokens": 0,
                     "total_tokens": _est_tokens([_flat(messages), content])}
        _record(db, cfg, purpose, usage, duration_ms, state["status"], state["error"])
        if out is not None:
            out.update({"content": content, "usage": _norm_usage(usage),
                        "duration_ms": duration_ms, "model": cfg.get("model") or "",
                        "aborted": state["aborted"], "error": state["error"]})

    def gen():
        try:
            with httpx.stream(
                "POST", endpoint, json=payload,
                headers={"Authorization": f"Bearer {cfg.get('api_key') or ''}"},
                timeout=_TIMEOUT_S,
                verify=bool(cfg.get("verify_ssl", True)),
            ) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "replace")[:300]
                    try:
                        detail = json.loads(body).get("error", {}).get("message", "")
                    except Exception:
                        detail = body
                    raise LLMError(f"HTTP {resp.status_code}: {detail or body}")
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        state["usage"] = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0] or {}).get("delta") or {}
                    text = delta.get("content") or ""
                    if text:
                        state["content"].append(text)
                        yield text
        except GeneratorExit:
            state["aborted"] = True      # 消费方中断（abort/放弃）——保留已生成部分
            raise
        except LLMError as e:
            state["status"], state["error"] = "error", str(e)
            raise
        except httpx.HTTPError as e:
            state["status"], state["error"] = "error", f"网络错误: {str(e)[:200]}"
            raise LLMError(state["error"]) from e
        except Exception as e:
            state["status"], state["error"] = "error", f"{type(e).__name__}: {str(e)[:200]}"
            raise LLMError(state["error"]) from e
        finally:
            _finish()                    # 正常/异常/中断三路都记账

    return gen()


def _norm_usage(usage: dict) -> dict:
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _flat(messages: list) -> str:
    return "".join(str(m.get("content") or "") for m in messages or [])
