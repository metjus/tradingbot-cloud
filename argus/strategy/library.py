"""The eight baseline strategies (§7 of the spec).

Every strategy: works on closed 5m bars with 15m/1h context, always
defines SL and TP, reports its reasons. Parameters are class attributes
so the backtester (phase 7) can sweep them; values below are sane
starting points, not optimised claims.
"""

from __future__ import annotations

from ..analysis.regime import Regime
from ..marketdata.model import Timeframe
from .base import MarketContext, Signal, Strategy

M5, M15, H1 = Timeframe.M5, Timeframe.M15, Timeframe.H1


def _trend_dir(ctx: MarketContext) -> str | None:
    """LONG/SHORT when 15m trend is aligned, else None."""
    f = ctx.f(M15)
    if f is None or f.ema9 is None or f.ema21 is None or f.adx14 is None:
        return None
    if f.adx14 >= 22 and f.ema9 > f.ema21 and (f.ema50 is None or f.close > f.ema50):
        return "LONG"
    if f.adx14 >= 22 and f.ema9 < f.ema21 and (f.ema50 is None or f.close < f.ema50):
        return "SHORT"
    return None


class TrendFollowing(Strategy):
    name = "trend_following"
    suited_regimes = (Regime.STRONG_UPTREND, Regime.STRONG_DOWNTREND)
    rr = 2.0
    atr_stop = 1.5

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        d = _trend_dir(ctx)
        f5 = ctx.f(M5)
        if d is None or f5 is None or f5.atr14 is None or f5.ema21 is None:
            return None
        price = f5.close
        # join the trend while price holds above/below its 5m EMA21
        if d == "LONG" and not price > f5.ema21:
            return None
        if d == "SHORT" and not price < f5.ema21:
            return None
        stop = price - self.atr_stop * f5.atr14 if d == "LONG" else price + self.atr_stop * f5.atr14
        tp = self._tp_from_rr(price, stop, self.rr, d)
        conf = 0.55
        reasons = [f"15m trend {d} (ADX {ctx.f(M15).adx14:.0f})", "price on trend side of 5m EMA21"]
        f1h = ctx.f(H1)
        if f1h is not None and f1h.ema21 is not None:
            if (d == "LONG") == (f1h.close > f1h.ema21):
                conf += 0.2
                reasons.append("1h confirms")
        if f5.vol_ratio is not None and f5.vol_ratio > 1.0:
            conf += 0.1
            reasons.append(f"volume {f5.vol_ratio:.1f}x avg")
        return Signal(self.name, ctx.symbol, d, price, stop, tp, min(conf, 0.95), reasons)


class Momentum(Strategy):
    name = "momentum"
    suited_regimes = (Regime.STRONG_UPTREND, Regime.STRONG_DOWNTREND, Regime.HIGH_VOLATILITY)
    rr = 1.8
    atr_stop = 1.2
    min_vol_ratio = 1.3

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        f5 = ctx.f(M5)
        if f5 is None or None in (f5.macd_hist, f5.macd_hist_prev, f5.atr14, f5.rsi14):
            return None
        if f5.vol_ratio is None or f5.vol_ratio < self.min_vol_ratio:
            return None
        rising = f5.macd_hist > f5.macd_hist_prev
        price = f5.close
        if f5.macd_hist > 0 and rising and f5.rsi14 > 55 and f5.rsi14 < 78:
            d = "LONG"
        elif f5.macd_hist < 0 and not rising and f5.rsi14 < 45 and f5.rsi14 > 22:
            d = "SHORT"
        else:
            return None
        stop = price - self.atr_stop * f5.atr14 if d == "LONG" else price + self.atr_stop * f5.atr14
        tp = self._tp_from_rr(price, stop, self.rr, d)
        conf = 0.5 + min(0.25, (f5.vol_ratio - self.min_vol_ratio) * 0.15)
        return Signal(self.name, ctx.symbol, d, price, stop, tp, conf, [
            f"MACD hist {'rising' if rising else 'falling'} ({f5.macd_hist:.4g})",
            f"RSI {f5.rsi14:.0f}", f"volume {f5.vol_ratio:.1f}x avg"])


