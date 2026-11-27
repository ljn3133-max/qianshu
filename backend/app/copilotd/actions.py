"""动作留痕：内存环形 + 落盘 jsonl（GET /status 的数据源）。

所有 restart/heal/detect 动作必记（安全边界要求"每步留痕"）；
jsonl 追加写，0600。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Optional


class ActionLog:
    def __init__(self, path: str, ring_size: int = 200):
        self.path = path
        self._ring: deque = deque(maxlen=ring_size)
        self._lock = threading.Lock()
        self._seq = 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def record(self, action: str, trigger: str, ok: bool,
               steps: Optional[list] = None, detail: Optional[dict] = None,
               duration_s: Optional[float] = None) -> dict:
        with self._lock:
            self._seq += 1
            rec = {
                "id": self._seq,
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "action": action,           # heal / restart / detect / recovered / check
                "trigger": trigger,         # api / watchdog
                "ok": ok,
                "duration_s": duration_s,
                "steps": steps or [],
                "detail": detail or {},
            }
            self._ring.append(rec)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass   # 留痕失败不阻断动作本身
        return rec

    def recent(self, n: int = 50) -> list:
        with self._lock:
            items = list(self._ring)
        return items[-n:]

    @property
    def total(self) -> int:
        return self._seq
