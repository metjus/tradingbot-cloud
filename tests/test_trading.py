import asyncio
import math
from datetime import datetime, timezone

import pytest

from argus.core.events import CANDLE, EventBus
from argus.db.database import Database
from argus.marketdata.model import Candle, Timeframe
from argus.monitoring.health import HealthMonitor
from argus.risk import RiskSupervisor
from argus.trading.broker import BracketOrder, PaperBroker, new_client_order_id
from argus.trading.executor import ExecutionService
from argus.trading.hours import can_enter, must_flatten
from argus.trading.portfolio import Portfolio


def bar(sym, ts, o, h, lo, c, tf=Timeframe.M1):
    return Candle(symbol=sym, timeframe=tf, ts=ts, open=o, high=h, low=lo,
                  close=c, volume=1.0, source="t")


def make_broker():
    fills = []
    b = PaperBroker(fee_pct=0.1, slippage_pct=0.0, clock=lambda: 0.0)
    b.on_fill = fills.append
    return b, fills


def test_paper_broker_entry_and_tp():
    b, fills = make_broker()
    b.on_candle(bar("BTC/USD", 0, 100, 100, 100, 100))
    b.submit_bracket(BracketOrder("c1", "BTC/USD", "LONG", 2.0, 99.0, 102.0))
    assert fills[0].kind == "entry" and fills[0].price == 100.0
    assert math.isclose(fills[0].fee, 100 * 2 * 0.001)
    b.on_candle(bar("BTC/USD", 60, 101, 102.5, 100.5, 102))
    assert fills[1].kind == "take_profit" and fills[1].price == 102.0
    assert b.positions() == []


def test_paper_broker_sl_wins_ties_and_duplicate_ignored():
    b, fills = make_broker()
    b.on_candle(bar("X/USD", 0, 100, 100, 100, 100))
    b.submit_bracket(BracketOrder("c1", "X/USD", "LONG", 1.0, 99.0, 102.0))
    b.submit_bracket(BracketOrder("c1", "X/USD", "LONG", 1.0, 99.0, 102.0))  # dup
    assert len(b.positions()) == 1
    b.on_candle(bar("X/USD", 60, 100, 103, 98, 101))  # both inside -> SL
    assert fills[-1].kind == "stop_loss" and fills[-1].price == 99.0


def test_paper_broker_short():
    b, fills = make_broker()
    b.on_candle(bar("X/USD", 0, 100, 100, 100, 100))
    b.submit_bracket(BracketOrder("s1", "X/USD", "SHORT", 1.0, 101.0, 98.0))
    b.on_candle(bar("X/USD", 60, 99, 99.5, 97.9, 98.1))
    assert fills[-1].kind == "take_profit" and fills[-1].price == 98.0


def test_portfolio_lifecycle_and_recovery(tmp_path):
    db = Database(tmp_path / "p.db")
    p = Portfolio(db, "paper", "paper", 10_000.0)
    p.record_entry("c1", "BTC/USD", "LONG", 2.0, 100.0, 0.2, 99.0, 102.0,
                   "momentum", 0.5, "reason", {"regime": "strong_uptrend"})
    assert p.account_state().open_positions == 1
    assert p.account_state().open_risk_pct == 0.5
    pl = p.record_exit("c1", 102.0, 0.2, "take_profit")
    assert math.isclose(pl, 2 * 2 - 0.4)
    assert math.isclose(p.equity, 10_000.0 + pl)
    assert p.trade_history()[0]["exit_reason"] == "take_profit"

    # recovery: a second portfolio over the same DB restores equity
    p2 = Portfolio(db, "paper", "paper", 10_000.0)
    assert math.isclose(p2.equity, p.equity)

    # open position survives restart
    p2.record_entry("c2", "ETH/USD", "SHORT", 1.0, 50.0, 0.1, 51.0, 48.0,
                    "breakout", 0.5, "r", {})
    p3 = Portfolio(db, "paper", "paper", 10_000.0)
    assert "c2" in p3.positions and p3.positions["c2"].direction == "SHORT"


