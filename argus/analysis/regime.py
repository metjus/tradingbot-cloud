"""Market regime detection - rule-based v1.

Phase 5 adds an ML regime classifier that is validated against this
baseline; the rest of the system only depends on the Regime enum, so the
implementation can be swapped without touching strategies.
"""

from __future__ import annotations

from enum import Enum

from ..ta.features import FeatureSet


class Regime(str, Enum):
    STRONG_UPTREND = "strong_uptrend"
    STRONG_DOWNTREND = "strong_downtrend"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    BREAKOUT_SETUP = "breakout_setup"
    UNCERTAIN = "uncertain"


def detect_regime(f15: FeatureSet | None, f1h: FeatureSet | None) -> Regime:
    """Classify from 15m features with 1h confirmation."""
    f = f15 or f1h
    if f is None or f.adx14 is None or f.ema21 is None:
        return Regime.UNCERTAIN

    atr_pct = (f.atr14 / f.close * 100.0) if (f.atr14 and f.close) else None

    # extreme volatility dominates everything else
    if atr_pct is not None and atr_pct > 1.8:
        return Regime.HIGH_VOLATILITY

    up_align = f.ema9 is not None and f.ema21 is not None and f.ema9 > f.ema21 \
        and (f.ema50 is None or f.ema21 > f.ema50)
    down_align = f.ema9 is not None and f.ema21 is not None and f.ema9 < f.ema21 \
        and (f.ema50 is None or f.ema21 < f.ema50)

    conf_up = True
    conf_down = True
    if f1h is not None and f1h.ema21 is not None:
        conf_up = f1h.close >= f1h.ema21
        conf_down = f1h.close <= f1h.ema21

    if f.adx14 >= 25 and up_align and f.plus_di is not None \
            and f.minus_di is not None and f.plus_di > f.minus_di and conf_up:
        return Regime.STRONG_UPTREND
    if f.adx14 >= 25 and down_align and f.plus_di is not None \
            and f.minus_di is not None and f.minus_di > f.plus_di and conf_down:
        return Regime.STRONG_DOWNTREND

    # tight range: squeeze -> breakout conditions forming
    if f.bb_width_pctile is not None and f.bb_width_pctile <= 20 and f.adx14 < 20:
        return Regime.BREAKOUT_SETUP

    if f.adx14 < 20:
        if atr_pct is not None and atr_pct < 0.25:
            return Regime.LOW_VOLATILITY
        return Regime.SIDEWAYS

    return Regime.UNCERTAIN
