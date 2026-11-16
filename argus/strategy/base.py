"""Strategy interface and the market context passed to strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..analysis.regime import Regime
from ..marketdata.model import Candle, Timeframe
from ..ta.features import FeatureSet


@dataclass(slots=True)
class MarketContext:
    symbol: str
    now_ts: int
    candles: dict[Timeframe, list[Candle]]      # closed bars, ascending
    features: dict[Timeframe, FeatureSet]
    regime: Regime
    spread_pct: float | None = None             # (ask-bid)/mid*100, None if unknown
    sentiment: float | None = None              # -1..1, None until phase 6
    extras: dict = field(default_factory=dict)

    def f(self, tf: Timeframe) -> FeatureSet | None:
        return self.features.get(tf)


@dataclass(slots=True)
class Signal:
    strategy: str
    symbol: str
    direction: str                 # LONG | SHORT
    entry: float                   # intended entry (close of signal bar)
    stop_loss: float
    take_profit: float
    confidence: float              # 0..1 strategy-internal quality
    reasons: list[str]
    timeframe: Timeframe = Timeframe.M5
    #: exploration (trial) signals trade at a fraction of the normal risk
    exploration: bool = False
    risk_scale: float = 1.0
    #: which trial account it belongs to ("explore", "ai"); "" = validated
    sleeve: str = ""
    #: limit entry validity in seconds (0 = one signal bar)
    expire_s: float = 0.0

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def rr(self) -> float:
        risk = self.risk_per_unit
        if risk <= 0:
            return 0.0
        return abs(self.take_profit - self.entry) / risk

    def valid(self) -> bool:
        if self.direction == "LONG":
            return self.stop_loss < self.entry < self.take_profit
        if self.direction == "SHORT":
            return self.take_profit < self.entry < self.stop_loss
        return False

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy, "symbol": self.symbol,
            "direction": self.direction, "entry": self.entry,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit,
            "confidence": round(self.confidence, 3), "rr": round(self.rr, 2),
            "timeframe": self.timeframe.value, "reasons": self.reasons,
            "exploration": self.exploration, "risk_scale": self.risk_scale,
            "sleeve": self.sleeve, "expire_s": self.expire_s,
        }


class Strategy(ABC):
    """A strategy inspects the context and proposes at most one signal."""

    name: str = "strategy"
    #: trial strategy without a validated edge: traded at a fraction of the
    #: normal risk, with its own caps and an automatic kill switch
    exploration: bool = False
    #: regimes this strategy is designed for; the decision engine
    #: penalises (not blocks) signals outside them
    suited_regimes: tuple[Regime, ...] = ()

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> Signal | None:
        ...

    def _tp_from_rr(self, entry: float, stop: float, rr: float, direction: str) -> float:
        risk = abs(entry - stop)
        return entry + rr * risk if direction == "LONG" else entry - rr * risk
