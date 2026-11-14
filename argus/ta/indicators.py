"""Technical indicators (pure Python, no heavy deps).

All functions take chronologically ascending sequences and return lists of
the same length with ``None`` where the indicator is not yet defined.
Implementations follow the standard definitions (Wilder smoothing for
RSI/ATR/ADX), verified by unit tests with hand-computed values.
"""

from __future__ import annotations

from ..marketdata.model import Candle

Series = list[float]
OptSeries = list[float | None]


def sma(values: Series, period: int) -> OptSeries:
    out: OptSeries = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be > 0")
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= period:
            acc -= values[i - period]
        if i >= period - 1:
            out[i] = acc / period
    return out


def ema(values: Series, period: int) -> OptSeries:
    """EMA seeded with the SMA of the first ``period`` values."""
    out: OptSeries = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(values: Series, period: int = 14) -> OptSeries:
    out: OptSeries = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_val(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_val(avg_gain, avg_loss)
    return out


def _rsi_val(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(values: Series, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[OptSeries, OptSeries, OptSeries]:
    """Returns (macd_line, signal_line, histogram)."""
    ef = ema(values, fast)
    es = ema(values, slow)
    line: OptSeries = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    defined = [v for v in line if v is not None]
    sig_def = ema(defined, signal) if len(defined) >= signal else [None] * len(defined)
    sig: OptSeries = [None] * len(values)
    j = 0
    for i, v in enumerate(line):
        if v is not None:
            sig[i] = sig_def[j]
            j += 1
    hist: OptSeries = [None if (l is None or s is None) else l - s for l, s in zip(line, sig)]
    return line, sig, hist


def true_ranges(candles: list[Candle]) -> Series:
    trs: Series = []
    prev_close: float | None = None
    for c in candles:
        if prev_close is None:
            trs.append(c.high - c.low)
        else:
            trs.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return trs


def atr(candles: list[Candle], period: int = 14) -> OptSeries:
    trs = true_ranges(candles)
    out: OptSeries = [None] * len(candles)
    if len(candles) < period:
        return out
    seed = sum(trs[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(candles)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def bollinger(values: Series, period: int = 20, mult: float = 2.0
              ) -> tuple[OptSeries, OptSeries, OptSeries]:
    """Returns (upper, middle, lower)."""
    mid = sma(values, period)
    up: OptSeries = [None] * len(values)
    lo: OptSeries = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        m = mid[i]
        var = sum((v - m) ** 2 for v in window) / period
        sd = var ** 0.5
        up[i] = m + mult * sd
        lo[i] = m - mult * sd
    return up, mid, lo


def vwap(candles: list[Candle], session_start_ts: int | None = None) -> OptSeries:
    """Volume-weighted average price, cumulative from session start
    (or from the beginning of the given window)."""
    out: OptSeries = [None] * len(candles)
    pv = 0.0
    vol = 0.0
    for i, c in enumerate(candles):
        if session_start_ts is not None and c.ts < session_start_ts:
            continue
        typical = (c.high + c.low + c.close) / 3.0
        pv += typical * c.volume
        vol += c.volume
        out[i] = pv / vol if vol > 0 else typical
    return out


def adx(candles: list[Candle], period: int = 14) -> tuple[OptSeries, OptSeries, OptSeries]:
    """Wilder ADX. Returns (adx, +di, -di)."""
    n = len(candles)
    adx_out: OptSeries = [None] * n
    pdi_out: OptSeries = [None] * n
    mdi_out: OptSeries = [None] * n
    if n < period + 1:
        return adx_out, pdi_out, mdi_out

    trs = true_ranges(candles)
    pdm: Series = [0.0] * n
    mdm: Series = [0.0] * n
    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        dn = candles[i - 1].low - candles[i].low
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0

    # Wilder smoothed sums, seeded over bars 1..period
    s_tr = sum(trs[1: period + 1])
    s_pdm = sum(pdm[1: period + 1])
    s_mdm = sum(mdm[1: period + 1])
    dxs: list[float] = []
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + trs[i]
            s_pdm = s_pdm - s_pdm / period + pdm[i]
            s_mdm = s_mdm - s_mdm / period + mdm[i]
        pdi = 100.0 * s_pdm / s_tr if s_tr > 0 else 0.0
        mdi = 100.0 * s_mdm / s_tr if s_tr > 0 else 0.0
        pdi_out[i] = pdi
        mdi_out[i] = mdi
        denom = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0
        dxs.append(dx)
        if len(dxs) == period:
            adx_out[i] = sum(dxs) / period
        elif len(dxs) > period:
            adx_out[i] = (adx_out[i - 1] * (period - 1) + dx) / period
    return adx_out, pdi_out, mdi_out


def rolling_high(values: Series, period: int) -> OptSeries:
    out: OptSeries = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = max(values[i - period + 1: i + 1])
    return out


def rolling_low(values: Series, period: int) -> OptSeries:
    out: OptSeries = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = min(values[i - period + 1: i + 1])
    return out
