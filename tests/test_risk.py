import math

import pytest

from argus.marketdata.model import Timeframe
from argus.risk import AccountState, RiskLimits, RiskSupervisor
from argus.strategy.base import Signal


def sig(entry=100.0, sl=99.0, tp=102.0, direction="LONG") -> Signal:
    return Signal("test", "BTC/USD", direction, entry, sl, tp, 0.8, ["r"], Timeframe.M5)


def account(**kw) -> AccountState:
    base = dict(equity=10_000.0, day_start_equity=10_000.0)
    base.update(kw)
    return AccountState(**base)


def test_position_sizing_exact():
    sup = RiskSupervisor(RiskLimits(risk_per_trade_pct=0.5, max_position_notional_pct=100.0))
    v = sup.check(sig(entry=100, sl=99, tp=102), account())
    assert v.approved
    assert math.isclose(v.risk_amount, 50.0)   # 0.5% of 10k
    assert math.isclose(v.quantity, 50.0)      # 50 EUR / 1 EUR per unit


def test_default_notional_cap_bites_on_tight_stop():
    # default cap 25% of equity: qty limited to 2500/100 = 25 units
    v = RiskSupervisor(RiskLimits(risk_per_trade_pct=0.5)).check(
        sig(entry=100, sl=99, tp=102), account())
    assert v.approved
    assert math.isclose(v.quantity, 25.0)
    assert math.isclose(v.risk_amount, 25.0)  # actual risk shrinks with qty


def test_risk_cannot_exceed_hard_ceiling():
    sup = RiskSupervisor(RiskLimits(risk_per_trade_pct=5.0, max_risk_per_trade_pct=1.0))
    v = sup.check(sig(), account())
    assert v.approved
    assert v.risk_pct == 1.0


def test_daily_loss_limit_blocks_new_trades():
    sup = RiskSupervisor()
    v = sup.check(sig(), account(equity=9_790.0))  # -2.1% today
    assert not v.approved
    assert any("daily loss limit" in x for x in v.vetoes)


def test_max_positions_and_daily_trades():
    sup = RiskSupervisor(RiskLimits(max_open_positions=2, max_daily_trades=3))
    assert not sup.check(sig(), account(open_positions=2)).approved
    assert not sup.check(sig(), account(trades_today=3)).approved


def test_invalid_sl_vetoed():
    bad = sig(entry=100, sl=101, tp=102)  # SL above entry on LONG
    v = RiskSupervisor().check(bad, account())
    assert not v.approved
    assert any("invalid signal" in x for x in v.vetoes)


def test_low_rr_vetoed():
    v = RiskSupervisor(RiskLimits(min_rr=1.5)).check(sig(tp=101.0), account())  # rr 1.0
    assert not v.approved
    assert any("risk/reward" in x for x in v.vetoes)


def test_wide_spread_vetoed():
    v = RiskSupervisor().check(sig(), account(), spread_pct=0.5)
    assert not v.approved
    assert any("spread" in x for x in v.vetoes)


def test_halt_blocks_everything():
    sup = RiskSupervisor()
    sup.halt("emergency stop")
    v = sup.check(sig(), account())
    assert not v.approved
    sup.resume()
    assert sup.check(sig(), account()).approved


def test_open_risk_budget():
    sup = RiskSupervisor(RiskLimits(risk_per_trade_pct=0.5, daily_max_loss_pct=2.0))
    v = sup.check(sig(), account(open_risk_pct=1.8))
    assert not v.approved
    assert any("daily budget" in x for x in v.vetoes)


def test_notional_cap_shrinks_quantity():
    # SL extremely tight -> raw qty would be huge; notional cap must bite
    sup = RiskSupervisor(RiskLimits(max_position_notional_pct=25.0))
    v = sup.check(sig(entry=100, sl=99.99, tp=100.5), account())
    assert v.approved
    assert v.quantity * 100 <= 10_000 * 0.25 + 1e-6


def test_short_sizing():
    v = RiskSupervisor(RiskLimits(max_position_notional_pct=100.0)).check(
        sig(entry=100, sl=101, tp=97.5, direction="SHORT"), account())
    assert v.approved
    assert math.isclose(v.quantity, 50.0)
