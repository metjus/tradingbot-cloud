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
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import __version__
from ..core.events import EventBus
from ..db.database import Database
from ..marketdata.model import Timeframe
from ..monitoring.health import HealthMonitor

log = logging.getLogger(__name__)


def create_app(db: Database, bus: EventBus, health: HealthMonitor,
               engine_info: dict, news=None, calendar=None,
               executor=None, portfolio=None, decision_engine=None,
               settings=None, engine=None) -> FastAPI:

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
        forming = engine.forming_bar(symbol, tf) if engine is not None else None
        if forming is not None and candles and forming.ts <= candles[-1].ts:
            forming = None
        return {"symbol": symbol, "timeframe": tf.value,
                "candles": [c.as_dict() for c in candles],
                "forming": forming.as_dict() if forming is not None else None}

    @app.get("/api/portfolio")
    def api_portfolio() -> dict:
        if portfolio is None:
            return {"available": False}
        snap = portfolio.snapshot()
        snap["available"] = True
        snap["bot_state"] = executor.state if executor else "unknown"
        return snap

    @app.get("/api/trades")
    def api_trades(limit: int = 100) -> dict:
        if portfolio is None:
            return {"trades": []}
        return {"trades": portfolio.trade_history(max(1, min(limit, 500)))}

    @app.post("/api/control")
    def api_control(payload: dict) -> dict:
        if executor is None:
            return {"ok": False, "error": "executor unavailable"}
        action = str(payload.get("action", ""))
        command_id = payload.get("command_id")
        target = payload.get("target")
        return executor.command(action, str(command_id) if command_id else None,
                                target=str(target) if target else None)

    @app.post("/api/positions/{coid}/close")
    def api_close_position(coid: str) -> dict:
        if executor is None:
            return {"ok": False, "error": "executor unavailable"}
        return executor.command("close_position", target=coid)

    # --- ML controls & knowledge export/import ---------------------------
    @app.get("/api/ml/status")
    def api_ml_status() -> dict:
        if engine is None:
            return {"available": False}
        sch = engine.ml_scheduler
        scorer = engine.decision_engine.ml_scorer
        return {"available": True, "running": sch.running,
                "last_run_at": sch.last_run_at,
                "last_result": sch.last_result,
                "active_model": (f"{scorer.meta.name} v{scorer.meta.version}"
                                 if scorer is not None else None),
                "auto_activate": engine.runtime["ml_auto_activate"]}

    @app.post("/api/ml/train")
    def api_ml_train() -> dict:
        if engine is None:
            return {"ok": False, "error": "engine unavailable"}
        started = engine.ml_scheduler.train_now()
        return {"ok": started, "error": None if started else "tréning už beží"}

    @app.post("/api/ml/activate")
    def api_ml_activate(payload: dict) -> JSONResponse:
        try:
            engine.registry.activate("setup-scorer", int(payload.get("version")))
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 400)
        engine.reload_model()
        return JSONResponse({"ok": True})

    @app.post("/api/ml/rollback")
    def api_ml_rollback() -> dict:
        v = engine.registry.rollback("setup-scorer")
        engine.reload_model()
        return {"ok": v is not None, "version": v}

    @app.get("/api/knowledge/export")
    def api_knowledge_export():
        from fastapi.responses import Response
        from ..ml.knowledge import export_bundle
        data = export_bundle(db, engine.settings.data_dir / "models", engine.runtime)
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M")
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="argus-knowledge-{stamp}.zip"'})

    @app.post("/api/knowledge/import")
    async def api_knowledge_import(request: Request, apply_settings: int = 0) -> JSONResponse:
        from ..ml.knowledge import import_bundle
        data = await request.body()
        if len(data) > 500 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "súbor je príliš veľký"}, 413)
        try:
            report = import_bundle(db, engine.settings.data_dir / "models", engine.registry,
                                   data, runtime=engine.runtime,
                                   apply_settings=bool(apply_settings))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 400)
        engine.reload_model()
        engine.ml_scheduler.rebuild_selector()
        from ..ml.training import load_profiles
        engine.selector.set_priors(load_profiles(db))
        return JSONResponse({"ok": True, "report": report})

    # --- LLM provider & local secrets --------------------------------------
    ALLOWED_SECRETS = {"anthropic_api_key", "openai_api_key", "gemini_api_key"}

    @app.get("/api/llm")
    def api_llm() -> dict:
        if engine is None:
            return {"available": False}
        st = engine.llm.status()
        st["available"] = True
        st["secrets_encrypted"] = engine.secrets.encrypted
        st["configured_keys"] = sorted(k for k in engine.secrets.configured()
                                       if k in ALLOWED_SECRETS)
        return st

    @app.post("/api/secrets")
    def api_set_secret(payload: dict) -> JSONResponse:
        name = str(payload.get("name", ""))
        value = str(payload.get("value", "")).strip()
        if name not in ALLOWED_SECRETS:
            return JSONResponse({"ok": False, "error": "neznámy kľúč"}, 400)
        if not value:
            engine.secrets.delete(name)
            db.audit("security", f"API kľúč {name} odstránený")
            return JSONResponse({"ok": True, "deleted": True})
        engine.secrets.set(name, value)
        db.audit("security", f"API kľúč {name} uložený")
        return JSONResponse({"ok": True})

    # --- mobile app pairing ------------------------------------------------
    @app.get("/api/cloud")
    def api_cloud() -> dict:
        return engine.cloud_status() if engine is not None else {"paired": False}

    @app.post("/api/cloud/pair")
    async def api_cloud_pair(payload: dict) -> JSONResponse:
        try:
            await engine.pair_cloud(str(payload.get("url", "")),
                                    str(payload.get("pairing_code", "")))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 400)
        return JSONResponse({"ok": True, **engine.cloud_status()})

    @app.post("/api/cloud/pin")
    async def api_cloud_pin() -> JSONResponse:
        try:
            d = await engine.create_pairing_pin()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 400)
        return JSONResponse({"ok": True, "pin": d["pin"], "expires_in": d["expires_in"]})

    @app.post("/api/cloud/unpair")
    async def api_cloud_unpair() -> dict:
        await engine.unpair_cloud()
        return {"ok": True}

    @app.post("/api/cloud/rotate")
    async def api_cloud_rotate() -> JSONResponse:
        try:
            await engine.rotate_dashboard_token()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 400)
        return JSONResponse({"ok": True, **engine.cloud_status()})

    @app.get("/api/settings")
    def api_get_settings() -> dict:
        if engine is None:
            return {"available": False}
        from ..fees import venues_table
        d = engine.runtime.describe()
        d["available"] = True
        d["venues"] = venues_table()
        d["active_symbols"] = engine.crypto_symbols
        return d

    @app.post("/api/settings")
    def api_update_settings(payload: dict) -> JSONResponse:
        if engine is None:
            return JSONResponse({"ok": False, "error": "engine unavailable"}, 400)
        try:
            changed = engine.runtime.update(payload)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, 400)
        from ..runtime_settings import RESTART_REQUIRED
        return JSONResponse({"ok": True, "changed": changed,
                             "restart_required": sorted(set(changed) & RESTART_REQUIRED),
                             "values": engine.runtime.as_dict()})

    @app.get("/api/strategies")
    def api_strategies() -> dict:
        if decision_engine is None:
            return {"strategies": []}
        sel = decision_engine.selector
        snap = sel.snapshot() if sel is not None else {"disabled": [], "stats": {}}
        out = []
        for s in decision_engine.strategies:
            st = snap["stats"].get(s.name) or {}
            out.append({
                "name": s.name,
                "suited_regimes": [r.value for r in s.suited_regimes],
                "enabled": s.name not in snap["disabled"],
                "n": st.get("n", 0), "win_rate": st.get("win_rate"),
                "profit_factor_r": st.get("profit_factor_r"),
                "expectancy_r": st.get("expectancy_r"),
                "by_regime": st.get("by_regime", {}),
            })
        symbols = engine.crypto_symbols if engine is not None else []
        matrix = sel.symbol_matrix(symbols, [s.name for s in decision_engine.strategies]) \
            if sel is not None else {}
        return {"strategies": out, "per_symbol": matrix, "symbols": symbols,
                "note": ("Každá minca má vlastný výber stratégií: kým nie je dosť vlastných "
                         "obchodov (15), rozhoduje kalibračný backtest po poplatkoch; "
                         "stratégia so ziskovým faktorom pod 1 sa pre danú mincu vypne.")}

    @app.get("/api/risk")
    def api_risk() -> dict:
        if executor is None:
            return {"available": False}
        L = executor.risk.limits
        acct = portfolio.account_state() if portfolio else None
        return {
            "available": True,
            "halted": executor.risk.halted,
            "halt_reason": executor.risk.halt_reason,
            "limits": {
                "risk_per_trade_pct": L.effective_risk_pct(),
                "max_risk_per_trade_pct": L.max_risk_per_trade_pct,
                "daily_max_loss_pct": L.daily_max_loss_pct,
                "max_open_positions": L.max_open_positions,
                "max_daily_trades": L.max_daily_trades,
                "min_rr": L.min_rr,
                "max_spread_pct": L.max_spread_pct,
                "max_position_notional_pct": L.max_position_notional_pct,
                "budget_eur": L.budget_eur,
                "max_position_eur": L.max_position_eur,
                "round_trip_cost_pct": round(L.round_trip_cost_pct, 3),
                "allow_short": L.allow_short,
            },
            "usage": None if acct is None else {
                "daily_pl_pct": round(acct.daily_pl_pct, 3),
                "open_positions": acct.open_positions,
                "trades_today": acct.trades_today,
                "open_risk_pct": round(acct.open_risk_pct, 2),
                "invested": round(acct.open_notional, 2),
            },
            "trailing": None if executor is None else {
                "enabled": executor.exit_params.enabled,
                "reduce_risk_at_r": executor.exit_params.reduce_risk_at_r,
                "break_even_at_r": executor.exit_params.break_even_at_r,
                "trail_start_r": executor.exit_params.trail_start_r,
                "trail_distance_r": executor.exit_params.trail_distance_r,
                "giveback_pct": executor.exit_params.giveback_pct,
                "sentiment_exit": executor.exit_params.sentiment_exit,
                "regime_exit": executor.exit_params.regime_exit,
                "round_trip_cost_pct": round(executor.exit_params.round_trip_cost_pct, 3),
            },
        }

    @app.get("/api/readiness")
    def api_readiness() -> dict:
        from ..readiness import evaluate_readiness
        start_eq = settings.paper_equity if settings is not None else 10_000.0
        return evaluate_readiness(db, start_eq).as_dict()

    @app.get("/api/market")
    def api_market() -> dict:
        """Live per-coin market info: 24h volume (coin + USD), funding,
        mark price - for the volume display and the futures UI."""
        if engine is None:
            return {"info": {}, "symbols": []}
        return engine.market_info()

    @app.get("/api/config")
    def api_config() -> dict:
        if settings is None:
            return {"available": False}
        s = settings
        return {"available": True, "config": {
            "trading_mode": s.trading_mode, "paper_broker": s.paper_broker,
            "paper_equity": s.paper_equity,
            "crypto_provider": s.crypto_provider,
            "crypto_symbols": engine.crypto_symbols if engine is not None else s.crypto_symbols,
            "stock_symbols": s.stock_symbols,
            "enter_threshold": s.enter_threshold,
            "candle_poll_seconds": s.candle_poll_seconds,
            "news_enabled": s.news_enabled,
            "llm_enabled": bool(s.anthropic_api_key),
            "llm_daily_budget_usd": s.llm_daily_budget_usd,
            "cloud_sync_enabled": bool(s.cloud_url and s.cloud_token),
            "ml_train_enabled": s.ml_train_enabled,
            "ml_train_hour_utc": s.ml_train_hour_utc,
            "alpaca_configured": bool(s.alpaca_key_id),
            "data_dir": str(s.data_dir),
        }, "note": "Hodnoty sa menia cez premenné prostredia ARGUS_* alebo .env (docs/INSTALACIA.md); kľúče a tokeny sa nezobrazujú."}

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

    @app.get("/api/explore")
    def api_explore() -> dict:
        """Trial (skúšobný režim) sleeve: result net of fees + kill rules."""
        if engine is None:
            return {"enabled": False, "trades": 0}
        return engine.explore_status()

    @app.get("/api/ai")
    def api_ai() -> dict:
        """AI trader: status, recent decisions with reasons, trial account."""
        if engine is None:
            return {"enabled": False, "decisions": []}
        return {**engine.ai_trader.status(), "account": engine.explore_status("ai")}

    @app.get("/api/watch")
    def api_watch() -> dict:
        """What the bot sees per coin right now (watchlist)."""
        if engine is None:
            return {"coins": [], "evaluations": 0}
        a = engine.analysis
        return {"coins": sorted(a.watch.values(), key=lambda w: w["symbol"]),
                "evaluations": a.evaluations,
                "strategies": [s.name for s in engine.decision_engine.strategies]}

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

    @app.get("/api/indicators/{symbol:path}/{timeframe}")
    def api_indicators(symbol: str, timeframe: str, limit: int = 400) -> dict:
        """Indicator series aligned with /api/candles (same source of truth
        as the engine's decisions - no client-side recomputation)."""
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            return {"error": f"unknown timeframe {timeframe!r}"}
        limit = max(30, min(limit, 2000))
        candles = db.get_candles(symbol, tf, limit=limit)
        if not candles:
            return {"ts": []}
        from ..ta import indicators as ind
        closes = [c.close for c in candles]
        macd_line, macd_sig, macd_hist = ind.macd(closes)
        bb_up, bb_mid, bb_lo = ind.bollinger(closes, 20, 2.0)
        adx_s, pdi, mdi = ind.adx(candles, 14)
        return {
            "adx": adx_s, "plus_di": pdi, "minus_di": mdi,
            "ts": [c.ts for c in candles],
            "ema9": ind.ema(closes, 9),
            "ema21": ind.ema(closes, 21),
            "ema50": ind.ema(closes, 50) if len(closes) >= 50 else [None] * len(closes),
            "ema200": ind.ema(closes, 200) if len(closes) >= 200 else [None] * len(closes),
            "vwap": ind.vwap(candles),
            "bb_upper": bb_up, "bb_mid": bb_mid, "bb_lower": bb_lo,
            "rsi": ind.rsi(closes, 14),
            "macd": macd_line, "macd_signal": macd_sig, "macd_hist": macd_hist,
            "atr": ind.atr(candles, 14),
        }

    @app.get("/api/audit")
    def api_audit(limit: int = 100) -> dict:
        rows = db.recent_audit(limit=max(1, min(limit, 500)))
        return {"entries": [
            {"ts": r.ts.isoformat(), "category": r.category,
             "severity": r.severity, "message": r.message, "data": r.data}
            for r in rows
        ]}

    # --- desktop UI (static, served at /) ---
    ui_dir = Path(__file__).resolve().parent.parent / "ui"
    if ui_dir.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/app", StaticFiles(directory=str(ui_dir), html=True), name="ui")

        @app.get("/")
        def root_redirect():
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/app/")

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
