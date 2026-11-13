"""Trailing/break-even stops, experience learning, nightly scheduler parts."""

import math
from datetime import datetime, timezone

import pytest

from argus.core.events import EventBus
from argus.db.database import Database
from argus.marketdata.model import Candle, Timeframe
from argus.ml.experience import load_experience, trade_to_record
from argus.monitoring.health import HealthMonitor
from argus.risk import RiskSupervisor
from argus.trading.broker import BracketOrder, PaperBroker
from argus.trading.executor import ExecutionService
from argus.trading.portfolio import Portfolio


def bar(sym, ts, o, h, lo, c):
    return Candle(symbol=sym, timeframe=Timeframe.M1, ts=ts, open=o, high=h,
                  low=lo, close=c, volume=1.0, source="t")


FEATURES = {"close": 100.0, "rsi14": 60.0, "adx14": 28.0, "atr14": 1.0,
            "ema9": 99.5, "ema21": 99.0, "vwap": 99.2, "macd_hist": 0.1,
            "bb_upper": 101.5, "bb_lower": 98.5, "bb_width_pctile": 40.0,
            "vol_ratio": 1.3}


def decision_payload(symbol="BTC/USD"):
    return {"verdict": "enter", "regime": "strong_uptrend",
            "features_m5": dict(FEATURES),
            "best": {"score": 82.0, "factors": {},
                     "signal": {"symbol": symbol, "direction": "LONG",
                                "entry": 100.0, "stop_loss": 99.0,
                                "take_profit": 104.0, "strategy": "momentum",
                                "confidence": 0.7, "reasons": ["r"], "rr": 4.0}},
            "risk": {"approved": True, "quantity": 2.0, "risk_pct": 0.5}}


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "a.db")
    bus = EventBus()
    broker = PaperBroker(fee_pct=0.0, slippage_pct=0.0, clock=lambda: 0.0)
    portfolio = Portfolio(db, "paper", "paper", 10_000.0)
    svc = ExecutionService(db, bus, HealthMonitor(bus), broker, portfolio,
                           RiskSupervisor(), trailing_enabled=True,
                           break_even_at_r=1.0, trail_start_r=1.5,
                           trail_distance_r=1.0)
    return db, bus, broker, portfolio, svc


def open_position(broker, svc, symbol="BTC/USD"):
    broker.on_candle(bar(symbol, 0, 100, 100, 100, 100))
    svc._maybe_enter(decision_payload(symbol))


def test_break_even_moves_stop(env):
    db, bus, broker, portfolio, svc = env
    open_position(broker, svc)
    pos = next(iter(portfolio.positions.values()))
    assert pos.stop_loss == 99.0 and pos.initial_stop == 99.0
    # +1R profit (price 101) -> SL to break-even (entry + 0.05R)
    broker.on_candle(bar("BTC/USD", 60, 100.5, 101.2, 100.4, 101.0))
    svc._manage_stops("BTC/USD", 101.0)
    assert pos.be_done
    assert math.isclose(pos.stop_loss, 100.05)
    # broker-side stop also moved
    assert math.isclose(broker.positions()[0].stop_loss, 100.05)


def test_trailing_follows_price_and_never_widens(env):
    db, bus, broker, portfolio, svc = env
    open_position(broker, svc)
    pos = next(iter(portfolio.positions.values()))
    svc._manage_stops("BTC/USD", 102.0)   # +2R -> trail at price - 1R = 101
    assert math.isclose(pos.stop_loss, 101.0)
    svc._manage_stops("BTC/USD", 103.0)   # trail up to 102
    assert math.isclose(pos.stop_loss, 102.0)
    svc._manage_stops("BTC/USD", 101.5)   # price falls -> stop must NOT widen
    assert math.isclose(pos.stop_loss, 102.0)


def test_trailed_stop_exit_books_profit(env):
    db, bus, broker, portfolio, svc = env
    open_position(broker, svc)
    svc._manage_stops("BTC/USD", 103.0)   # stop now 102
    broker.on_candle(bar("BTC/USD", 120, 103, 103.2, 101.8, 101.9))  # hits 102
    assert portfolio.positions == {}
    t = portfolio.trade_history()[0]
    assert t["exit_reason"] == "stop_loss"
    assert t["pl"] == pytest.approx((102.0 - 100.0) * 2.0)


def test_paper_update_stop_rejects_widening():
    b = PaperBroker(clock=lambda: 0.0)
    b.on_candle(bar("X/USD", 0, 100, 100, 100, 100))
    b.submit_bracket(BracketOrder("c", "X/USD", "LONG", 1.0, 99.0, 102.0))
    assert not b.update_stop("c", 98.0)    # widening refused
    assert b.update_stop("c", 99.5)


def test_experience_roundtrip(env):
    db, bus, broker, portfolio, svc = env
    open_position(broker, svc)
    broker.on_candle(bar("BTC/USD", 60, 103, 104.5, 102.5, 104.2))  # TP 104 hit
    assert portfolio.positions == {}

    records = load_experience(db)
    assert len(records) == 1
    r = records[0]
    assert r.outcome == "win"
    assert r.signal.strategy == "momentum" and r.signal.direction == "LONG"
    assert r.features["rsi14"] == 60.0
    assert r.regime.value == "strong_uptrend"
    assert r.pl_r == pytest.approx(4.0)    # 8 EUR profit / (1.0 * 2 qty)

    # vectorizes into the shared ML dataset format
    from argus.ml.dataset import build_dataset
    ds = build_dataset(records)
    assert len(ds) == 1 and ds.y[0] == 1


def test_trade_without_context_skipped(tmp_path):
    db = Database(tmp_path / "x.db")
    p = Portfolio(db, "paper", "paper", 10_000.0)
    p.record_entry("c9", "BTC/USD", "LONG", 1.0, 100.0, 0.0, 99.0, 102.0,
                   "momentum", 0.5, "r", {})   # no signal/features context
    p.record_exit("c9", 102.0, 0.0, "take_profit")
    assert load_experience(db) == []


async def test_scheduler_selector_rebuild_on_close(env):
    import asyncio
    from argus.analysis.selector import StrategySelector
    from argus.ml.registry import ModelRegistry
    from argus.ml.scheduler import TrainingScheduler

    db, bus, broker, portfolio, svc = env
    selector = StrategySelector(min_sample=1)
    sched = TrainingScheduler(db, bus, HealthMonitor(bus),
                              ModelRegistry(db, db.engine.url.database and
                                            __import__("pathlib").Path("/tmp/m")),
                              selector, ["BTC/USD"], enabled=False)
    sched.start()
    open_position(broker, svc)
    broker.on_candle(bar("BTC/USD", 60, 103, 104.5, 102.5, 104.2))  # win
    await asyncio.sleep(0.1)
    sched.stop()
    snap = selector.snapshot()
    assert snap["stats"].get("momentum", {}).get("n") == 1
