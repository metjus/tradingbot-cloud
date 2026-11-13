"""Feature snapshot per (symbol, timeframe): last values of all indicators.

Strategies and the decision engine read these snapshots instead of
recomputing indicators; the snapshot also becomes the ML feature vector /
trade entry_context in later phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..marketdata.model import Candle
from . import indicators as ind


def _last(series: list) -> float | None:
    return series[-1] if series else None


@dataclass(slots=True)
class FeatureSet:
    close: float
    ema9: float | None = None
    ema21: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    macd_hist_prev: float | None = None
    atr14: float | None = None
    bb_upper: float | None = None
    bb_mid: float | None = None
    bb_lower: float | None = None
    bb_width_pct: float | None = None       # (upper-lower)/mid
    bb_width_pctile: float | None = None    # percentile of width over window
    vwap: float | None = None
    adx14: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    high_20: float | None = None            # highest high of last 20 closed bars
    low_20: float | None = None
    vol_avg20: float | None = None
    vol_last: float | None = None
    extras: dict = field(default_factory=dict)

    @property
    def vol_ratio(self) -> float | None:
        if self.vol_avg20 and self.vol_avg20 > 0 and self.vol_last is not None:
            return self.vol_last / self.vol_avg20
        return None

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "close", "ema9", "ema21", "ema50", "ema200", "rsi14", "macd",
            "macd_signal", "macd_hist", "atr14", "bb_upper", "bb_mid",
            "bb_lower", "bb_width_pct", "vwap", "adx14", "plus_di",
            "minus_di", "high_20", "low_20", "vol_avg20", "vol_last",
        )}
        d["vol_ratio"] = self.vol_ratio
        return d


def compute_features(candles: list[Candle]) -> FeatureSet | None:
    """Compute a FeatureSet from CLOSED candles (ascending). Needs >= 30 bars;
    ema200 and others stay None until enough history exists."""
    if len(candles) < 30:
        return None
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    macd_line, macd_sig, macd_hist = ind.macd(closes)
    bb_up, bb_mid, bb_lo = ind.bollinger(closes, 20, 2.0)
    adx_s, pdi_s, mdi_s = ind.adx(candles, 14)

    width_series: list[float] = []
    for u, m, lo in zip(bb_up, bb_mid, bb_lo):
        if u is not None and m:
            width_series.append((u - lo) / m)
    width_pctile = None
    width_pct = width_series[-1] if width_series else None
    if width_pct is not None and len(width_series) >= 20:
        window = width_series[-100:]
        width_pctile = 100.0 * sum(1 for w in window if w <= width_pct) / len(window)

    vol_sma = ind.sma(vols, 20)

    fs = FeatureSet(
        close=closes[-1],
        ema9=_last(ind.ema(closes, 9)),
        ema21=_last(ind.ema(closes, 21)),
        ema50=_last(ind.ema(closes, 50)) if len(closes) >= 50 else None,
        ema200=_last(ind.ema(closes, 200)) if len(closes) >= 200 else None,
        rsi14=_last(ind.rsi(closes, 14)),
        macd=_last(macd_line),
        macd_signal=_last(macd_sig),
        macd_hist=_last(macd_hist),
        macd_hist_prev=macd_hist[-2] if len(macd_hist) >= 2 else None,
        atr14=_last(ind.atr(candles, 14)),
        bb_upper=_last(bb_up), bb_mid=_last(bb_mid), bb_lower=_last(bb_lo),
        bb_width_pct=width_pct, bb_width_pctile=width_pctile,
        vwap=_last(ind.vwap(candles)),
        adx14=_last(adx_s), plus_di=_last(pdi_s), minus_di=_last(mdi_s),
        high_20=max(highs[-21:-1]) if len(highs) >= 21 else None,
        low_20=min(lows[-21:-1]) if len(lows) >= 21 else None,
        vol_avg20=_last(vol_sma), vol_last=vols[-1],
    )
    return fs
