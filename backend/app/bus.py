"""极简进程内事件总线，用于把采集线程产生的转写结果推给 SSE 客户端。

采集跑在普通线程里，SSE 跑在事件循环里。中间用 `loop.call_soon_threadsafe`
把事件安全地投递到 asyncio.Queue，避免跨线程直接操作 coroutine 对象。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import deque
from typing import Any

log = logging.getLogger("wingman.bus")


class EventBus:
    def __init__(self, history: int = 300) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._seq = itertools.count(1)

    # ------------------------------------------------------ 生命周期

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------ 订阅

    def subscribe(self, replay: bool = True) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        if replay:
            for evt in list(self._history):
                try:
                    q.put_nowait(evt)
                except asyncio.QueueFull:
                    break
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # ------------------------------------------------------ 发布

    def publish(self, event: dict[str, Any]) -> None:
        evt = dict(event)
        evt.setdefault("seq", next(self._seq))
        self._history.append(evt)
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._fanout, evt)
                return
            except RuntimeError:
                pass
        self._fanout(evt)

    def _fanout(self, evt: dict[str, Any]) -> None:
        dead: list[asyncio.Queue] = []
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # 消费端太慢，丢掉最旧的，保证实时性优先
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subs.discard(q)

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        return list(self._history)[-n:]

    def clear(self) -> None:
        self._history.clear()


bus = EventBus()
