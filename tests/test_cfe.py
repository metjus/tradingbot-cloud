"""CFE in Argus = the research reference, signal for signal, without look-ahead.

Reference: tests/cfe_reference/ is an unmodified copy of backtest/cfe.py,
engine.py and indicators.py from metjus/trash (branch
claude/tradingview-futures-indicator-4qmlz9). Data: real Kraken Futures 15m
and 1h candles (tests/data/cfe_kraken_sample.json.gz).
"""

import gzip
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from argus.cfe.adapter import CHART_SEC, default_params, frame
from argus.marketdata.model import Candle, Timeframe
from argus.strategy.base import MarketContext
from argus.strategy.cfe_strategy import CfeStrategy

DATA = json.loads(gzip.open(Path(__file__).parent / "data" / "cfe_kraken_sample.json.gz",
                            "rt").read())


def _reference():
    """Import the untouched research module; numba is optional here."""
    if "numba" not in sys.modules:
        try:
            import numba  # noqa: F401
        except ImportError:
            fake = types.ModuleType("numba")
            fake.njit = lambda *a, **k: (a[0] if a and callable(a[0]) and not k
                                         else (lambda f: f))
            sys.modules["numba"] = fake
    here = str(Path(__file__).parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from cfe_reference import cfe as ref
    return ref


def _candles(rows, tf, sym="ETH/USD"):
    return [Candle(sym, tf, int(r[0]), r[1], r[2], r[3], r[4], r[5], complete=True)
            for r in rows]


def _live_signals(m15, h1, bars, sym="ETH/USD"):
    """Drive CfeStrategy as the bot does: at a 15m close it only sees the
    candles that are closed at that moment (2000 x 15m, 3600 x 1h)."""
    s = CfeStrategy()
    c15 = _candles(m15, Timeframe.M15, sym)
    c1h = _candles(h1, Timeframe.H1, sym)
    t1h = np.array([r[0] for r in h1])
    got = {}
    for i in bars:
        now = int(m15[i][0]) + CHART_SEC
        lo = max(0, i - 2100)
        k = int(np.searchsorted(t1h, now - 3600, side="right"))
        ctx = MarketContext(symbol=sym, now_ts=now, candles={}, features={},
                            regime=None, extras={})
        ctx.extras["m15_loader"] = lambda lo=lo, i=i: c15[lo:i + 1]
        ctx.extras["h1_loader"] = lambda k=k: c1h[max(0, k - 3700):k]
        sig = s.evaluate(ctx)
        if sig is not None:
            got[int(m15[i][0])] = sig
    return got


@pytest.mark.parametrize("coin", ["PF_ETHUSD", "PF_SOLUSD", "PF_DOGEUSD"])
def test_argus_cfe_signals_match_the_reference(coin):
    ref = _reference()
    m15 = np.array(DATA[coin]["m15"])
    h1 = np.array(DATA[coin]["h1"])
    p = ref.P2(htf_min=1.0, min_score=70.0, session_filter=True, cost_mult=4.0,
               fee_taker=0.05, fee_maker=0.02, slippage=0.02, entry_mode=1, valid_bars=12)
    full = ref.compute(frame(m15), p, base_df=frame(h1), chart_tf="15m")
    start = len(m15) - 2500
    want = {}
    for i in np.where(full["sig_l"] | full["sig_s"])[0]:
        if i >= start:
            k = "l" if full["sig_l"][i] else "s"
            want[int(m15[i][0])] = (k, full[f"entry_{k}"][i], full[f"sl_{k}"][i],
                                    full[f"tp1_{k}"][i], full[f"tp3_{k}"][i], full[f"score_{k}"][i])
    # every reference signal bar + a spread of other bars (each live call is
    # a full window recomputation, so not all 2500 bars)
    sig_bars = [i for i in range(start, len(m15)) if int(m15[i][0]) in want]
    other = list(range(start, len(m15), 97))
    got = _live_signals(m15, h1, sorted(set(sig_bars + other)))
    assert want, "the sample must contain signals"
    assert set(got) == set(want)
    for ts, (k, entry, sl, tp1, tp3, score) in want.items():
        g = got[ts]
        assert g.direction == ("LONG" if k == "l" else "SHORT")
        assert g.entry == pytest.approx(entry) and g.stop_loss == pytest.approx(sl)
        assert g.tps[0] == pytest.approx(tp1) and g.take_profit == pytest.approx(tp3)
        assert g.confidence == pytest.approx(score / 100.0)
        assert g.entry_type == "limit" and g.cancel_price == pytest.approx(tp1)
        assert g.expire_s == 12 * 900 and g.sleeve == "cfe"


def test_no_lookahead_future_bars_do_not_change_past_values():
    from argus.cfe.adapter import run
    m15 = np.array(DATA["PF_SOLUSD"]["m15"])
    h1 = np.array(DATA["PF_SOLUSD"]["h1"])
    p = default_params()
    i = len(m15) - 700
    closed_h1 = h1[h1[:, 0] + 3600 <= m15[i, 0] + CHART_SEC]
    cut = run(m15[:i + 1], closed_h1, p)
    later = run(m15[:i + 401], h1[h1[:, 0] + 3600 <= m15[i + 400, 0] + CHART_SEC], p)
    j = len(later["c"]) - 401          # bar i inside the later (window) run
    for key in ("score_l", "score_s", "htf_align", "sig_l", "sig_s", "entry_l", "sl_l"):
        a, b = np.asarray(cut[key])[-1], np.asarray(later[key])[j]
        assert (a == b) or (np.isnan(a) and np.isnan(b)) or np.isclose(a, b), key


def test_cfe_evaluates_once_per_closed_15m_bar_and_waits_for_data():
    m15 = np.array(DATA["PF_ETHUSD"]["m15"])
    h1 = np.array(DATA["PF_ETHUSD"]["h1"])
    s = CfeStrategy()
    c15 = _candles(m15, Timeframe.M15)
    c1h = _candles(h1, Timeframe.H1)
    now = int(m15[-1][0]) + CHART_SEC
    ctx = MarketContext(symbol="ETH/USD", now_ts=now + 300, candles={}, features={},
                        regime=None, extras={})
    calls = {"n": 0}

    def m15_loader():
        calls["n"] += 1
        return c15[-2000:-1]                         # last bar not stored yet
    ctx.extras.update(m15_loader=m15_loader, h1_loader=lambda: c1h)
    s.evaluate(ctx)
    assert "ETH/USD" not in s.last                    # waits for the bar
    ctx2 = MarketContext(symbol="ETH/USD", now_ts=now + 300, candles={}, features={},
                         regime=None, extras={"m15_loader": lambda: c15[-2000:],
                                              "h1_loader": lambda: c1h})
    s.evaluate(ctx2)
    assert s.last["ETH/USD"]["ts"] == now
    n = calls["n"]
    s.evaluate(ctx)                                    # same bar again: no work
    assert calls["n"] == n


# --- execution: limit retest, cancel at TP1, thirds, break-even, lock -------------

def _exec(tmp_path):
    from argus.core.events import EventBus
    from argus.db.database import Database
    from argus.monitoring.health import HealthMonitor
    from argus.risk import RiskLimits, RiskSupervisor
    from argus.trading.broker import PaperBroker
    from argus.trading.executor import ExecutionService
    from argus.trading.portfolio import Portfolio
    db = Database(tmp_path / "c.db")
    bus = EventBus()
    broker = PaperBroker(fee_pct=0.05, slippage_pct=0.0, maker_fee_pct=0.02)
    pf = Portfolio(db, broker_name="paper", mode="paper", start_equity=10_000.0)
    ex = ExecutionService(db, bus, HealthMonitor(bus), broker, pf,
                          RiskSupervisor(RiskLimits(allow_short=True)))
    ex.state = "running"
    return ex, broker, pf


def _cfe_decision(entry=100.0, sl=98.0, qty=3.0):
    from argus.strategy.base import Signal
    tps = (entry + 2.0, entry + 4.0, entry + 6.0)            # 1R / 2R / 3R
    s = Signal("cfe", "ETH/USD", "LONG", entry, sl, tps[2], 0.8, ["x"],
               timeframe=Timeframe.M15, exploration=True, sleeve="cfe", expire_s=12 * 900,
               tps=tps, entry_type="limit", cancel_price=tps[0])
    return {"best": {"signal": s.as_dict()},
            "risk": {"approved": True, "quantity": qty, "leverage": 3.0, "risk_pct": 0.5}}


def test_cfe_limit_is_cancelled_when_tp1_comes_first(tmp_path):
    ex, broker, pf = _exec(tmp_path)
    broker.on_tick("ETH/USD", 101.0)
    ex._maybe_enter(_cfe_decision())
    assert len(broker.pending_orders()) == 3 and not pf.positions
    assert all(o.quantity == pytest.approx(1.0) for o in broker.pending_orders())
    ex._maybe_enter(_cfe_decision())                          # same coin: already planned
    assert len(broker.pending_orders()) == 3
    broker.on_tick("ETH/USD", 100.0)                          # touch only: no fill
    assert not pf.positions
    broker.on_tick("ETH/USD", 102.1)                          # TP1 before the retest
    assert broker.pending_orders() == [] and not pf.positions


def test_cfe_thirds_break_even_after_tp1_and_lock_after_tp2(tmp_path):
    ex, broker, pf = _exec(tmp_path)
    broker.on_tick("ETH/USD", 101.0)
    ex._maybe_enter(_cfe_decision())
    broker.on_tick("ETH/USD", 99.9)                           # traded through the limit
    legs = sorted(pf.positions.values(), key=lambda p: p.take_profit)
    assert [p.take_profit for p in legs] == [102.0, 104.0, 106.0]
    assert all(p.entry_price == 100.0 and p.stop_loss == 98.0 for p in legs)
    broker.on_tick("ETH/USD", 102.0)                          # TP1
    rest = sorted(pf.positions.values(), key=lambda p: p.take_profit)
    assert len(rest) == 2 and all(p.stop_loss == 100.0 for p in rest)   # break-even
    broker.on_tick("ETH/USD", 104.0)                          # TP2
    last = list(pf.positions.values())
    assert len(last) == 1 and last[0].stop_loss == 102.0      # locked at TP1
    broker.on_tick("ETH/USD", 101.9)                          # pullback: out at TP1
    assert not pf.positions
    reasons = sorted(t["exit_reason"] for t in pf.trade_history())
    assert reasons == ["stop_loss", "take_profit", "take_profit"]
    assert sum(t["pl"] for t in pf.trade_history()) > 0
    assert ex._groups == {} and ex._group_of == {}           # bookkeeping released


def test_cfe_has_its_own_switch(tmp_path):
    from argus.decision import DecisionEngine
    from argus.risk import AccountState, RiskLimits, RiskSupervisor
    from argus.strategy.base import Signal, Strategy

    class Fixed(Strategy):
        name, exploration, any_coin, gate = "cfe", True, True, "cfe"

        def evaluate(self, c):
            return Signal("cfe", "ETH/USD", "LONG", 100.0, 98.0, 106.0, 0.8, [],
                          timeframe=Timeframe.M15, exploration=True, sleeve="cfe",
                          tps=(102.0, 104.0, 106.0))
    eng = DecisionEngine(RiskSupervisor(RiskLimits(allow_short=True, risk_per_trade_pct=2.0,
                                                   round_trip_cost_pct=0.09)),
                         strategies=[Fixed()], enter_threshold=99)
    eng.active_enabled = True                                  # the trend switch is not CFE's
    acc = AccountState(equity=10_000.0, day_start_equity=10_000.0)
    ctx = MarketContext(symbol="ETH/USD", now_ts=0, candles={}, features={},
                        regime=None, extras={})
    assert eng.evaluate(ctx, acc).verdict == "no_signal"
    eng.cfe_enabled, eng.cfe_risk_pct = True, 0.5
    res = eng.evaluate(ctx, acc)
    assert res.verdict == "enter" and res.risk.risk_pct == pytest.approx(0.5)
