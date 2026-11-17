"""Popular TradingView indicators, re-implemented from their published
formulas (the maths, not the Pine code): Supertrend, UT Bot, Parabolic SAR,
Chandelier Exit, SSL Channel, Hull MA, Ichimoku, Squeeze Momentum
(LazyBear), WaveTrend (LazyBear), MACD.

Measured on 2 years of Kraken Futures data (15 coins, real costs), none of
them is a profitable strategy on its own: 3 of 60 indicator/exit/timeframe
variants were barely positive in both years (PF 1.02-1.03), every 15m
variant lost. They are therefore NOT traded directly; their current states
are given to the AI trader as additional context, the way a discretionary
trader glances at them on a chart.
"""

from __future__ import annotations

import numpy as np

from ..marketdata.model import Candle


def _arr(candles: list[Candle]):
    a = np.array([[c.open, c.high, c.low, c.close, c.volume] for c in candles], dtype=float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x)
    k = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = k * x[i] + (1 - k) * out[i - 1]
    return out


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _rolling(x: np.ndarray, n: int, fn) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        out[i] = fn(x[i - n + 1:i + 1])
    return out


def _atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    out = np.empty_like(tr)
    out[:n] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def _wma(x, n):
    w = np.arange(1, n + 1, dtype=float)
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        out[i] = np.dot(np.nan_to_num(x[i - n + 1:i + 1]), w) / w.sum()
    return out


def supertrend(h, l, c, n=10, m=3.0) -> int:
    A = _atr(h, l, c, n)
    hl2 = (h + l) / 2
    up, dn = hl2 - m * A, hl2 + m * A
    fu, fl, st = dn.copy(), up.copy(), 0
    for i in range(1, len(c)):
        fu[i] = dn[i] if (dn[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = up[i] if (up[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        st = 1 if c[i] > fu[i - 1] else (-1 if c[i] < fl[i - 1] else st)
    return st


def utbot(h, l, c, key=2.0, n=10) -> int:
    L = key * _atr(h, l, c, n)
    t = c[0]
    st = 0
    for i in range(1, len(c)):
        p = t
        if c[i] > p and c[i - 1] > p:
            t = max(p, c[i] - L[i])
        elif c[i] < p and c[i - 1] < p:
            t = min(p, c[i] + L[i])
        else:
            t = c[i] - L[i] if c[i] > p else c[i] + L[i]
        st = 1 if c[i] > t else -1
    return st


def psar(h, l, step=0.02, mx=0.2) -> int:
    up, af, ep, s = True, step, h[0], l[0]
    for i in range(1, len(h)):
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
    return 1 if up else -1


def hull_slope(c, n=55) -> int:
    hm = _wma(2 * _wma(c, n // 2) - _wma(c, n), int(np.sqrt(n)))
    return 1 if hm[-1] > hm[-2] else -1


def ichimoku(h, l, c) -> str:
    def mid(n):
        return (_rolling(h, n, np.max) + _rolling(l, n, np.min)) / 2
    ten, kij = mid(9), mid(26)
    sa, sb = (ten + kij) / 2, mid(52)
    if len(c) < 80:
        return "n/a"
    top = max(sa[-27], sb[-27])
    bot = min(sa[-27], sb[-27])
    pos = "above cloud" if c[-1] > top else ("below cloud" if c[-1] < bot else "inside cloud")
    tk = "tenkan>kijun" if ten[-1] > kij[-1] else "tenkan<kijun"
    return f"{pos}, {tk}"


def squeeze(h, l, c) -> str:
    m, sd = _sma(c, 20), _rolling(c, 20, np.std)
    ub, lb = m + 2 * sd, m - 2 * sd
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    rm = _sma(tr, 20)
    uk, lk = m + 1.5 * rm, m - 1.5 * rm
    on = (lb[-1] > lk[-1]) and (ub[-1] < uk[-1])
    src = c - ((_rolling(h, 20, np.max) + _rolling(l, 20, np.min)) / 2 + m) / 2
    win = np.nan_to_num(src[-20:])
    x = np.arange(20, dtype=float)
    slope, icpt = np.polyfit(x, win, 1)
    val = icpt + slope * 19
    win2 = np.nan_to_num(src[-21:-1])
    s2, i2 = np.polyfit(x, win2, 1)
    prev = i2 + s2 * 19
    trend = "rising" if val > prev else "falling"
    return f"{'squeeze ON' if on else 'released'}, momentum {val:+.4g} {trend}"


def wavetrend(h, l, c, n1=10, n2=21) -> str:
    ap = (h + l + c) / 3
    esa = _ema(ap, n1)
    d = _ema(abs(ap - esa), n1)
    ci = (ap - esa) / (0.015 * np.where(d == 0, 1e-12, d))
    w1 = _ema(ci, n2)
    w2 = _sma(w1, 4)
    zone = "overbought" if w1[-1] > 53 else ("oversold" if w1[-1] < -53 else "neutral")
    cross = "wt1>wt2" if w1[-1] > w2[-1] else "wt1<wt2"
    return f"wt1 {w1[-1]:.0f} ({zone}), {cross}"


def macd(c) -> str:
    m = _ema(c, 12) - _ema(c, 26)
    s = _ema(m, 9)
    return f"hist {m[-1] - s[-1]:+.4g} ({'above' if m[-1] > s[-1] else 'below'} signal)"


def tv_summary(candles: list[Candle]) -> str | None:
    """One line of indicator states for the AI brief; None if too short."""
    if len(candles) < 80:
        return None
    _o, h, l, c, _v = _arr(candles)
    dirn = {1: "up", -1: "down", 0: "n/a"}
    return (f"Supertrend {dirn[supertrend(h, l, c)]}, UT Bot {dirn[utbot(h, l, c)]}, "
            f"PSAR {dirn[psar(h, l)]}, Hull55 {dirn[hull_slope(c)]}, "
            f"Ichimoku {ichimoku(h, l, c)}, Squeeze {squeeze(h, l, c)}, "
            f"WaveTrend {wavetrend(h, l, c)}, MACD {macd(c)}")
