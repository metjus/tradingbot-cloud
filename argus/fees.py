"""Exchange fee model (verified against official fee pages, Nov 2026).

  * Kraken Pro, Tier 1 (30-day volume from $0), effective 2026-07-09:
    maker 0.40 %, taker 0.80 %   (blog.kraken.com/product/pro/new-kraken-pro-fee-tiers)
  * Binance spot, Regular user / VIP 0:
    maker 0.10 %, taker 0.10 %; paying fees in BNB: 0.075 %   (binance.com/en/fee/trading)

The bot enters and exits with market orders, i.e. pays the TAKER rate on
both sides, plus slippage. Every entry decision and every position size
is computed net of this round-trip cost (see RiskSupervisor).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Venue:
    key: str
    name: str
    maker_pct: float
    taker_pct: float
    can_short_spot: bool = False
    futures: bool = False          # perpetual futures: short + leverage
    max_leverage: float = 1.0      # venue/regulatory cap (EU retail)
    maintenance_margin_pct: float = 0.0   # isolated-margin maintenance, % of notional
    funding_pct_per_hour: float = 0.0     # typical |funding| cost estimate
    note: str = ""


VENUES: dict[str, Venue] = {
    "kraken_futures": Venue(
        "kraken_futures", "Kraken Futures (perpetuals, Tier 1)", 0.02, 0.05,
        futures=True, max_leverage=10.0, maintenance_margin_pct=5.0,
        funding_pct_per_hour=0.002,
        note="Long aj short, páka, v EÚ regulované (MiFID). Poplatky ako Binance Futures."),
    "binance": Venue("binance", "Binance spot (VIP 0)", 0.10, 0.10,
                     note="Od 1. 7. 2026 pre EÚ účty nedostupné (MiCA)."),
    "binance_bnb": Venue("binance_bnb", "Binance (VIP 0, poplatky v BNB)", 0.075, 0.075,
                         note="Vyžaduje držať BNB na platbu poplatkov."),
    "kraken": Venue("kraken", "Kraken Pro spot (Tier 1)", 0.40, 0.80,
                    note="Od 9. 7. 2026 veľmi drahé pre krátkodobé obchody."),
}

DEFAULT_VENUE = "kraken_futures"
DEFAULT_SLIPPAGE_PCT = 0.02   # per side, liquid majors


def venue(key: str) -> Venue:
    return VENUES.get(key, VENUES[DEFAULT_VENUE])


def fee_pct(key: str, order_type: str = "taker") -> float:
    v = venue(key)
    return v.maker_pct if order_type == "maker" else v.taker_pct


def round_trip_cost_pct(key: str, order_type: str = "taker",
                        slippage_pct: float = DEFAULT_SLIPPAGE_PCT) -> float:
    """Total cost of entering AND exiting, in % of the position value."""
    return 2.0 * fee_pct(key, order_type) + 2.0 * slippage_pct


def venues_table() -> list[dict]:
    return [{"key": v.key, "name": v.name, "maker_pct": v.maker_pct,
             "taker_pct": v.taker_pct,
             "round_trip_pct": round(round_trip_cost_pct(v.key), 3),
             "futures": v.futures, "max_leverage": v.max_leverage,
             "note": v.note} for v in VENUES.values()]
