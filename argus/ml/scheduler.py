"""Continuous learning scheduler (§9D automation).

Nightly (default 03:00 UTC): retrain the setup scorer from historical
backtest signals MERGED with the bot's own closed trades, walk-forward
validate and register the candidate. Candidates are NEVER auto-activated:
activation remains an explicit step (CLI / future UI action) after the
validation gates and a paper-test window.

Also rebuilds the adaptive strategy selector from lived experience on
every closed trade and at startup.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..analysis.selector import StrategySelector
from ..core.events import EventBus
from ..db.database import Database
from ..monitoring.health import HealthMonitor, Status
from .experience import load_experience
from .registry import ModelRegistry
from .training import train_from_history

log = logging.getLogger(__name__)


class TrainingScheduler:
    def __init__(self, db: Database, bus: EventBus, health: HealthMonitor,
                 registry: ModelRegistry, selector: StrategySelector,
                 symbols: list[str], train_hour_utc: int = 3,
                 history_days: int = 60, enabled: bool = True) -> None:
        self._db = db
        self._bus = bus
        self._health = health
        self._registry = registry
        self._selector = selector
        self._symbols = symbols
        self._hour = train_hour_utc
        self._days = history_days
        self._enabled = enabled
        self._tasks: list[asyncio.Task] = []
        health.register("ml_training", stale_after=26 * 3600)
        health.set_status("ml_training", Status.OK,
                          f"nightly at {train_hour_utc:02d}:00 UTC" if enabled else "disabled")

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.rebuild_selector()
        trade_q = self._bus.subscribe("trade")
        self._tasks = [loop.create_task(self._trade_loop(trade_q), name="ml-selector")]
        if self._enabled:
            self._tasks.append(loop.create_task(self._nightly_loop(), name="ml-nightly"))

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    # --- selector upkeep --------------------------------------------------
    def rebuild_selector(self) -> None:
        records = load_experience(self._db)
        self._selector.reset()
        if records:
            self._selector.ingest(records)

    async def _trade_loop(self, q: asyncio.Queue) -> None:
        while True:
            ev = await q.get()
            if isinstance(ev.data, dict) and ev.data.get("kind") == "closed":
                try:
                    self.rebuild_selector()
                except Exception as exc:
                    log.warning("selector rebuild failed: %s", exc)

    # --- nightly training -------------------------------------------------
    async def _nightly_loop(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_run())
            try:
                await self.run_training()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("nightly training failed: %s", exc)
                self._db.audit("ml", f"Nightly training failed: {exc}", "error")
                self._health.set_status("ml_training", Status.DEGRADED, str(exc))

    def _seconds_until_run(self) -> float:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=self._hour, minute=0, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def run_training(self) -> dict:
        self._health.heartbeat("ml_training", Status.OK, "training running")
        extra = load_experience(self._db)
        out = await train_from_history(self._db, self._registry, self._symbols,
                                       days=self._days, extra_records=extra)
        val = out.get("validation", {})
        status = out.get("status", "skipped")
        msg = (f"setup-scorer v{out.get('version')}: {status}, "
               f"AUC {val.get('mean_auc')}, n={val.get('n_samples')} "
               f"(z toho {len(extra)} vlastných obchodov)")
        self._bus.publish("ml", {"title": "Výsledok nočného ML tréningu", "message": msg})
        self._health.heartbeat("ml_training", Status.OK, msg[:120])
        self.rebuild_selector()
        return out
