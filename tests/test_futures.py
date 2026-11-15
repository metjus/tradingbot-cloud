"""Futures trading: leverage, margin, liquidation, funding, shorts,
coin universe and the 4h strategies."""

import math
import time

from argus.marketdata.kraken_futures import from_pf, to_pf
from argus.marketdata.model import Candle, Timeframe
from argus.risk import AccountState, RiskLimits, RiskSupervisor
from argus.strategy.base import MarketContext, Signal
from argus.strategy.futures import Donchian4h, TrendRider4h
from argus.ta.features import compute_features
from argus.trading.broker import BracketOrder, PaperBroker

H1, H4 = Timeframe.H1, Timeframe.H4


def limits(**kw):
    base = dict(round_trip_cost_pct=0.14, allow_short=True, leverage=3.0,
                max_leverage=10.0, maintenance_margin_pct=5.0, min_rr=1.3)
    base.update(kw)
    return RiskLimits(**base)


def sig(direction="LONG", entry=100.0, stop=98.0, tp=112.0):
    return Signal("trend_rider_4h", "BTC/USD", direction, entry, stop, tp,
                  0.7, [], timeframe=H4)


def account(**kw):
    base = dict(equity=10_000.0, day_start_equity=10_000.0)
    base.update(kw)
    return AccountState(**base)


# --- symbol mapping ---------------------------------------------------------

def test_symbol_mapping_roundtrip():
    assert to_pf("BTC/USD") == "PF_XBTUSD"
    assert from_pf("PF_XBTUSD") == "BTC/USD"
    assert to_pf("SOL/USD") == "PF_SOLUSD"
    assert from_pf("PF_SOLUSD") == "SOL/USD"


# --- risk: leverage, margin, liquidation guard ------------------------------

def test_leverage_lowers_margin_not_risk():
    rs = RiskSupervisor(limits())
    v1 = RiskSupervisor(limits(leverage=1.0)).check(sig(), account())
    v3 = rs.check(sig(), account())
    assert v1.approved and v3.approved
    # same risk amount (same stop distance, same risk %)
    assert math.isclose(v1.risk_amount, v3.risk_amount, rel_tol=1e-9)
    assert math.isclose(v3.margin * 3, v3.notional, rel_tol=1e-9)
    assert v3.margin < v1.margin


def test_short_allowed_on_futures():
    v = RiskSupervisor(limits()).check(sig("SHORT", 100.0, 102.0, 88.0), account())
    assert v.approved, v.vetoes
    assert v.liq_price is not None and v.liq_price > 102.0


def test_liquidation_guard_blocks_wide_stops_at_high_leverage():
    # 10x: liquidation ~5% away; a 4.5% stop must be vetoed
    v = RiskSupervisor(limits(leverage=10.0)).check(
        sig(entry=100.0, stop=95.5, tp=120.0), account())
    assert not v.approved
    assert any("likvida" in x for x in v.vetoes)
    # 3x: liquidation ~28% away; the same stop is fine
    v = RiskSupervisor(limits(leverage=3.0)).check(
        sig(entry=100.0, stop=95.5, tp=120.0), account())
    assert v.approved, v.vetoes


def test_correlation_cap_vetoes_stacked_direction():
    rs = RiskSupervisor(limits(max_aligned_positions=2))
    ok = rs.check(sig(), account(open_long=1, open_positions=1))
    assert ok.approved
    v = rs.check(sig(), account(open_long=2, open_positions=2))
    assert not v.approved and any("smere LONG" in x for x in v.vetoes)
    # opposite direction is not blocked by the cap
    v2 = rs.check(sig("SHORT", 100.0, 102.0, 88.0),
                  account(open_long=2, open_positions=2))
    assert v2.approved, v2.vetoes


def test_budget_binds_margin_not_notional():
    # budget 1000, leverage 5 -> notional may reach 5000
    rs = RiskSupervisor(limits(leverage=5.0, budget_eur=1000.0,
                               max_position_eur=1000.0, risk_per_trade_pct=1.0))
    v = rs.check(sig(entry=100.0, stop=99.9, tp=101.0), account())
    assert v.approved, v.vetoes
    assert v.notional <= 5000.0 + 1e-6
    assert v.margin <= 1000.0 + 1e-6


# --- paper broker: shorts, liquidation, funding -----------------------------

def make_broker(**kw):
    t = {"now": 1000.0}
    b = PaperBroker(fee_pct=0.05, slippage_pct=0.0,
                    clock=lambda: t["now"], **kw)
    fills = []
    b.on_fill = fills.append
    return b, fills, t


