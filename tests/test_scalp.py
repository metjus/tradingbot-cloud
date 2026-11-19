"""Scalp mode (rýchle obchody): signals, sizing, fees, time stop, target
capital, paper-account reset and the switch from the old strategies."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from argus.config import Settings
from argus.decision import DecisionEngine
from argus.engine import Engine
from argus.marketdata.model import Candle, Timeframe
from argus.risk import AccountState, RiskLimits, RiskSupervisor
from argus.strategy import scalp
from argus.strategy.base import MarketContext, Signal, Strategy

M1 = Timeframe.M1


def _bars(n=300, sym="SOL/USD", end=None, price=100.0, breakout=True):
    """Flat 1m bars with a last bar breaking the 15-bar high on volume."""
    end = end or (int(time.time()) // 60 * 60 - 60)
    t0 = end - (n - 1) * 60
    out = []
    for i in range(n):
        o = c = price + (0.05 if i % 2 else -0.05)
        out.append(Candle(sym, M1, t0 + i * 60, o, max(o, c) + 0.05, min(o, c) - 0.05, c, 10.0,
                          complete=True))
    if breakout:
        last = out[-1]
        out[-1] = Candle(sym, M1, last.ts, price, price + 0.6, price - 0.02, price + 0.5, 40.0,
                         complete=True)
    return out


def _engine(tmp_path, **kw):
    e = Engine(Settings(data_dir=tmp_path / "d", crypto_symbols=["SOL/USD"], news_enabled=False,
                        ml_train_enabled=False, **kw))
    e.scalp.symbols = ["SOL/USD"]
    return e


# --- signal & levels --------------------------------------------------------------
def test_breakout_signal_limit_at_the_broken_level():
    bars = _bars()
    s = scalp.signal(bars, "breakout")
    hi = max(b.high for b in bars[-16:-1])
    assert s is not None and s.direction == "LONG" and s.level == pytest.approx(hi)
    assert scalp.signal(_bars(breakout=False), "breakout") is None


def test_mean_reversion_signal_fades_the_stretch():
    bars = _bars(breakout=False)
    last = bars[-1]
    bars[-1] = Candle(last.symbol, M1, last.ts, 100.0, 100.0, 98.9, 99.0, 10.0, complete=True)
    s = scalp.signal(bars, "mean_reversion")
    assert s is not None and s.direction == "LONG" and s.level == 99.0


def test_take_profit_nets_the_target_after_both_maker_fees():
    # 300 EUR / 5 slots x 5x = 300 EUR position, 1.50 EUR net -> 0.50 % + 0.04 %
    assert scalp.take_profit_pct(1.5, 300.0) == pytest.approx(0.54)
    sl, tp = scalp.levels(scalp.ScalpSignal("SHORT", 100.0, ""), 0.54, 0.6)
    assert tp == pytest.approx(99.46) and sl == pytest.approx(100.6)


# --- sizing ------------------------------------------------------------------------
def _rs(**kw):
    base = dict(leverage=5.0, max_leverage=10.0, maintenance_margin_pct=1.0, allow_short=True,
                daily_max_loss_pct=5.0, budget_eur=300.0, round_trip_cost_pct=0.09)
    base.update(kw)
    return RiskSupervisor(RiskLimits(**base))


def test_scalp_size_is_capital_over_slots_times_leverage():
    acc = AccountState(equity=300.0, day_start_equity=300.0)
    v = _rs().check_scalp("LONG", 100.0, 99.4, acc, slots=5, used_slots=0)
    assert v.approved and v.margin == pytest.approx(60.0) and v.notional == pytest.approx(300.0)
    assert v.quantity == pytest.approx(3.0)
    # profits are reinvested: a bigger account trades bigger positions
    v2 = _rs(budget_eur=None).check_scalp("LONG", 100.0, 99.4,
                                          AccountState(equity=600.0, day_start_equity=600.0), 5, 0)
    assert v2.notional == pytest.approx(600.0)


def test_scalp_limits_slots_daily_loss_and_budget():
    acc = AccountState(equity=300.0, day_start_equity=300.0)
    assert not _rs().check_scalp("LONG", 100.0, 99.4, acc, 5, 5).approved            # slots full
    lost = AccountState(equity=284.0, day_start_equity=300.0)                          # -5.3 %
    v = _rs().check_scalp("LONG", 100.0, 99.4, lost, 5, 0)
    assert not v.approved and any("denný limit" in x for x in v.vetoes)
    v = _rs().check_scalp("LONG", 100.0, 99.4, acc, 5, 4, used_margin=280.0)
    assert not v.approved                                                             # no money left


# --- the whole path: signal -> limit -> fill -> TP, fees -----------------------------
def test_scalp_trade_from_signal_to_take_profit(tmp_path):
    e = _engine(tmp_path)
    assert e.portfolio.equity == 300.0 and e.risk.limits.leverage == 5.0
    bars = _bars()
    e.db.upsert_candles(bars)
    e.broker.on_tick("SOL/USD", bars[-1].close)
    d = e.scalp.on_bar(bars[-1])
    assert d is not None
    sig = d["best"]["signal"]
    level = max(b.high for b in bars[-16:-1])
    assert sig["sleeve"] == "scalp" and sig["entry"] == pytest.approx(level)
    assert len(e.broker.pending_orders()) == 1 and not e.portfolio.positions
    assert e.scalp.on_bar(bars[-1]) is None                     # one per coin
    e.broker.on_tick("SOL/USD", level)                          # back to the level: maker fill
    pos = list(e.portfolio.positions.values())[0]
    assert pos.entry_price == pytest.approx(level) and pos.notional == pytest.approx(300.0)
    e.broker.on_tick("SOL/USD", pos.take_profit)
    t = e.portfolio.trade_history()[0]
    assert t["exit_reason"] == "take_profit"
    assert t["pl"] == pytest.approx(1.5, abs=0.01)              # 1.50 EUR net of both fees
    assert e.portfolio.equity == pytest.approx(301.5, abs=0.01)
    st = e.scalp_status()
    assert st["trades"] == 1 and st["exits"]["take_profit"] == 1
    assert st["target_eur"] == pytest.approx(1.5 * 301.5 / 300.0, abs=0.01)   # grows


def test_scalp_time_stop_closes_at_market(tmp_path):
    e = _engine(tmp_path)
    bars = _bars()
    e.db.upsert_candles(bars)
    e.broker.on_tick("SOL/USD", bars[-1].close)
    e.scalp.on_bar(bars[-1])
    level = max(b.high for b in bars[-16:-1])
    e.broker.on_tick("SOL/USD", level)
    pos = list(e.portfolio.positions.values())[0]
    e.executor._manage_stops("SOL/USD", level)
    assert e.portfolio.positions                                 # still young
    pos.opened_at = datetime.now(timezone.utc) - timedelta(minutes=61)
    e.executor._manage_stops("SOL/USD", level)
    assert not e.portfolio.positions
    assert e.portfolio.trade_history()[0]["exit_reason"] == "time_stop"


def test_scalp_skips_stale_bars_and_respects_disabled(tmp_path):
    e = _engine(tmp_path)
    old = _bars(end=int(time.time()) // 60 * 60 - 3600)
    e.db.upsert_candles(old)
    e.broker.on_tick("SOL/USD", old[-1].close)
    assert e.scalp.on_bar(old[-1]) is None                      # an hour old: replay
    e.runtime.update({"scalp_enabled": False})
    bars = _bars()
    e.db.upsert_candles(bars)
    assert e.scalp.on_bar(bars[-1]) is None


# --- target capital ----------------------------------------------------------------
def test_target_capital_closes_everything_and_stops_entries(tmp_path):
    e = _engine(tmp_path)
    bars = _bars()
    e.db.upsert_candles(bars)
    e.broker.on_tick("SOL/USD", bars[-1].close)
    e.scalp.on_bar(bars[-1])
    level = max(b.high for b in bars[-16:-1])
    e.broker.on_tick("SOL/USD", level)
    assert e.portfolio.positions
    e.runtime.update({"target_capital_eur": 300.5})
    pos = list(e.portfolio.positions.values())[0]
    e.broker.on_tick("SOL/USD", level * 1.003)                  # +0.3 %, under the TP
    e.portfolio.mark_price("SOL/USD", level * 1.003)
    assert e.executor.check_target()
    assert not e.portfolio.positions and e.executor.target_reached
    assert e.portfolio.trade_history()[0]["exit_reason"] == "target_reached"
    assert e.portfolio.equity >= 300.4                          # the target, give or take cents
    e.db.upsert_candles(_bars(sym="SOL/USD"))
    assert e.scalp.on_bar(_bars()[-1]) is None                  # no new entries
    e.runtime.update({"target_capital_eur": 400.0})             # raised -> trading again
    assert not e.executor.target_reached
    assert pos.client_order_id not in e.portfolio.positions


# --- reset & the switch to the new plan ----------------------------------------------
def test_reset_paper_account_archives_history(tmp_path):
    e = _engine(tmp_path)
    bars = _bars()
    e.db.upsert_candles(bars)
    e.broker.on_tick("SOL/USD", bars[-1].close)
    e.scalp.on_bar(bars[-1])
    e.broker.on_tick("SOL/USD", max(b.high for b in bars[-16:-1]))
    assert e.portfolio.positions
    res = e.reset_paper(500.0)
    assert res["closed"] == 1 and res["archived"] == 1
    assert e.portfolio.equity == 500.0 and not e.portfolio.positions
    assert e.portfolio.trade_history() == [] and e.portfolio.stats()["closed_trades"] == 0
    assert e.runtime["budget_eur"] == 500.0
    e2 = _engine(tmp_path)                                       # restart
    assert e2.portfolio.equity == 500.0 and e2.portfolio.trade_history() == []


def test_update_switches_to_the_new_plan_once(tmp_path):
    from argus.db.database import Database
    from argus.db.schema import AppSetting
    data = tmp_path / "d"
    data.mkdir()
    db = Database(data / "argus.db")
    with db.session() as s:                                      # a 1.2.x store
        s.add(AppSetting(key="runtime_settings", value={
            "leverage": 3.0, "daily_max_loss_pct": 2.0, "ai_trader_enabled": True,
            "active_enabled": True, "risk_guard": True, "entry_order": "limit",
            "budget_eur": 10_000.0}))
        s.commit()
    e = Engine(Settings(data_dir=data, crypto_symbols=[], news_enabled=False,
                        ml_train_enabled=False, paper_equity=300.0))
    assert e.runtime["leverage"] == 5.0 and e.runtime["daily_max_loss_pct"] == 5.0
    assert not e.runtime["ai_trader_enabled"] and not e.runtime["legacy_enabled"]
    assert e.portfolio.equity == 300.0 and e.runtime["budget_eur"] == 300.0
    assert not e.decision_engine.legacy_enabled
    e.runtime.update({"leverage": 10.0})
    e2 = Engine(Settings(data_dir=data, crypto_symbols=[], news_enabled=False,
                         ml_train_enabled=False))
    assert e2.runtime["leverage"] == 10.0                        # only once


def test_old_strategies_off_keep_the_analysis():
    class Always(Strategy):
        name = "always"
        suited_regimes = ()

        def evaluate(self, c):
            return Signal("always", "SOL/USD", "LONG", 100.0, 98.0, 110.0, 1.0, [])
    eng = DecisionEngine(RiskSupervisor(RiskLimits(round_trip_cost_pct=0.09)),
                         strategies=[Always()], enter_threshold=0)
    eng.legacy_enabled = False
    ctx = MarketContext(symbol="SOL/USD", now_ts=0, candles={}, features={}, regime=None,
                        extras={})
    res = eng.evaluate(ctx, AccountState(equity=300.0, day_start_equity=300.0))
    assert res.verdict == "no_signal" and res.regime is not None
