"""Trend portfolio (aktívny režim): 12 trend-following "flip" systems on
UTC-aligned 2h-8h bars, run on the ~20 most traded perpetuals.

Each system is always on one side of the market while its indicator
points there (Parabolic SAR, Supertrend, EMA cross, Donchian channel); when
the indicator flips, the position is closed at market and - if the
filter agrees - the opposite one is opened. A 3 ATR safety stop sits on
every position; there is no take-profit and no adaptive exit: the flip is
the exit, exactly as researched.

Research (2y Kraken Futures, 17 coins, taker entry AND exit, slippage,
funding; selection on year 1 only, judged on the unseen year 2):
  * 240 systems tested; the 12 below were chosen on year 1 alone (PF >= 1.4,
    one per indicator x timeframe). Year 2: every one of them PF > 1.1,
    together PF 1.29 on ~13 trades/day at 20 coins.
  * Portfolio (hourly mark-to-market, risk scaled by market volatility):
    Sharpe 1.15 (2y) / 1.05 (year 2). At 0.02 % risk per trade the
    10 000-year block bootstrap gives a median year of +20 %, worst 5 %
    -9..-15 %, losing year 15-20 %, median max drawdown 11-15 %.
  * Trades cluster (win rate ~36 %): 40 % of all 500-trade windows have
    PF < 1 even though the whole sample has PF 1.43 - judge it on months,
    not days.
"""

from __future__ import annotations

import numpy as np

from ..analysis.regime import Regime
from ..marketdata.model import Timeframe
from .base import MarketContext, Signal, Strategy

H1, H4 = Timeframe.H1, Timeframe.H4
STOP_ATR = 3.0
TP_CAP_R = 20.0
#: 1h candles needed for the slowest system (8h bars, EMA200 + warm-up)
HISTORY_H1 = 3600

#: (strategy name, symbol) -> current side of the system (+1 / -1). The
#: executor closes a position as soon as its system points the other way,
#: even when the filter does not allow the opposite entry.
FLIP_STATE: dict[tuple[str, str], int] = {}


# --- indicators (identical to the research code) ------------------------------

def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x)
    al = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = al * x[i] + (1 - al) * out[i - 1]
    return out