class Breakout(Strategy):
    name = "breakout"
    suited_regimes = (Regime.BREAKOUT_SETUP, Regime.SIDEWAYS)
    rr = 2.0
    min_vol_ratio = 1.5

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        f5 = ctx.f(M5)
        if f5 is None or None in (f5.high_20, f5.low_20, f5.atr14):
            return None
        if f5.vol_ratio is None or f5.vol_ratio < self.min_vol_ratio:
            return None
        price = f5.close
        rng = f5.high_20 - f5.low_20
        if rng <= 0:
            return None
        squeeze_bonus = 0.15 if (f5.bb_width_pctile is not None and f5.bb_width_pctile <= 25) else 0.0
        if price > f5.high_20:
            d, stop = "LONG", max(f5.high_20 - 0.25 * rng, price - 2.0 * f5.atr14)
        elif price < f5.low_20:
            d, stop = "SHORT", min(f5.low_20 + 0.25 * rng, price + 2.0 * f5.atr14)
        else:
            return None
        tp = self._tp_from_rr(price, stop, self.rr, d)
        return Signal(self.name, ctx.symbol, d, price, stop, tp, 0.5 + squeeze_bonus + 0.1, [
            f"close beyond 20-bar {'high' if d == 'LONG' else 'low'}",
            f"volume {f5.vol_ratio:.1f}x avg"] +
            (["BB squeeze release"] if squeeze_bonus else []))


class Pullback(Strategy):
    name = "pullback"
    suited_regimes = (Regime.STRONG_UPTREND, Regime.STRONG_DOWNTREND)
    rr = 2.2
    atr_stop = 1.2
    zone_atr = 0.5

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        d = _trend_dir(ctx)
        f5 = ctx.f(M5)
        if d is None or f5 is None or None in (f5.ema21, f5.atr14, f5.rsi14):
            return None
        price = f5.close
        near_ema = abs(price - f5.ema21) <= self.zone_atr * f5.atr14
        if not near_ema:
            return None
        bars = ctx.candles.get(M5) or []
        if len(bars) < 2:
            return None
        last = bars[-1]
        if d == "LONG":
            rejection = last.close > last.open and (last.close - last.low) > 0.6 * (last.high - last.low)
            if not (rejection and 35 <= f5.rsi14 <= 60):
                return None
            stop = min(last.low, price - self.atr_stop * f5.atr14)
        else:
            rejection = last.close < last.open and (last.high - last.close) > 0.6 * (last.high - last.low)
            if not (rejection and 40 <= f5.rsi14 <= 65):
                return None
            stop = max(last.high, price + self.atr_stop * f5.atr14)
        tp = self._tp_from_rr(price, stop, self.rr, d)
        return Signal(self.name, ctx.symbol, d, price, stop, tp, 0.65, [
            f"pullback to 5m EMA21 in 15m {d} trend", "rejection candle",
            f"RSI {f5.rsi14:.0f}"])


class VwapReversal(Strategy):
    name = "vwap_reversal"
    suited_regimes = (Regime.SIDEWAYS, Regime.LOW_VOLATILITY)
    stretch_atr = 2.0

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        f5 = ctx.f(M5)
        if f5 is None or None in (f5.vwap, f5.atr14, f5.rsi14):
            return None
        price = f5.close
        stretch = (price - f5.vwap) / f5.atr14 if f5.atr14 > 0 else 0.0
        if stretch >= self.stretch_atr and f5.rsi14 >= 70:
            d = "SHORT"
            stop = price + 1.0 * f5.atr14
        elif stretch <= -self.stretch_atr and f5.rsi14 <= 30:
            d = "LONG"
            stop = price - 1.0 * f5.atr14
        else:
            return None
        tp = f5.vwap  # revert to vwap
        sig = Signal(self.name, ctx.symbol, d, price, stop, tp, 0.55, [
            f"price {stretch:+.1f} ATR from VWAP", f"RSI {f5.rsi14:.0f} extreme"])
        return sig if sig.valid() and sig.rr >= 1.2 else None


