"""Futures strategies on higher timeframes (1h/4h), validated on 2 years
of Kraken Futures data NET of futures costs (taker 0.05 %/side, slippage,
funding). The 5m intraday strategies lose after costs on every tested
venue; these trade far less often and hold for days.

Research summary (15 liquid perps, 2024-11 .. 2026-11, out-of-sample =
second year):
  * EMA 21/50 cross on 4h + trend & BTC-direction filter + ATR trailing:
    in-sample PF 1.45, out-of-sample PF 1.09-1.14 across the whole
    parameter neighbourhood (trail 3-4 ATR, stop 1.5-3 ATR) - the only
    family profitable in BOTH halves. Longs carry it (PF 1.79); shorts
    alone lose (PF 0.77), so the per-coin selector prunes them where
    they lose.
  * Donchian 55-bar 4h breakout: positive in-sample, flat out-of-sample -
    shipped but gated by the same selector.
  * Everything on 15m/1h and mean-reversion (RSI2): negative after costs.

Both strategies fire at most once per closed 4h bar and rely on the
adaptive exit engine (trailing stop, give-back, volume-fade) instead of a
fixed take-profit: the TP sits at 20R, far enough to never cut a boom
short (the best validated trade reached +15.6R; capping at 6R would have
cost about a tenth of all profit). The market, not greed, ends the trade.
"""

from __future__ import annotations

from ..analysis.regime import Regime
from ..marketdata.model import Timeframe
from ..ta import indicators as ind
from .base import MarketContext, Signal, Strategy

M5, H1, H4 = Timeframe.M5, Timeframe.H1, Timeframe.H4
TP_CAP_R = 20.0


def _price(ctx: MarketContext) -> float | None:
    f = ctx.f(M5) or ctx.f(H1) or ctx.f(H4)
    return f.close if f else None


def _btc_ok(ctx: MarketContext, direction: str) -> bool:
    """Altcoins follow BTC: longs only while BTC is above its 4h EMA200,
    shorts only below. Missing info (BTC itself) = allowed."""
    t = ctx.extras.get("btc_trend")
    if t is None:
        return True
    return t >= 0 if direction == "LONG" else t <= 0


class _H4Strategy(Strategy):
    """Shared gate: evaluate once per closed 4h bar."""

    def __init__(self) -> None:
        self._last_h4: dict[str, int] = {}

    def _new_h4(self, ctx: MarketContext) -> list | None:
        bars = ctx.candles.get(H4) or []
        if len(bars) < 60:
            return None
        if self._last_h4.get(ctx.symbol) == bars[-1].ts:
            return None
        self._last_h4[ctx.symbol] = bars[-1].ts
        return bars


class TrendRider4h(_H4Strategy):
    """EMA 21/50 cross on 4h, in the direction of the 4h EMA200 trend and
    of BTC, ridden with a trailing stop."""

    name = "trend_rider_4h"
    suited_regimes = (Regime.STRONG_UPTREND, Regime.STRONG_DOWNTREND,
                      Regime.BREAKOUT_SETUP, Regime.SIDEWAYS, Regime.UNCERTAIN)
    atr_stop = 2.0

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        bars = self._new_h4(ctx)
        f4 = ctx.f(H4)
        if bars is None or f4 is None or f4.atr14 is None or f4.ema200 is None:
            return None
        closes = [b.close for b in bars]
        e21 = ind.ema(closes, 21)
        e50 = ind.ema(closes, 50)
        if not e21 or not e50 or e21[-2] is None or e50[-2] is None:
            return None
        now_up = e21[-1] > e50[-1]
        was_up = e21[-2] > e50[-2]
        if now_up == was_up:
            return None                       # no fresh cross on this 4h bar
        d = "LONG" if now_up else "SHORT"
        price = _price(ctx)
        if price is None or not _btc_ok(ctx, d):
            return None
        if d == "LONG" and price <= f4.ema200:
            return None
        if d == "SHORT" and price >= f4.ema200:
            return None
        stop = price - self.atr_stop * f4.atr14 if d == "LONG" \
            else price + self.atr_stop * f4.atr14
        tp = self._tp_from_rr(price, stop, TP_CAP_R, d)
        reasons = [f"4h EMA21/50 cross {d}", "smer potvrdený 4h EMA200",
                   "BTC filter OK", "trailing stop vedie pozíciu"]
        sig = Signal(self.name, ctx.symbol, d, price, stop, tp, 0.7, reasons,
                     timeframe=H4)
        return sig if sig.valid() else None


class Donchian4h(_H4Strategy):
    """Close beyond the 55-bar 4h channel in the EMA200 + BTC direction."""

    name = "donchian_4h"
    suited_regimes = (Regime.STRONG_UPTREND, Regime.STRONG_DOWNTREND,
                      Regime.BREAKOUT_SETUP, Regime.HIGH_VOLATILITY)
    channel = 55
    atr_stop = 2.0

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        bars = self._new_h4(ctx)
        f4 = ctx.f(H4)
        if bars is None or len(bars) < self.channel + 2 or f4 is None \
                or f4.atr14 is None or f4.ema200 is None:
            return None
        prior = bars[-(self.channel + 1):-1]
        hh = max(b.high for b in prior)
        ll = min(b.low for b in prior)
        price = bars[-1].close
        if price > hh:
            d = "LONG"
        elif price < ll:
            d = "SHORT"
        else:
            return None
        if not _btc_ok(ctx, d):
            return None
        if d == "LONG" and price <= f4.ema200:
            return None
        if d == "SHORT" and price >= f4.ema200:
            return None
        stop = price - self.atr_stop * f4.atr14 if d == "LONG" \
            else price + self.atr_stop * f4.atr14
        tp = self._tp_from_rr(price, stop, TP_CAP_R, d)
        sig = Signal(self.name, ctx.symbol, d, price, stop, tp, 0.6, [
            f"4h close za 55-bar {'maximom' if d == 'LONG' else 'minimom'}",
            "smer potvrdený 4h EMA200", "BTC filter OK"], timeframe=H4)
        return sig if sig.valid() else None


#: Donchian4h is kept for research but NOT traded: with the full live stack
#: (maker entries, volume exit, no TP cap) it measured IS PF 0.97 / OOS PF
#: 1.03 - more trades, no edge, extra drawdown.
FUTURES_STRATEGIES: list[Strategy] = [TrendRider4h()]