def test_trading_hours():
    mon_noon = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)   # Monday 15:00 UTC
    mon_night = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
    mon_preclose = datetime(2026, 11, 16, 20, 55, tzinfo=timezone.utc)
    saturday = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    assert can_enter("BTC/USD", mon_night)            # crypto 24/7
    assert not must_flatten("BTC/USD", mon_night)
    assert can_enter("NVDA", mon_noon)
    assert not can_enter("NVDA", mon_night)
    assert not can_enter("NVDA", saturday)
    assert not can_enter("NVDA", mon_preclose)        # inside flat window
    assert must_flatten("NVDA", mon_preclose)
    assert not must_flatten("NVDA", mon_noon)


@pytest.fixture
def exec_env(tmp_path):
    db = Database(tmp_path / "e.db")
    bus = EventBus()
    broker = PaperBroker(fee_pct=0.0, slippage_pct=0.0, clock=lambda: 0.0)
    portfolio = Portfolio(db, "paper", "paper", 10_000.0)
    risk = RiskSupervisor()
    svc = ExecutionService(db, bus, HealthMonitor(bus), broker, portfolio, risk)
    return db, bus, broker, portfolio, svc


def decision_payload(symbol="BTC/USD", direction="LONG"):
    return {"verdict": "enter", "regime": "strong_uptrend",
            "best": {"score": 82.0, "factors": {},
                     "signal": {"symbol": symbol, "direction": direction,
                                "entry": 100.0, "stop_loss": 99.0,
                                "take_profit": 102.0, "strategy": "momentum",
                                "reasons": ["r1"], "rr": 2.0}},
            "risk": {"approved": True, "quantity": 2.0, "risk_pct": 0.5}}


def test_executor_entry_and_close_flow(exec_env):
    db, bus, broker, portfolio, svc = exec_env
    broker.on_candle(bar("BTC/USD", 0, 100, 100, 100, 100))
    svc._maybe_enter(decision_payload())
    assert len(portfolio.positions) == 1
    # duplicate symbol entry blocked
    svc._maybe_enter(decision_payload())
    assert len(portfolio.positions) == 1
    # TP hit
    broker.on_candle(bar("BTC/USD", 60, 101, 102.5, 100.5, 102))
    assert len(portfolio.positions) == 0
    assert math.isclose(portfolio.equity, 10_000.0 + 4.0)
    trades = portfolio.trade_history()
    assert trades[0]["exit_reason"] == "take_profit"
    assert trades[0]["entry_reason"] == "r1"


def test_executor_pause_lets_positions_run(exec_env):
    db, bus, broker, portfolio, svc = exec_env
    broker.on_candle(bar("BTC/USD", 0, 100, 100, 100, 100))
    svc._maybe_enter(decision_payload())
    out = svc.command("pause", command_id="cmd-1")
    assert out["state"] == "pausing"
    # no new entries while pausing
    svc._maybe_enter(decision_payload(symbol="ETH/USD"))
    assert len(portfolio.positions) == 1
    # idempotent command replay
    assert svc.command("pause", command_id="cmd-1")["duplicate"] is True
    # position closes on its own SL -> state becomes paused
    broker.on_candle(bar("BTC/USD", 60, 100, 100.2, 98.5, 99))
    assert len(portfolio.positions) == 0
    assert svc.state == "paused"
    # resume
    assert svc.command("start")["state"] == "running"


def test_executor_emergency_stop_closes_all(exec_env):
    db, bus, broker, portfolio, svc = exec_env
    broker.on_candle(bar("BTC/USD", 0, 100, 100, 100, 100))
    broker.on_candle(bar("ETH/USD", 0, 50, 50, 50, 50))
    svc._maybe_enter(decision_payload("BTC/USD"))
    svc._maybe_enter(decision_payload("ETH/USD"))
    assert len(portfolio.positions) == 2
    out = svc.command("emergency_stop")
    assert out["state"] == "stopped"
    assert len(portfolio.positions) == 0
    assert svc.risk.halted
    # entries blocked while stopped
    svc._maybe_enter(decision_payload("BTC/USD"))
    assert len(portfolio.positions) == 0
    trades = portfolio.trade_history()
    assert all(t["exit_reason"] == "manual_close" for t in trades)


def test_executor_respects_session_hours(exec_env, monkeypatch):
    db, bus, broker, portfolio, svc = exec_env
    broker.on_candle(bar("NVDA", 0, 100, 100, 100, 100))
    sat = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    import argus.trading.executor as ex
    monkeypatch.setattr(ex, "can_enter", lambda s, now=None: False)
    svc._maybe_enter(decision_payload("NVDA"))
    assert len(portfolio.positions) == 0
