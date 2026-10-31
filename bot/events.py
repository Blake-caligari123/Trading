"""Event bus: every engine action becomes an event, persisted to JSONL and streamed to the terminal UI."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

LEVELS = ("debug", "info", "trade", "ai", "evolve", "warn", "error", "risk")


class EventBus:
    def __init__(self, log_path: Path | None = None, keep: int = 2000):
        self.history: deque[dict] = deque(maxlen=keep)
        self.subscribers: set[asyncio.Queue] = set()
        self.log_path = log_path
        self.loop: asyncio.AbstractEventLoop | None = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, level: str, msg: str, **data: Any) -> dict:
        ev = {"ts": time.time(), "level": level, "msg": msg}
        if data:
            ev["data"] = data
        self.history.append(ev)
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        if self.loop and self.subscribers:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self.loop:
                self._fanout(ev)
            else:  # emitted from a worker thread
                self.loop.call_soon_threadsafe(self._fanout, ev)
        return ev

    def _fanout(self, ev: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        self.loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
