"""Cloud sync client: outbound-only link to the Cloudflare Worker (§3C).

- pushes a state snapshot every few seconds (and immediately on trade/state
  events), authenticated with a bearer token over HTTPS
- polls the command queue, executes commands locally via the executor's
  idempotent ``command()`` (dedupe by command id) and acknowledges each
  with its result
- pushes notification events (trade opened/closed, state changes, errors)
  for Web Push delivery
- never exposes local ports; if the cloud is unreachable the engine keeps
  trading and the dashboard shows the last sync time

API keys never leave the machine: the snapshot contains state only.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .. import __version__
from ..core.events import EventBus
from ..db.database import Database
from ..monitoring.health import HealthMonitor, Status

log = logging.getLogger(__name__)

NOTIFY_EVENTS = {"trade", "bot_state", "system_alert", "ml"}


class CloudSync:
    def __init__(self, db: Database, bus: EventBus, health: HealthMonitor,
                 executor, portfolio, news, base_url: str, token: str,
                 push_seconds: float = 10.0, poll_seconds: float = 5.0,
                 market_provider=None) -> None:
        self._market = market_provider     # () -> dict | None
        self._db = db
        self._bus = bus
        self._health = health
        self._executor = executor
        self._portfolio = portfolio
        self._news = news
        self._base = base_url.rstrip("/")
        self._push_s = push_seconds
        self._poll_s = poll_seconds
        self._client = httpx.AsyncClient(timeout=15.0, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": f"argus-engine/{__version__}"})
        self._tasks: list[asyncio.Task] = []
        self._dirty = asyncio.Event()
        self._dirty.set()            # first snapshot goes out immediately
        # what the Worker reported back on the last successful push
        self.remote: dict = {}
        self.last_sync_ts: float | None = None
        health.register("cloud_sync", stale_after=max(60.0, push_seconds * 5))

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        event_q = self._bus.subscribe(None)  # subscribe before any task runs
        self._tasks = [
            loop.create_task(self._push_loop(), name="cloud-push"),
            loop.create_task(self._command_loop(), name="cloud-commands"),
            loop.create_task(self._event_loop(event_q), name="cloud-events"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self._client.aclose()

    # --- snapshot ---------------------------------------------------------
    def snapshot(self) -> dict:
        snap = {
            "ts": int(time.time()),
            "version": __version__,
            "bot_state": self._executor.state if self._executor else "unknown",
            "health": {"overall": self._health.overall.value,
                       "components": self._health.snapshot()},
            "portfolio": self._portfolio.snapshot() if self._portfolio else {},
            "trades": self._portfolio.trade_history(30) if self._portfolio else [],
            "news": self._news.recent(15) if self._news else [],
            "sentiment": self._news.tracker.snapshot() if self._news else {},
        }
        if self._market is not None:
            try:
                snap["market"] = self._market()
            except Exception:
                pass
        return snap

    async def _push_loop(self) -> None:
        failures = 0
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=self._push_s)
            except asyncio.TimeoutError:
                pass
            self._dirty.clear()
            try:
                r = await self._client.post(f"{self._base}/ingest/state",
                                            json=self.snapshot())
                r.raise_for_status()
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        self.remote = {k: data.get(k) for k in
                                       ("phones", "pin_redeemed_ts", "server_ts")}
                except ValueError:
                    pass
                self.last_sync_ts = time.time()
                failures = 0
                self._health.heartbeat("cloud_sync", Status.OK, "synced")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self._health.set_status("cloud_sync", Status.DEGRADED,
                                        f"{type(exc).__name__} x{failures}")
                if failures in (1, 10):
                    log.warning("cloud sync failed (%d): %s", failures, exc)
                await asyncio.sleep(min(60.0, 2.0 ** min(failures, 6)))

    # --- events -> notifications ------------------------------------------
    async def _event_loop(self, q: asyncio.Queue) -> None:
        while True:
            ev = await q.get()
            if ev.type not in NOTIFY_EVENTS:
                continue
            self._dirty.set()
            payload = ev.data if isinstance(ev.data, dict) else {}
            category, title, body = self._format_notification(ev.type, payload)
            if title is None:
                continue
            try:
                await self._client.post(f"{self._base}/ingest/notification", json={
                    "ts": int(time.time()), "category": category,
                    "title": title, "body": body, "data": payload})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("notification push failed: %s", exc)

    @staticmethod
    def _format_notification(ev_type: str, d: dict):
        if ev_type == "trade" and d.get("kind") == "opened":
            return ("trade_opened", f"Otvorený obchod: {d.get('symbol')}",
                    f"{d.get('direction')} qty {d.get('quantity'):g} @ {d.get('entry'):g} · "
                    f"SL {d.get('stop_loss'):g} · TP {d.get('take_profit'):g} · "
                    f"{d.get('strategy', '')}")
        if ev_type == "trade" and d.get("kind") == "closed":
            pl = d.get("pl", 0.0)
            return ("trade_closed", f"Uzatvorený obchod: {d.get('symbol')}",
                    f"Výsledok {pl:+.2f} € ({d.get('reason')}) · kapitál {d.get('equity')}")
        if ev_type == "bot_state":
            return ("bot_state", f"ARGUS: {d.get('state', '?').upper()}",
                    str(d.get("why", "")))
        if ev_type == "system_alert":
            return (d.get("category", "system_error"), "ARGUS upozornenie",
                    str(d.get("message", "")))
        if ev_type == "ml":
            return ("ml_training", d.get("title", "ML tréning"),
                    str(d.get("message", "")))
        return (None, None, None)

    # --- commands ------------------------------------------------------------
    async def _command_loop(self) -> None:
        while True:
            try:
                r = await self._client.get(f"{self._base}/commands/pending")
                r.raise_for_status()
                for cmd in r.json().get("commands", []):
                    await self._execute_and_ack(cmd)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # push loop already tracks connectivity health
            await asyncio.sleep(self._poll_s)

    async def _execute_and_ack(self, cmd: dict) -> None:
        cid = str(cmd.get("id", ""))
        action = str(cmd.get("action", ""))
        issued = float(cmd.get("ts", 0))
        if not cid or not action:
            return
        if issued and time.time() - issued > 300:
            result = {"ok": False, "error": "command expired"}
        elif self._executor is None:
            result = {"ok": False, "error": "executor unavailable"}
        else:
            result = self._executor.command(action, command_id=f"cloud-{cid}",
                                            target=cmd.get("target") or None)
        self._db.audit("sync", f"Cloud command {action} ({cid})", data=result)
        try:
            await self._client.post(f"{self._base}/commands/ack",
                                    json={"id": cid, "result": result,
                                          "ts": int(time.time())})
        except Exception as exc:
            log.warning("command ack failed for %s: %s", cid, exc)
        self._dirty.set()
