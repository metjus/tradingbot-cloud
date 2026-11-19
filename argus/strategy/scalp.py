"""Rýchle obchody (scalp mode, paper experiment).

Many small trades on closed 1m bars of the most liquid perpetuals:

  * entry: a resting LIMIT (maker 0.02 %) at the signal level, valid a few
    minutes; it fills only when price comes back to the level
  * take-profit: a LIMIT (maker) placed so a win nets the target in EUR
    after both fees; the target grows with the capital (100 % of the
    profit is reinvested, the position is a share of the current capital)
  * stop-loss: market (taker 0.05 % + slippage); time stop after N minutes
  * several positions at once, one per coin, each with capital / slots as
    margin times the leverage

Two entry types (research on 400 days of Kraken Futures 1m data):

  breakout        close breaks the 15-bar high/low on 1.5x volume ->
                  limit at the broken level (retest)
  mean_reversion  close 2 standard deviations away from the 60-bar mean ->
                  limit at the close (fade the stretch)

Research verdict (400 days, 10 coins, exact Kraken fees): NO variant made
money after fees - the minute moves these entries catch are smaller than
the costs. It runs because the user wants to watch it on paper; it must
never trade real money without an explicit decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..marketdata.model import Candle

KINDS = ("breakout", "mean_reversion")
BREAKOUT_BARS = 15
BREAKOUT_VOL_K = 1.5
VOL_EMA = 60
MR_BARS = 60
MR_K = 2.0
#: 1m bars the signal needs (EMA warm-up included)
NEED_BARS = 300

MAKER_PCT = 0.02          # Kraken Futures maker fee per side, %


@dataclass(slots=True)
class ScalpSignal:
    direction: str        # LONG | SHORT
    level: float          # limit entry price
    reason: str


def _ema(values: list[float], n: int) -> list[float]:
    a = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def signal(bars: list[Candle], kind: str = "breakout") -> ScalpSignal | None:
    """Signal on the LAST bar of ``bars`` (closed 1m bars, oldest first)."""
    if len(bars) < max(BREAKOUT_BARS, MR_BARS) + 2:
        return None
    last = bars[-1]
    if kind == "mean_reversion":
        closes = [b.close for b in bars[-MR_BARS:]]
        m = sum(closes) / MR_BARS
        sd = math.sqrt(max(sum(c * c for c in closes) / MR_BARS - m * m, 0.0))
        if sd <= 0:
            return None
        z = (last.close - m) / sd
        if z <= -MR_K:
            return ScalpSignal("LONG", last.close,
                               f"cena {abs(z):.1f} smerodajnej odchýlky pod 60-min priemerom")
        if z >= MR_K:
            return ScalpSignal("SHORT", last.close,
                               f"cena {z:.1f} smerodajnej odchýlky nad 60-min priemerom")
        return None
    prev = bars[-BREAKOUT_BARS - 1:-1]
    hi = max(b.high for b in prev)
    lo = min(b.low for b in prev)
    vavg = _ema([b.volume for b in bars[:-1]], VOL_EMA)[-1]
    if vavg <= 0:
        return None
    # the EMA in the research includes the signal bar itself
    vavg = (2.0 / (VOL_EMA + 1)) * last.volume + (1 - 2.0 / (VOL_EMA + 1)) * vavg
    if last.volume <= BREAKOUT_VOL_K * vavg:
        return None
    if last.close > hi:
        return ScalpSignal("LONG", hi, f"prerazenie 15-min maxima s {last.volume / vavg:.1f}× objemom"
                                       f" - limit na prerazenej úrovni")
    if last.close < lo:
        return ScalpSignal("SHORT", lo, f"prerazenie 15-min minima s {last.volume / vavg:.1f}× objemom"
                                        f" - limit na prerazenej úrovni")
    return None


def take_profit_pct(target_eur: float, notional: float) -> float:
    """Price move (%) so a win nets ``target_eur`` after the maker fee on
    the entry AND on the take-profit."""
    if notional <= 0:
        return 0.0
    return target_eur / notional * 100.0 + 2 * MAKER_PCT


def levels(sig: ScalpSignal, tp_pct: float, sl_pct: float) -> tuple[float, float]:
    """(stop_loss, take_profit) around the limit level."""
    s = 1.0 if sig.direction == "LONG" else -1.0
    return (sig.level * (1 - s * sl_pct / 100.0), sig.level * (1 + s * tp_pct / 100.0))
