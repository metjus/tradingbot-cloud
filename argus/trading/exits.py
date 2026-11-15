"""Adaptive exit management for open positions (§14, user items 6 and 9).

Problem it addresses: trades that go into profit and then reverse into a
loss. Protection is layered and only ever TIGHTENS a stop:

  1. risk reduction   at +0.5R  -> stop to -0.5R (half the risk gone)
  2. break-even       at +0.8R  -> stop to entry + round-trip costs, so a
                                   "break-even" exit really loses nothing
  3. trailing         from +1.2R -> stop trails 0.8R behind the price
  4. give-back        best profit >= 1R and 60 % of it given back -> exit
  5. sentiment shift  news sentiment turns clearly against the position
                      -> exit if in profit, otherwise cut risk to -0.5R
  6. regime flip      15m regime turns into a strong trend against the
                      position -> exit if in profit, otherwise cut risk
  7. TP extension     strong aligned regime + sentiment while price is
                      near take-profit -> move TP out by 0.5R (max 2x),
                      the trailing stop then protects the gain
  8. volume fade      in profit >= 1R and the signal-timeframe volume has
                      stayed under half its 20-bar average for 2 bars ->
                      momentum died, close and keep the profit (validated
                      on 2y Kraken Futures data: OOS PF 1.09 -> 1.17)

All thresholds are runtime settings. Every action is a plain data object
the executor applies through the broker and records with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..analysis.regime import Regime

ADVERSE_REGIME = {"LONG": Regime.STRONG_DOWNTREND, "SHORT": Regime.STRONG_UPTREND}
ALIGNED_REGIME = {"LONG": Regime.STRONG_UPTREND, "SHORT": Regime.STRONG_DOWNTREND}


@dataclass(slots=True)
class ExitParams:
    enabled: bool = True
    reduce_risk_at_r: float = 0.5
    break_even_at_r: float = 0.8
    trail_start_r: float = 1.2
    trail_distance_r: float = 0.8
    giveback_pct: float = 60.0
    giveback_min_r: float = 1.0
    sentiment_exit: bool = True
    sentiment_threshold: float = 0.45   # move against position, -1..1 scale
    regime_exit: bool = True
    round_trip_cost_pct: float = 0.0
    max_tp_extensions: int = 2
    volume_exit: bool = True
    volume_exit_min_r: float = 1.0
    volume_exit_bars: int = 2        # consecutive quiet bars required


@dataclass(slots=True)
class ExitAction:
    kind: str              # "stop" | "close" | "tp"
    price: float = 0.0     # new stop / new take-profit
    reason: str = ""


def _sign(direction: str) -> float:
    return 1.0 if direction == "LONG" else -1.0


def evaluate_exit(pos, price: float, params: ExitParams,
                  sentiment: float | None = None,
                  regime: Regime | None = None,
                  vol_fade: int | None = None) -> list[ExitAction]:
    """Decide protective actions for one position at the current price."""
    if not params.enabled or pos.risk_per_unit <= 0:
        return []
    s = _sign(pos.direction)
    R = pos.risk_per_unit
    r = pos.profit_r(price) + 1e-9   # tolerate float noise at thresholds
    pos.max_favorable_r = max(pos.max_favorable_r, r)
    actions: list[ExitAction] = []

    min_gap = price * 0.0005   # a stop must stay this far on the protective side

    def better(stop: float) -> bool:
        tighter = (s > 0 and stop > pos.stop_loss) or (s < 0 and stop < pos.stop_loss)
        # never place a stop at/through the current price - it would fire at once
        valid = (s > 0 and stop < price - min_gap) or (s < 0 and stop > price + min_gap)
        return tighter and valid

    # --- hard exits first -------------------------------------------------
    mfe = pos.max_favorable_r
    if mfe >= params.giveback_min_r and r <= mfe * (1 - params.giveback_pct / 100.0):
        return [ExitAction("close", reason=(
            f"ochrana zisku: z +{mfe:.2f}R vrátené na {r:+.2f}R"))]

    if params.volume_exit and vol_fade is not None             and r >= params.volume_exit_min_r             and vol_fade >= params.volume_exit_bars:
        return [ExitAction("close", reason=(
            f"objem vyschol ({vol_fade} sviečky pod polovicou priemeru), "
            f"zisk +{r:.2f}R uzamknutý"))]

    if params.sentiment_exit and sentiment is not None:
        baseline = pos.entry_sentiment or 0.0
        against = (baseline - sentiment) * s   # positive = moved against us
        hard_against = sentiment * s <= -params.sentiment_threshold
        if against >= params.sentiment_threshold or hard_against:
            if r > 0.15:
                return [ExitAction("close", reason=(
                    f"zvrat sentimentu ({baseline:+.2f} -> {sentiment:+.2f}), zisk uzamknutý"))]
            cut = pos.entry_price - s * 0.5 * R
            if better(cut):
                actions.append(ExitAction("stop", cut, "zvrat sentimentu: riziko znížené na polovicu"))

    if params.regime_exit and regime is not None and regime is ADVERSE_REGIME[pos.direction]:
        if r > 0.3:
            return [ExitAction("close", reason=(
                f"zvrat trhového režimu na {regime.value}, zisk uzamknutý"))]
        cut = pos.entry_price - s * 0.5 * R
        if better(cut) and not any(a.kind == "stop" and a.price == cut for a in actions):
            actions.append(ExitAction("stop", cut, f"režim {regime.value} proti pozícii"))

    # --- stepwise stop tightening ----------------------------------------
    candidates: list[tuple[float, str]] = []
    if r >= params.reduce_risk_at_r and not pos.risk_reduced:
        candidates.append((pos.entry_price - s * 0.5 * R, "riziko znížené na 0,5R"))
    if r >= params.break_even_at_r:
        cost = pos.entry_price * params.round_trip_cost_pct / 100.0
        candidates.append((pos.entry_price + s * (cost + 0.05 * R),
                           "break-even vrátane poplatkov"))
    if r >= params.trail_start_r:
        candidates.append((price - s * params.trail_distance_r * R, "trailing stop"))
    valid = [c for c in candidates if better(c[0])]
    if valid:
        best = max(valid, key=lambda c: c[0] * s)
        actions.append(ExitAction("stop", best[0], best[1]))

    # --- take-profit extension -------------------------------------------
    if (pos.tp_extensions < params.max_tp_extensions and regime is not None
            and regime is ALIGNED_REGIME[pos.direction]
            and (sentiment is None or sentiment * s >= 0.2)):
        dist_to_tp = (pos.take_profit - price) * s
        if 0 <= dist_to_tp <= 0.2 * R:
            actions.append(ExitAction("tp", pos.take_profit + s * 0.5 * R,
                                      "silný trend: take-profit posunutý ďalej"))
    return actions