def _atr(h, l, c, n: int = 14) -> np.ndarray:
    tr = np.empty_like(c)
    tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = np.empty_like(tr)
    out[:n] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def psar_states(h, l, step: float, mx: float) -> np.ndarray:
    n = len(h)
    out = np.zeros(n)
    up, af, ep, s = True, step, h[0], l[0]
    for i in range(1, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < s:
                up, s, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(mx, af + step)
        else:
            s = max(s, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(mx, af + step)
        out[i] = 1 if up else -1
    return out


def supertrend_states(h, l, c, n: int, m: float) -> np.ndarray:
    a = _atr(h, l, c, n)
    out = np.zeros(len(c))
    fu = fl = 0.0
    d = 1
    for i in range(len(c)):
        hl = (h[i] + l[i]) / 2
        bu, bl = hl + m * a[i], hl - m * a[i]
        if i == 0:
            fu, fl = bu, bl
        else:
            fu = bu if (bu < fu or c[i - 1] > fu) else fu
            fl = bl if (bl > fl or c[i - 1] < fl) else fl
            if d == 1 and c[i] < fl:
                d = -1
            elif d == -1 and c[i] > fu:
                d = 1
        out[i] = d
    return out


def donchian_states(h, l, c, n: int) -> np.ndarray:
    out = np.zeros(len(c))
    d = 0
    for i in range(n, len(c)):
        if c[i] > h[i - n:i].max():
            d = 1
        elif c[i] < l[i - n:i].min():
            d = -1
        out[i] = d
    return out


def ema_states(c, fast: int, slow: int) -> np.ndarray:
    return np.where(_ema(c, fast) > _ema(c, slow), 1.0, -1.0)


# --- bars --------------------------------------------------------------------

def tf_bars(h1: list, tf_h: int, now_ts: int) -> dict | None:
    """Complete UTC-aligned tf_h-hour bars built from closed 1h candles."""
    rows = [b for b in h1 if b.ts + 3600 <= now_ts and getattr(b, "complete", True)]
    if not rows:
        return None
    if tf_h == 1:
        t = np.array([b.ts for b in rows], dtype=np.int64)
        return {"t": t, "o": np.array([b.open for b in rows]),
                "h": np.array([b.high for b in rows]), "l": np.array([b.low for b in rows]),
                "c": np.array([b.close for b in rows])}
    sec = tf_h * 3600
    t, o, h, l, c = [], [], [], [], []
    cur, grp = None, []
    for b in rows + [None]:
        k = None if b is None else b.ts // sec
        if grp and k != cur:
            if len(grp) >= tf_h:
                t.append(cur * sec)
                o.append(grp[0].open)
                h.append(max(x.high for x in grp))
                l.append(min(x.low for x in grp))
                c.append(grp[-1].close)
            grp = []
        if b is not None:
            cur = k
            grp.append(b)
    if not t:
        return None
    return {"t": np.array(t, dtype=np.int64), "o": np.array(o), "h": np.array(h),
            "l": np.array(l), "c": np.array(c)}


def _bars_for(ctx: MarketContext, tf_h: int) -> dict | None:
    """Per-evaluation cache: the long 1h history is loaded once per symbol
    and evaluation, and only when some system's bar has just closed."""
    cache = ctx.extras.setdefault("_tf_cache", {})
    if tf_h not in cache:
        h1 = cache.get("h1")
        if h1 is None:
            loader = ctx.extras.get("h1_loader")
            h1 = loader() if loader is not None else (ctx.candles.get(H1) or [])
            cache["h1"] = h1
        cache[tf_h] = tf_bars(h1, tf_h, int(ctx.now_ts))
    return cache[tf_h]


def _btc_ok(ctx: MarketContext, direction: str) -> bool:
    t = ctx.extras.get("btc_trend")
    if t is None:
        return True
    return t >= 0 if direction == "LONG" else t <= 0


def _price(ctx: MarketContext) -> float | None:
    for tf in (Timeframe.M5, H1, H4):
        f = ctx.f(tf)
        if f is not None:
            return f.close
    return None


IND_LABEL = {"psar": "Parabolic SAR", "st": "Supertrend", "ema": "EMA cross", "don": "Donchian"}
FILTER_LABEL = {0: "bez filtra", 1: "filter EMA200", 2: "filter BTC"}


class FlipSystem(Strategy):
    """One trend system: always on the side its indicator points to."""

    exploration = True
    any_coin = True
    flip_exit = True
    suited_regimes = tuple(Regime)

    def __init__(self, ind: str, params: tuple, tf_h: int, filt: int) -> None:
        self.ind, self.params, self.tf_h, self.filt = ind, tuple(params), tf_h, filt
        p = "_".join(f"{x:g}" for x in self.params)
        self.name = f"trend_{ind}{p}_{tf_h}h" + {0: "", 1: "_f", 2: "_btc"}[filt]
        self._done: dict[str, int] = {}

    @property
    def label(self) -> str:
        return (f"{IND_LABEL[self.ind]}({', '.join(f'{x:g}' for x in self.params)}) "
                f"{self.tf_h}h, {FILTER_LABEL[self.filt]}")

    def states(self, bars: dict) -> np.ndarray:
        h, l, c = bars["h"], bars["l"], bars["c"]
        if self.ind == "psar":
            return psar_states(h, l, *self.params)
        if self.ind == "st":
            return supertrend_states(h, l, c, int(self.params[0]), self.params[1])
        if self.ind == "ema":
            return ema_states(c, int(self.params[0]), int(self.params[1]))
        return donchian_states(h, l, c, int(self.params[0]))

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        sec = self.tf_h * 3600
        now = int(ctx.now_ts)
        end = now // sec * sec                       # end of the last elapsed bar
        if self._done.get(ctx.symbol) == end:
            return None
        bars = _bars_for(ctx, self.tf_h)
        if bars is None or len(bars["c"]) < 60:
            return None
        last_end = int(bars["t"][-1]) + sec
        if last_end != end:
            # the 1h candles of the bar that just closed are not stored yet:
            # retry on the next 5-min evaluation (give up after 30 min)
            if now - end > 1800:
                self._done[ctx.symbol] = end
            return None
        self._done[ctx.symbol] = end
        st = self.states(bars)
        FLIP_STATE[(self.name, ctx.symbol)] = int(st[-1])
        if st[-1] == 0 or st[-1] == st[-2]:
            return None                               # no flip on this bar
        if now - last_end > 3600:
            return None                               # stale flip (restart)
        d = "LONG" if st[-1] > 0 else "SHORT"
        c = bars["c"]
        if self.filt == 1:
            if len(c) < 210:
                return None
            if (d == "LONG") != (c[-1] > _ema(c, 200)[-1]):
                return None
        elif self.filt == 2 and not _btc_ok(ctx, d):
            return None
        a = _atr(bars["h"], bars["l"], c)[-1]
        price = _price(ctx) or float(c[-1])
        if a <= 0 or price <= 0:
            return None
        stop = price - STOP_ATR * a if d == "LONG" else price + STOP_ATR * a
        if stop <= 0:
            return None
        tp = self._tp_from_rr(price, stop, TP_CAP_R, d)
        sig = Signal(self.name, ctx.symbol, d, price, stop, tp, 0.5, [
            f"{self.label}: otočenie na {d}",
            "aktívny režim: výstup pri ďalšom otočení alebo na stope 3 ATR"],
            timeframe=H4 if self.tf_h >= 4 else H1, exploration=True,
            risk_scale=0.01, sleeve="active")
        return sig if sig.valid() else None


#: chosen on the first year of data only (see module docstring)
TREND_SPECS = [
    ("don", (55,), 8, 1), ("ema", (21, 50), 8, 1), ("st", (20, 3.0), 6, 1),
    ("psar", (0.005, 0.05), 6, 1), ("st", (10, 3.0), 4, 1), ("st", (10, 4.0), 8, 1),
    ("don", (55,), 6, 1), ("psar", (0.005, 0.05), 4, 1), ("psar", (0.01, 0.1), 8, 2),
    ("ema", (21, 50), 4, 1), ("ema", (21, 50), 6, 0), ("st", (10, 4.0), 2, 1),
]


def build_trend_systems() -> list[FlipSystem]:
    return [FlipSystem(*s) for s in TREND_SPECS]


TREND_NAMES = {FlipSystem(*s).name for s in TREND_SPECS}


def market_vol_factor(daily_vol: float | None, ref: float = 0.035,
                      lo: float = 0.25, hi: float = 1.5) -> float:
    """Risk multiplier from market volatility (median 30-day std of daily
    returns across the traded coins): calm market -> up to 1.5x, wild
    market -> down to 0.25x. Researched: Sharpe 0.95 -> 1.15."""
    if not daily_vol or daily_vol <= 0:
        return 1.0
    return float(min(hi, max(lo, ref / daily_vol)))