def test_short_position_profits_when_price_falls():
    b, fills, t = make_broker()
    b.on_tick("ETH/USD", 2000.0)
    b.submit_bracket(BracketOrder("c1", "ETH/USD", "SHORT", 1.0,
                                  stop_loss=2100.0, take_profit=1800.0))
    assert fills[0].kind == "entry"
    b.on_tick("ETH/USD", 1800.0, 2000.0)
    assert fills[-1].kind == "take_profit" and fills[-1].price == 1800.0


def test_liquidation_fires_when_price_passes_liq_level():
    b, fills, t = make_broker(maintenance_margin_pct=5.0)
    b.on_tick("ETH/USD", 1000.0)
    b.submit_bracket(BracketOrder("c1", "ETH/USD", "LONG", 1.0,
                                  stop_loss=1.0,          # absurdly wide stop
                                  take_profit=5000.0, leverage=10.0))
    pos = b.positions()[0]
    assert pos.liq_price is not None and math.isclose(pos.liq_price, 950.0)
    b.on_tick("ETH/USD", 940.0, 2000.0)
    assert fills[-1].kind == "liquidation"


def test_funding_accrues_and_settles_with_exit():
    b, fills, t = make_broker()
    b.funding_provider = lambda sym: 0.0001       # 0.01 %/h, long pays
    b.on_tick("BTC/USD", 100.0)
    b.submit_bracket(BracketOrder("c1", "BTC/USD", "LONG", 10.0,
                                  stop_loss=90.0, take_profit=200.0))
    t["now"] += 3600.0
    b.on_tick("BTC/USD", 100.0, t["now"])          # 1 hour passes
    pos = b.positions()[0]
    assert math.isclose(pos.funding_accrued, 0.0001 * 1000.0, rel_tol=1e-6)
    b.close_position("c1")
    # exit fee includes the funding
    assert fills[-1].fee >= 0.1


# --- 4h strategies ----------------------------------------------------------

def h4_series(n, trend=1.0, start=100.0):
    out, price = [], start
    for i in range(n):
        o = price
        c = price + trend * (0.4 + 0.1 * (i % 3))
        h = max(o, c) + 0.3
        lo = min(o, c) - 0.3
        out.append(Candle("SOL/USD", H4, 1_700_000_000 + i * 14400,
                          o, h, lo, c, 100.0, complete=True))
        price = c
    return out


def ctx_for(bars, extras=None):
    fs = compute_features(bars)
    return MarketContext(symbol="SOL/USD", now_ts=bars[-1].ts + 14400,
                         candles={H4: bars}, features={H4: fs},
                         regime=None, extras=extras or {})


def dip_recovery_series():
    """Long 4h uptrend, a sharp dip, then a strong recovery: EMA21 crosses
    below and back above EMA50 while price holds above EMA200."""
    bars = h4_series(200, trend=0.6)
    price = bars[-1].close
    for i in range(60):
        tr = -2.2 if i < 16 else 2.4
        o = price
        c = price + tr
        bars.append(Candle("SOL/USD", H4, bars[-1].ts + 14400,
                           o, max(o, c) + 0.3, min(o, c) - 0.3, c, 100.0,
                           complete=True))
        price = c
    return bars


def test_trend_rider_fires_once_per_h4_bar_and_respects_btc_filter():
    bars = dip_recovery_series()
    strat = TrendRider4h()
    fired = []
    for i in range(210, len(bars)):
        window = bars[: i + 1]
        s = strat.evaluate(ctx_for(window))
        if s:
            fired.append((i, s.direction))
            assert s.valid() and s.stop_loss < s.entry < s.take_profit
            # the same bar never fires twice
            assert strat.evaluate(ctx_for(window)) is None
    assert [d for _, d in fired] == ["LONG"]

    # with BTC pointing down the same cross is suppressed
    strat2 = TrendRider4h()
    blocked = [strat2.evaluate(ctx_for(bars[: i + 1], {"btc_trend": -1}))
               for i in range(210, len(bars))]
    assert not any(s and s.direction == "LONG" for s in blocked)


def test_donchian_breakout_signal_valid():
    bars = h4_series(230, trend=0.2)
    last = bars[-1]
    bars[-1] = Candle("SOL/USD", H4, last.ts, last.open,
                      last.close + 30, last.low, last.close + 28, 500.0,
                      complete=True)
    s = Donchian4h().evaluate(ctx_for(bars))
    assert s is not None and s.direction == "LONG" and s.valid()
    assert s.stop_loss < s.entry < s.take_profit
