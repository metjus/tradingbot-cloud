"""AI Decision Engine (§8): scores every opportunity 0-100 and explains it.

Flow per evaluation:
  1. detect regime (15m + 1h features)
  2. ask every strategy for a signal
  3. score each signal from weighted, inspectable factors
  4. best signal above threshold -> candidate trade -> Risk Supervisor
  5. everything (entered, rejected, vetoed) is persisted with reasons

The score is a setup-quality heuristic, NOT a probability of profit. In
phase 5 an ML model (P(TP before SL)) becomes one more scoring factor;
thresholds are config, optimised only by backtests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .analysis.regime import Regime, detect_regime
from .marketdata.model import Timeframe
from .risk import AccountState, RiskSupervisor, RiskVerdict
from .strategy.base import MarketContext, Signal, Strategy
from .strategy.library import ALL_STRATEGIES

log = logging.getLogger(__name__)

M5, M15, H1 = Timeframe.M5, Timeframe.M15, Timeframe.H1


@dataclass(slots=True)
class ScoredSignal:
    signal: Signal
    score: float
    factors: dict[str, float]

    def as_dict(self) -> dict:
        return {"signal": self.signal.as_dict(), "score": round(self.score, 1),
                "factors": {k: round(v, 1) for k, v in self.factors.items()}}


@dataclass(slots=True)
class DecisionResult:
    symbol: str
    regime: Regime
    verdict: str                      # enter | reject | veto | no_signal
    best: ScoredSignal | None = None
    risk: RiskVerdict | None = None
    evaluated: list[ScoredSignal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "regime": self.regime.value,
            "verdict": self.verdict,
            "best": self.best.as_dict() if self.best else None,
            "risk": self.risk.as_dict() if self.risk else None,
            "evaluated": [s.as_dict() for s in self.evaluated],
            "notes": self.notes,
        }


class DecisionEngine:
    def __init__(self, risk: RiskSupervisor, strategies: list[Strategy] | None = None,
                 enter_threshold: float = 70.0, ml_scorer=None, selector=None) -> None:
        self.risk = risk
        self.strategies = strategies if strategies is not None else ALL_STRATEGIES
        self.enter_threshold = enter_threshold
        #: optional SetupScorer (argus.ml.model); adds a -20..+20 adjustment
        self.ml_scorer = ml_scorer
        #: optional StrategySelector; disabled strategies are skipped
        self.selector = selector

    # --- scoring ----------------------------------------------------------
    def score(self, sig: Signal, ctx: MarketContext) -> ScoredSignal:
        """0-100 from weighted factors; each factor is 0..its weight."""
        f: dict[str, float] = {}

        # strategy's own confidence (0-30)
        f["strategy_confidence"] = 30.0 * max(0.0, min(sig.confidence, 1.0))

        # regime fit (0-20)
        strat = next((s for s in self.strategies if s.name == sig.strategy), None)
        if strat is not None and ctx.regime in strat.suited_regimes:
            f["regime_fit"] = 20.0
        elif ctx.regime is Regime.UNCERTAIN:
            f["regime_fit"] = 8.0
        else:
            f["regime_fit"] = 4.0

        # multi-timeframe trend alignment (0-15)
        align = 0.0
        for tf, w in ((M15, 9.0), (H1, 6.0)):
            ff = ctx.f(tf)
            if ff is not None and ff.ema21 is not None:
                if (sig.direction == "LONG") == (ff.close > ff.ema21):
                    align += w
        f["mtf_alignment"] = align

        # risk/reward (0-15): 1.2 -> 4, 2.0 -> 10, >=3 -> 15
        rr = sig.rr
        f["risk_reward"] = max(0.0, min(15.0, (rr - 0.6) * 6.25))

        # volume support (0-10)
        f5 = ctx.f(M5)
        vr = f5.vol_ratio if f5 is not None else None
        f["volume"] = 0.0 if vr is None else max(0.0, min(10.0, (vr - 0.8) * 8.0))

        # volatility sanity (0-10): penalise extreme ATR% regimes
        vol_score = 5.0
        if f5 is not None and f5.atr14 is not None and f5.close:
            atr_pct = f5.atr14 / f5.close * 100.0
            if 0.08 <= atr_pct <= 0.9:
                vol_score = 10.0
            elif atr_pct > 1.8 or atr_pct < 0.03:
                vol_score = 0.0
        f["volatility_fit"] = vol_score

        # sentiment (0-10): neutral 5 until the news module (phase 6) feeds it
        if ctx.sentiment is None:
            f["sentiment"] = 5.0
        else:
            s = max(-1.0, min(1.0, ctx.sentiment))
            aligned = s if sig.direction == "LONG" else -s
            f["sentiment"] = 5.0 + 5.0 * aligned

        # spread/cost (penalty up to -10)
        if ctx.spread_pct is not None:
            f["spread_penalty"] = -min(10.0, ctx.spread_pct * 40.0)

        # ML setup scorer: P(TP before SL) mapped to -20..+20 around p=0.5.
        # Absent or failing model = 0 (engine fully functional without ML).
        if self.ml_scorer is not None:
            f5s = ctx.f(M5)
            if f5s is not None:
                try:
                    p = self.ml_scorer.score_live(sig, ctx.regime, f5s.as_dict(), ctx.now_ts)
                    f["ml_probability"] = (max(0.0, min(1.0, p)) - 0.5) * 40.0
                except Exception as exc:
                    log.warning("ml scorer failed, ignoring: %s", exc)

        total = max(0.0, min(100.0, sum(f.values())))
        return ScoredSignal(signal=sig, score=total, factors=f)

    # --- evaluation -------------------------------------------------------
    def evaluate(self, ctx: MarketContext, account: AccountState) -> DecisionResult:
        ctx.regime = detect_regime(ctx.f(M15), ctx.f(H1))
        res = DecisionResult(symbol=ctx.symbol, regime=ctx.regime, verdict="no_signal")

        for strat in self.strategies:
            if self.selector is not None and not self.selector.is_enabled(strat.name):
                continue
            try:
                sig = strat.evaluate(ctx)
            except Exception as exc:  # a broken strategy must never kill the engine
                log.exception("strategy %s failed: %s", strat.name, exc)
                res.notes.append(f"strategy {strat.name} error: {exc}")
                continue
            if sig is None:
                continue
            if not sig.valid():
                res.notes.append(f"{strat.name}: invalid SL/TP layout, discarded")
                continue
            res.evaluated.append(self.score(sig, ctx))

        if not res.evaluated:
            return res

        res.evaluated.sort(key=lambda s: s.score, reverse=True)
        res.best = res.evaluated[0]

        if res.best.score < self.enter_threshold:
            res.verdict = "reject"
            res.notes.append(
                f"best score {res.best.score:.0f} below threshold {self.enter_threshold:.0f}")
            return res

        res.risk = self.risk.check(res.best.signal, account, ctx.spread_pct)
        res.verdict = "enter" if res.risk.approved else "veto"
        return res
