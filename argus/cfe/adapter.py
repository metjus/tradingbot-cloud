"""Argus candles -> CFE (reference engine) inputs, without look-ahead.

* chart: closed 15m candles (the validated timeframe), last CHART_WINDOW bars
* HTF:   closed 1h candles (long history, so the 4h EMA200 is settled), plus
  a placeholder for the hour that is still forming built from the 15m bars.
  The reference reads the HTF state of the PREVIOUS closed HTF bar via the
  bucket of the current bar; without the forming bucket it would silently
  read one bar too far back (found while matching it signal for signal).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .cfe import P2, compute
from .engine import resample

CHART_TF = "15m"
CHART_SEC = 900
CHART_WINDOW = 2000           # 15m bars (EMA200 + ATR-rank 500 + structure warm-up)
BASE_WINDOW = 3600            # 1h bars (150 days)
MIN_BARS = 600


def frame(rows: np.ndarray) -> pd.DataFrame:
    """rows: [ts_seconds, open, high, low, close, volume] -> reference frame.
    quote/taker columns are not used by CFE v2 (resample needs them)."""
    return pd.DataFrame({
        "open_time": (rows[:, 0] * 1000).astype(np.int64),
        "open": rows[:, 1], "high": rows[:, 2], "low": rows[:, 3], "close": rows[:, 4],
        "volume": rows[:, 5], "quote_volume": rows[:, 5] * rows[:, 4],
        "taker_buy_volume": rows[:, 5] / 2.0})


def candle_rows(candles, tf_sec: int, now_ts: int) -> np.ndarray:
    """Closed candles only (bar end <= now)."""
    rows = [(c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles
            if c.ts + tf_sec <= now_ts and getattr(c, "complete", True)]
    return np.array(rows, dtype=float).reshape(-1, 6)


def base_with_forming(h1: np.ndarray, m15: np.ndarray) -> np.ndarray:
    """Closed 1h rows + the forming hour(s) aggregated from the 15m rows."""
    if len(m15) == 0:
        return h1
    last = h1[-1, 0] if len(h1) else -1
    tail = m15[m15[:, 0] >= last + 3600]
    if len(tail) == 0:
        return h1
    r = resample(frame(tail), "1h")
    ext = np.column_stack([r["open_time"].to_numpy() / 1000.0, r["open"], r["high"],
                           r["low"], r["close"], r["volume"]])
    return np.vstack([h1, ext]) if len(h1) else ext


def default_params(**kw) -> P2:
    """Defaults requested for Argus: HTF alignment >= 1.0 (all higher
    timeframes agree), score >= 70, session 13-24 UTC, stop >= 4x costs,
    weights trend 45 / struct 15 / volume 10 / momentum 10 / liquidity 10 /
    volatility 10 (DEFAULT_WEIGHTS), limit retest entry valid 12 bars,
    TP 1/2/3 R with thirds. Costs: Kraken Futures taker/maker."""
    p = P2(htf_min=1.0, min_score=70.0, session_filter=True, cost_mult=4.0,
           fee_taker=0.05, fee_maker=0.02, slippage=0.02, entry_mode=1, valid_bars=12)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def run(m15: np.ndarray, h1: np.ndarray, p: P2) -> dict:
    m15 = m15[-CHART_WINDOW:]
    base = base_with_forming(h1[-BASE_WINDOW:], m15)
    return compute(frame(m15), p, base_df=frame(base), chart_tf=CHART_TF)