class SupportResistance(Strategy):
    name = "support_resistance"
    suited_regimes = (Regime.SIDEWAYS, Regime.LOW_VOLATILITY)
    zone_atr = 0.35
    rr = 1.8

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        f5 = ctx.f(M5)
        f15 = ctx.f(M15)
        if f5 is None or f15 is None or None in (f15.high_20, f15.low_20, f5.atr14):
            return None
        price = f5.close
        bars = ctx.candles.get(M5) or []
        if not bars:
            return None
        last = bars[-1]
        support, resistance = f15.low_20, f15.high_20
        if abs(price - support) <= self.zone_atr * f5.atr14 and last.close > last.open:
            d, stop = "LONG", support - 0.8 * f5.atr14
        elif abs(price - resistance) <= self.zone_atr * f5.atr14 and last.close < last.open:
            d, stop = "SHORT", resistance + 0.8 * f5.atr14
        else:
            return None
        tp = self._tp_from_rr(price, stop, self.rr, d)
        return Signal(self.name, ctx.symbol, d, price, stop, tp, 0.5, [
            f"bounce at 15m {'support' if d == 'LONG' else 'resistance'}",
            "confirming candle"])


class VolatilityBreakout(Strategy):
    name = "volatility_breakout"
    suited_regimes = (Regime.BREAKOUT_SETUP, Regime.HIGH_VOLATILITY)
    k = 0.6
    rr = 1.6

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        f5 = ctx.f(M5)
        bars = ctx.candles.get(M5) or []
        if f5 is None or f5.atr14 is None or len(bars) < 3:
            return None
        prev, last = bars[-2], bars[-1]
        price = last.close
        thrust = self.k * f5.atr14
        if price > prev.high + thrust:
            d, stop = "LONG", prev.high - 0.5 * f5.atr14
        elif price < prev.low - thrust:
            d, stop = "SHORT", prev.low + 0.5 * f5.atr14
        else:
            return None
        if f5.vol_ratio is None or f5.vol_ratio < 1.2:
            return None
        tp = self._tp_from_rr(price, stop, self.rr, d)
        return Signal(self.name, ctx.symbol, d, price, stop, tp, 0.55, [
            f"close {self.k} ATR beyond previous bar {'high' if d == 'LONG' else 'low'}",
            f"volume {f5.vol_ratio:.1f}x avg"])


class MeanReversion(Strategy):
    name = "mean_reversion"
    suited_regimes = (Regime.SIDEWAYS, Regime.LOW_VOLATILITY)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        f5 = ctx.f(M5)
        f15 = ctx.f(M15)
        if f5 is None or None in (f5.bb_lower, f5.bb_upper, f5.bb_mid, f5.rsi14, f5.atr14):
            return None
        # only in non-trending conditions
        if f15 is not None and f15.adx14 is not None and f15.adx14 >= 25:
            return None
        price = f5.close
        if price <= f5.bb_lower and f5.rsi14 <= 30:
            d, stop = "LONG", price - 1.2 * f5.atr14
        elif price >= f5.bb_upper and f5.rsi14 >= 70:
            d, stop = "SHORT", price + 1.2 * f5.atr14
        else:
            return None
        tp = f5.bb_mid
        sig = Signal(self.name, ctx.symbol, d, price, stop, tp, 0.5, [
            f"close at BB {'lower' if d == 'LONG' else 'upper'} band",
            f"RSI {f5.rsi14:.0f}", "non-trending 15m (ADX < 25)"])
        return sig if sig.valid() and sig.rr >= 1.0 else None


ALL_STRATEGIES: list[Strategy] = [
    TrendFollowing(), Momentum(), Breakout(), Pullback(),
    VwapReversal(), SupportResistance(), VolatilityBreakout(), MeanReversion(),
]
