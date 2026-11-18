"""CFE - Confluence Futures Engine as an Argus strategy (paper only).

Runs the reference engine (argus/cfe, identical to the research
repository's backtest/cfe.py) on CLOSED 15m candles with the 1h and 4h trend
as higher-timeframe context, and turns a signal on the last closed bar into:

  * a resting LIMIT at the broken structure level, valid 12 bars, cancelled
    if price reaches TP1 first (the move left without us);
  * stop below the impulse leg (- 0.2 ATR), take-profit in thirds at
    1R / 2R / 3R, stop to break-even after TP1 and to TP1 after TP2.

entry_mode "market" instead enters at the signal bar's close (TP stays
1/2/3 R; the 2R part is the minimum target).

Research verdict (validation report in docs/cfe/): no positive expected
value after costs (15m about -0.02 R per trade). It runs on its own paper
account with its own kill switch, never live without explicit consent.
"""

from __future__ import annotations

from ..analysis.regime import Regime
from ..marketdata.model import Timeframe
from .base import MarketContext, Signal, Strategy

M15, H1 = Timeframe.M15, Timeframe.H1


class CfeStrategy(Strategy):
    name = "cfe"
    exploration = True
    any_coin = True
    gate = "cfe"
    suited_regimes = tuple(Regime)

    def __init__(self, **params) -> None:
        self.params = dict(params)
        self.entry_mode = "limit"
        self._done: dict[str, int] = {}
        #: last evaluation per coin, for the UI: score, setup, reason
        self.last: dict[str, dict] = {}

    def p(self):
        from ..cfe.adapter import default_params
        kw = dict(self.params)
        kw["entry_mode"] = 0 if self.entry_mode == "market" else 1
        return default_params(**kw)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        from ..cfe.adapter import CHART_SEC, MIN_BARS, candle_rows, run
        now = int(ctx.now_ts)
        end = now // CHART_SEC * CHART_SEC              # end of the last closed 15m bar
        if self._done.get(ctx.symbol) == end:
            return None
        m15_loader = ctx.extras.get("m15_loader")
        h1_loader = ctx.extras.get("h1_loader")
        cache = ctx.extras.setdefault("_tf_cache", {})
        m15_c = m15_loader() if m15_loader else (ctx.candles.get(M15) or [])
        if "h1" not in cache:
            cache["h1"] = h1_loader() if h1_loader else (ctx.candles.get(H1) or [])
        m15 = candle_rows(m15_c, CHART_SEC, now)
        h1 = candle_rows(cache["h1"], 3600, now)
        if len(m15) < MIN_BARS or len(h1) < 300:
            return None
        if int(m15[-1, 0]) + CHART_SEC != end:
            if now - end > 600:                          # the bar never arrived: skip it
                self._done[ctx.symbol] = end
            return None
        self._done[ctx.symbol] = end
        p = self.p()
        out = run(m15, h1, p)
        k = "l" if out["sig_l"][-1] else ("s" if out["sig_s"][-1] else None)
        best = "l" if out["score_l"][-1] >= out["score_s"][-1] else "s"
        self.last[ctx.symbol] = {
            "ts": end, "score_long": round(float(out["score_l"][-1]), 1),
            "score_short": round(float(out["score_s"][-1]), 1),
            "htf_align": round(float(out["htf_align"][-1]), 2),
            "setup": bool(out["setup_l"][-1] or out["setup_s"][-1]),
            "signal": k is not None, "best": best}
        if k is None:
            return None
        d = "LONG" if k == "l" else "SHORT"
        entry = float(out[f"entry_{k}"][-1])
        sl = float(out[f"sl_{k}"][-1])
        tps = (float(out[f"tp1_{k}"][-1]), float(out[f"tp2_{k}"][-1]),
               float(out[f"tp3_{k}"][-1]))
        score = float(out[f"score_{k}"][-1])
        limit = self.entry_mode != "market"
        sig = Signal(self.name, ctx.symbol, d, entry, sl, tps[2], score / 100.0, [
            f"CFE skóre {score:.0f}/100 (HTF zhoda {out['htf_align'][-1]:+.2f})",
            f"prelomenie štruktúry s impulzom, {'limit na prelomenej úrovni' if limit else 'vstup na close'}",
            "TP 1R / 2R / 3R po tretinách, po TP1 stop na vstup, po TP2 na TP1"],
            timeframe=M15, exploration=True, risk_scale=0.25, sleeve="cfe",
            expire_s=float(p.valid_bars * CHART_SEC) if limit else 0.0,
            tps=tps, entry_type="limit" if limit else "market",
            cancel_price=tps[0] if limit else 0.0)
        return sig if sig.valid() else None
