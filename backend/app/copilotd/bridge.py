"""平台桥接：daemon → 千枢 API（审计写入 + 最近变更查询）。

接口（见 app/api/v1/agent_lifecycle.py，X-Copilotd-Token 鉴权）：
  GET  /api/agent/lifecycle/recent   最近变更工单 + 审计流水（定位用）
  POST /api/agent/lifecycle/report   自愈事件 → 平台审计（不写站内信/红点）

平台不可用时上报进 ReportQueue，恢复后补报（不丢事件）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import List, Optional


class BridgeError(Exception):
    pass


class BridgeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 6.0):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        if not self.enabled:
            raise BridgeError("bridge token 未配置（.env COPILOTD_BRIDGE_TOKEN）")
        url = self.base_url + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Copilotd-Token", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise BridgeError(f"HTTP {e.code}: {body}") from e
        except Exception as e:  # noqa: BLE001
            raise BridgeError(str(e)[:160]) from e

    def recent_changes(self) -> dict:
        return self._call("GET", "/api/agent/lifecycle/recent")

    def report(self, action: str, title: str, content_md: str,
               level: str = "warning", detail: Optional[dict] = None) -> dict:
        return self._call("POST", "/api/agent/lifecycle/report", {
            "action": action, "title": title, "content_md": content_md,
            "level": level, "detail": detail or {},
        })


class ReportQueue:
    """平台不可用时的待补报队列（平台恢复后按序补报）。"""

    def __init__(self, limit: int = 100):
        self._pending: List[dict] = []
        self._limit = limit

    def add(self, action: str, title: str, content_md: str,
            level: str = "warning", detail: Optional[dict] = None) -> None:
        if len(self._pending) >= self._limit:
            self._pending.pop(0)   # 环形：超限丢最旧
        self._pending.append({"action": action, "title": title, "content_md": content_md,
                              "level": level, "detail": detail or {}})

    def flush(self, bridge: BridgeClient) -> dict:
        """逐条补报；单条失败即停（留待下次），返回 sent/failed 计数。"""
        sent = 0
        while self._pending:
            item = self._pending[0]
            try:
                bridge.report(**item)
            except Exception:  # noqa: BLE001 —— 平台又挂了，剩余留给下轮
                break
            self._pending.pop(0)
            sent += 1
        return {"sent": sent, "pending": len(self._pending)}

    @property
    def pending(self) -> int:
        return len(self._pending)
