"""Local HTTP/WebSocket API for the desktop UI (loopback only).

The UI shell (and nothing else) talks to this API. It binds to
127.0.0.1 - it is never exposed to the network; the mobile dashboard is
served by the Cloudflare layer in a later phase, fed by an outbound sync
client, not by this server.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import __version__
from ..core.events import EventBus
from ..db.database import Database
from ..marketdata.model import Timeframe
from ..monitoring.health import HealthMonitor

log = logging.getLogger(__name__)


def create_app(db: Database, bus: EventBus, health: HealthMonitor,
               engine_info: dict, news=None, calendar=None) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(title="ARGUS Engine API", version=__version__, lifespan=lifespan)

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        return JSONResponse({
            "overall": health.overall.value,
            "components": health.snapshot(),
            "version": __version__,
        })

    @app.get("/api/status")
    def api_status() -> dict:
        return {
            "version": __version__,
            "mode": engine_info.get("mode", "paper"),
            "state": engine_info.get("state", "unknown"),
            "symbols": engine_info.get("symbols", []),
            "provider": engine_info.get("provider", ""),
            "overall_health": health.overall.value,
        }

    @app.get("/api/candles/{symbol:path}/{timeframe}")
    def api_candles(symbol: str, timeframe: str, limit: int = 300) -> dict:
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            return {"error": f"unknown timeframe {timeframe!r}",
                    "allowed": [t.value for t in Timeframe]}
        limit = max(1, min(limit, 2000))
        candles = db.get_candles(symbol, tf, limit=limit)
        return {"symbol": symbol, "timeframe": tf.value,
                "candles": [c.as_dict() for c in candles]}

    @app.get("/api/news")
    def api_news(limit: int = 50) -> dict:
        if news is None:
            return {"news": [], "sentiment": {}, "enabled": False}
        return {"news": news.recent(max(1, min(limit, 200))),
                "sentiment": news.tracker.snapshot(), "enabled": True}

    @app.get("/api/events")
    def api_events(hours: int = 48) -> dict:
        if calendar is None:
            return {"events": []}
        return {"events": [
            {"ts": e.ts.isoformat(), "name": e.name, "importance": e.importance,
             "currencies": e.currencies} for e in calendar.upcoming(hours)]}

    @app.get("/api/models")
    def api_models() -> dict:
        from sqlalchemy import select
        from ..db.schema import ModelVersion
        with db.session() as s:
            rows = s.execute(select(ModelVersion).order_by(
                ModelVersion.name, ModelVersion.version.desc())).scalars().all()
        return {"models": [
            {"name": r.name, "version": r.version, "task": r.task,
             "status": r.status, "active": r.active, "n_samples": r.n_samples,
             "trained_at": r.trained_at.isoformat() if r.trained_at else None,
             "metrics": r.metrics} for r in rows]}

    @app.get("/api/decisions")
    def api_decisions(limit: int = 50) -> dict:
        from sqlalchemy import select
        from ..db.schema import Decision
        with db.session() as s:
            rows = s.execute(
                select(Decision).order_by(Decision.id.desc()).limit(max(1, min(limit, 200)))
            ).scalars().all()
        return {"decisions": [
            {"ts": r.ts.isoformat(), "symbol": r.symbol, "strategy": r.strategy,
             "score": r.score, "verdict": r.verdict, "vetoed_by": r.vetoed_by,
             "detail": r.reasons}
            for r in rows
        ]}

    @app.get("/api/audit")
    def api_audit(limit: int = 100) -> dict:
        rows = db.recent_audit(limit=max(1, min(limit, 500)))
        return {"entries": [
            {"ts": r.ts.isoformat(), "category": r.category,
             "severity": r.severity, "message": r.message, "data": r.data}
            for r in rows
        ]}

    @app.websocket("/ws")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        q = bus.subscribe(None)  # all events
        try:
            while True:
                ev = await q.get()
                data = ev.data
                if hasattr(data, "as_dict"):
                    data = data.as_dict()
                await ws.send_json({"type": ev.type, "ts": ev.ts.isoformat(), "data": data})
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            bus.unsubscribe(q)

    return app
