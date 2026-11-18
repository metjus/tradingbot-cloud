"""CFE backtest on Kraken Futures data with the research simulator.

Signals: the same engine + default parameters the bot trades (adapter.py).
Execution: the reference simulator (sim.py, run_sequential_v2): limit retest
fill only when price trades THROUGH the level, cancelled at TP1 or after 12
bars, thirds at 1R/2R/3R, break-even after TP1, stop to TP1 after TP2,
Kraken fees (maker 0.02 % on the limit and TP fills, taker 0.05 % + 0.02 %
slippage on stops), hourly funding when a funding file is given.

    python -m argus.cfe.backtest --days 365              # downloads from Kraken
    python -m argus.cfe.backtest --data-dir DIR          # PF_*_15m.json / _1h.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import sim
from .adapter import default_params, frame
from .cfe import compute

COINS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "ADA/USD", "LINK/USD",
         "AVAX/USD", "LTC/USD", "SUI/USD", "NEAR/USD", "XLM/USD", "BNB/USD", "UNI/USD",
         "DOT/USD", "HYPE/USD", "ZEC/USD"]


def funding_bars(open_time_ms: np.ndarray, funding: list | None) -> np.ndarray:
    """Hourly Kraken funding rates (fraction per hour, long pays when > 0)
    mapped to the 15m bar that opens at the funding hour."""
    f = np.zeros(len(open_time_ms))
    if not funding:
        return f
    t = pd.to_datetime([x[0] for x in funding], utc=True).asi8 // 10**6
    idx = np.searchsorted(open_time_ms, t)
    ok = (idx < len(open_time_ms)) & (open_time_ms[np.clip(idx, 0, len(f) - 1)] == t)
    np.add.at(f, idx[ok], np.array([float(x[1]) for x in funding])[ok])
    return f


def backtest_coin(m15: np.ndarray, h1: np.ndarray, p=None, funding=None) -> pd.DataFrame:
    p = p or default_params()
    out = compute(frame(m15), p, base_df=frame(h1), chart_tf="15m")
    sig = np.where(out["sig_l"], 1, np.where(out["sig_s"], -1, 0)).astype(np.int8)
    lv = {}
    for key, d in (("l", 1), ("s", -1)):
        e = out[f"entry_{key}"] if p.entry_mode == 1 else out["c"]
        dist = np.abs(e - out[f"sl_{key}"])
        lv[key] = (e, out[f"sl_{key}"], e + d * p.tp1_r * dist, e + d * p.tp2_r * dist,
                   e + d * p.tp3_r * dist)
    fund = funding_bars(out["open_time"], funding)
    T = sim.run_sequential_v2(sig, *lv["l"], *lv["s"], p.entry_mode, p.valid_bars,
                              out["o"], out["h"], out["l"], out["c"], out["atr"], fund,
                              0, len(sig), p.frac_tp1, p.frac_tp2, p.be_after_tp1,
                              p.lock_after_tp2, 0.0, 0, p.fee_taker, p.fee_maker, p.slippage,
                              False)
    return sim.trades_v2(T, out["open_time"], 900_000)


def summary(tr: pd.DataFrame, days: float, risk_pct: float = 0.5) -> dict:
    if len(tr) == 0:
        return {"trades": 0}
    tr = tr.sort_values("entry_time")
    r = tr["r_net"].to_numpy()
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    eq = np.cumprod(1.0 + risk_pct / 100.0 * r)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    t = pd.to_datetime(tr["entry_time"])
    mid = t.min() + (t.max() - t.min()) / 2
    a, b = r[(t < mid).to_numpy()], r[(t >= mid).to_numpy()]
    return {
        "trades": int(len(r)), "per_day": round(len(r) / days, 2),
        "win_rate_pct": round(float((r > 0).mean() * 100), 1),
        "avg_R_net": round(float(r.mean()), 3), "avg_R_gross": round(float(tr["r_gross"].mean()), 3),
        "avg_cost_R": round(float(tr["r_cost"].mean()), 3),
        "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
        "total_R": round(float(r.sum()), 1),
        "first_half_avg_R": round(float(a.mean()), 3) if len(a) else None,
        "second_half_avg_R": round(float(b.mean()), 3) if len(b) else None,
        f"return_at_{risk_pct}pct_risk": round(float((eq[-1] - 1) * 100), 1),
        "max_drawdown_pct": round(float((eq / peak - 1).min() * 100), 1),
        "long_avg_R": round(float(tr.loc[tr["dir"] == 1, "r_net"].mean()), 3),
        "short_avg_R": round(float(tr.loc[tr["dir"] == -1, "r_net"].mean()), 3),
    }


def _load_dir(d: Path, pf: str):
    def rd(name):
        f = d / name
        return json.load(open(f)) if f.exists() else None
    m15, h1 = rd(f"{pf}_15m.json"), rd(f"{pf}_1h.json")
    if m15 is None or h1 is None:
        return None
    return np.array(m15, dtype=float), np.array(h1, dtype=float), rd(f"{pf}_funding.json")


async def _download(sym: str, days: int):
    from ..marketdata.kraken_futures import fetch_history
    m = await fetch_history(sym, days, "15m")
    h = await fetch_history(sym, days + 60, "1h")
    rows = lambda cs: np.array([[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in cs], float)
    return rows(m), rows(h), None


def main() -> None:
    from ..marketdata.kraken_futures import to_pf
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--entry", choices=["limit", "market"], default="limit")
    args = ap.parse_args()
    p = default_params(entry_mode=0 if args.entry == "market" else 1)
    allt, span = [], 0.0
    for sym in args.coins.split(","):
        got = (_load_dir(args.data_dir, to_pf(sym)) if args.data_dir
               else asyncio.run(_download(sym, args.days)))
        if got is None:
            continue
        m15, h1, fund = got
        tr = backtest_coin(m15, h1, p, fund)
        tr["symbol"] = sym
        allt.append(tr)
        span = max(span, (m15[-1, 0] - m15[0, 0]) / 86400)
        s = summary(tr, span)
        print(f"{sym:9s} {s.get('trades', 0):4d} obchodov  avg {s.get('avg_R_net', 0):+.3f} R  "
              f"PF {s.get('profit_factor')}")
    tr = pd.concat(allt) if allt else pd.DataFrame()
    print(json.dumps(summary(tr, span) if len(tr) else {"trades": 0}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
