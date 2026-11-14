"""In-process async event bus.

Components publish typed events; subscribers receive them via asyncio
queues. Keeps engine modules decoupled (market data -> strategies ->
execution in later phases) and gives the local API a single tap point
for streaming state to the UI.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    type: str
    data: Any = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventBus:
    """Fan-out pub/sub. Slow subscribers drop oldest events (bounded queues)
    so one stuck consumer can never stall market data processing."""

    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subs: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self._all: list[asyncio.Queue[Event]] = []

    def subscribe(self, event_type: str | None = None) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        if event_type is None:
            self._all.append(q)
        else:
            self._subs[event_type].append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        for lst in list(self._subs.values()) + [self._all]:
            if q in lst:
                lst.remove(q)

    def publish(self, event_type: str, data: Any = None) -> None:
        ev = Event(type=event_type, data=data)
        for q in self._subs.get(event_type, []) + self._all:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # drop oldest
                    q.put_nowait(ev)
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
                log.warning("event queue full, dropped oldest (type=%s)", event_type)


# Well-known event types (phase 3)
CANDLE = "candle"            # data: Candle (closed bar)
CANDLE_PARTIAL = "candle.partial"
TICK = "tick"                # data: Tick
HEALTH = "health"            # data: {component, status, detail}
AUDIT = "audit"              # data: {category, severity, message, ...}
