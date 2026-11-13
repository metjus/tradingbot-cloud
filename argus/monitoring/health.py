"""System monitoring: component health registry with heartbeats.

Each component (data provider, aggregator, cloud sync, ...) reports
heartbeats and status changes. The monitor derives STALE automatically
when heartbeats stop arriving, publishes transitions on the event bus and
writes them to the audit log — the UI's System Monitoring panel reads
exactly this.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from ..core.events import HEALTH, EventBus

log = logging.getLogger(__name__)


class Status(str, Enum):
    STARTING = "starting"
    OK = "ok"
    DEGRADED = "degraded"
    STALE = "stale"
    DOWN = "down"


@dataclass
class ComponentHealth:
    name: str
    status: Status = Status.STARTING
    detail: str = ""
    last_heartbeat: float = field(default_factory=time.monotonic)
    stale_after: float = 90.0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "seconds_since_heartbeat": round(time.monotonic() - self.last_heartbeat, 1),
        }


class HealthMonitor:
    def __init__(self, bus: EventBus, check_interval: float = 5.0) -> None:
        self._bus = bus
        self._components: dict[str, ComponentHealth] = {}
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None

    def register(self, name: str, stale_after: float = 90.0) -> None:
        self._components[name] = ComponentHealth(name=name, stale_after=stale_after)

    def heartbeat(self, name: str, status: Status = Status.OK, detail: str = "") -> None:
        comp = self._components.get(name)
        if comp is None:
            self.register(name)
            comp = self._components[name]
        comp.last_heartbeat = time.monotonic()
        self._set(comp, status, detail)

    def set_status(self, name: str, status: Status, detail: str = "") -> None:
        comp = self._components.get(name)
        if comp is None:
            self.register(name)
            comp = self._components[name]
        self._set(comp, status, detail)

    def _set(self, comp: ComponentHealth, status: Status, detail: str) -> None:
        if status is not comp.status:
            log.info("health: %s %s -> %s (%s)", comp.name, comp.status.value, status.value, detail)
            comp.status = status
            comp.detail = detail
            self._bus.publish(HEALTH, comp.as_dict())
        else:
            comp.detail = detail

    def snapshot(self) -> list[dict]:
        return [c.as_dict() for c in self._components.values()]

    @property
    def overall(self) -> Status:
        if not self._components:
            return Status.STARTING
        order = [Status.DOWN, Status.STALE, Status.DEGRADED, Status.STARTING, Status.OK]
        statuses = {c.status for c in self._components.values()}
        for st in order:
            if st in statuses:
                return st
        return Status.OK  # pragma: no cover

    async def run(self) -> None:
        while True:
            now = time.monotonic()
            for comp in self._components.values():
                if comp.status in (Status.OK, Status.DEGRADED) and \
                        now - comp.last_heartbeat > comp.stale_after:
                    self._set(comp, Status.STALE,
                              f"no heartbeat for {int(now - comp.last_heartbeat)}s")
            await asyncio.sleep(self._check_interval)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="health-monitor")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
