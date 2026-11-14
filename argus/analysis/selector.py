"""Adaptive strategy selection (§13).

Tracks per-strategy, per-regime outcome statistics and disables strategies
whose profit factor (in R multiples) stays below 1.0 on a sufficient
sample. Re-enabling requires fresh passing statistics - recomputed from
data, never by decay. Every state change is audited.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from ..analysis.regime import Regime
from ..backtest.engine import SignalRecord

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StratStats:
    n: int = 0
    wins: int = 0
    r_sum: float = 0.0
    r_pos: float = 0.0
    r_neg: float = 0.0
    by_regime: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))  # regime -> [n, wins]

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def profit_factor_r(self) -> float:
        return self.r_pos / self.r_neg if self.r_neg > 0 else float("inf")

    @property
    def expectancy_r(self) -> float:
        return self.r_sum / self.n if self.n else 0.0

    def as_dict(self) -> dict:
        return {"n": self.n, "win_rate": round(self.win_rate, 3),
                "profit_factor_r": round(self.profit_factor_r, 3)
                if self.r_neg > 0 else None,
                "expectancy_r": round(self.expectancy_r, 3),
                "by_regime": {k: {"n": v[0], "wins": v[1]} for k, v in self.by_regime.items()}}


class StrategySelector:
    def __init__(self, min_sample: int = 30, min_pf: float = 1.0,
                 audit=None) -> None:
        self.min_sample = min_sample
        self.min_pf = min_pf
        self._stats: dict[str, StratStats] = defaultdict(StratStats)
        self._disabled: set[str] = set()
        self._audit = audit
        # per coin: lived stats and backtest-calibrated priors (net of fees)
        self._sym_stats: dict[tuple[str, str], StratStats] = defaultdict(StratStats)
        self._priors: dict[tuple[str, str], dict] = {}
        self.min_symbol_sample = 15
        self.min_prior_sample = 30

    def reset(self) -> None:
        """Clear lived stats before a full rebuild (priors are kept)."""
        self._stats = defaultdict(StratStats)
        self._sym_stats = defaultdict(StratStats)

    def set_priors(self, profiles: dict) -> None:
        """profiles: {symbol: {strategy: {n, win_rate, profit_factor_r, expectancy_r}}}
        from the fee-aware calibration backtest."""
        self._priors = {(sym, strat): dict(v)
                        for sym, strats in (profiles or {}).items()
                        for strat, v in (strats or {}).items()}

    def ingest(self, records: list[SignalRecord]) -> None:
        for r in records:
            if r.outcome not in ("win", "loss", "timeout"):
                continue
            for st in (self._stats[r.signal.strategy],
                       self._sym_stats[(r.signal.symbol, r.signal.strategy)]):
                st.n += 1
                st.r_sum += r.pl_r
                if r.pl_r > 0:
                    st.r_pos += r.pl_r
                    st.wins += 1
                else:
                    st.r_neg += -r.pl_r
                reg = st.by_regime[r.regime.value]
                reg[0] += 1
                reg[1] += 1 if r.pl_r > 0 else 0
        self._recompute()

    def _recompute(self) -> None:
        newly_disabled, newly_enabled = [], []
        for name, st in self._stats.items():
            bad = st.n >= self.min_sample and st.r_neg > 0 and st.profit_factor_r < self.min_pf
            if bad and name not in self._disabled:
                self._disabled.add(name)
                newly_disabled.append(name)
            elif not bad and name in self._disabled:
                self._disabled.discard(name)
                newly_enabled.append(name)
        for name in newly_disabled:
            log.warning("strategy %s DISABLED (PF %.2f on n=%d)", name,
                        self._stats[name].profit_factor_r, self._stats[name].n)
            if self._audit:
                self._audit("ml", f"Strategy {name} disabled (PF below {self.min_pf})",
                            "warning", self._stats[name].as_dict())
        for name in newly_enabled:
            log.info("strategy %s re-enabled", name)
            if self._audit:
                self._audit("ml", f"Strategy {name} re-enabled", "info",
                            self._stats[name].as_dict())

    def is_enabled(self, strategy_name: str, symbol: str | None = None) -> bool:
        if strategy_name in self._disabled:
            return False
        if symbol is None:
            return True
        return self.symbol_verdict(strategy_name, symbol)[0]

    def symbol_verdict(self, strategy_name: str, symbol: str) -> tuple[bool, str]:
        """(enabled, source) for one coin: lived trades decide once there are
        enough of them, otherwise the calibration backtest (net of fees)."""
        st = self._sym_stats.get((symbol, strategy_name))
        if st is not None and st.n >= self.min_symbol_sample:
            ok = st.r_neg == 0 or st.profit_factor_r >= self.min_pf
            return ok, "vlastné obchody"
        prior = self._priors.get((symbol, strategy_name))
        if prior is not None and prior.get("n", 0) >= self.min_prior_sample:
            pf = prior.get("profit_factor_r")
            ok = pf is None or pf >= self.min_pf
            return ok, "kalibrácia"
        return True, "málo dát"

    def regime_win_rate(self, strategy_name: str, regime: Regime) -> float | None:
        st = self._stats.get(strategy_name)
        if st is None:
            return None
        n, wins = st.by_regime.get(regime.value, [0, 0])
        return wins / n if n >= 10 else None

    def snapshot(self) -> dict:
        return {"disabled": sorted(self._disabled),
                "stats": {k: v.as_dict() for k, v in self._stats.items()}}

    def symbol_matrix(self, symbols: list[str], strategies: list[str]) -> dict:
        out: dict = {}
        for sym in symbols:
            row = {}
            for strat in strategies:
                ok, source = self.symbol_verdict(strat, sym)
                lived = self._sym_stats.get((sym, strat))
                prior = self._priors.get((sym, strat))
                row[strat] = {
                    "enabled": ok and strat not in self._disabled, "source": source,
                    "lived": lived.as_dict() if lived is not None and lived.n else None,
                    "calibration": prior}
            out[sym] = row
        return out
