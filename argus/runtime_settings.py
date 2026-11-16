"""User-editable settings, stored in the local DB and applied live.

Static/bootstrap config (paths, ports, keys) stays in ``config.Settings``;
everything a trader wants to tweak from the UI lives here. Values are
validated and clamped - hard safety ceilings (e.g. 1 % risk per trade)
cannot be exceeded from the UI either.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from .db.database import Database
from .db.schema import AppSetting
from .fees import VENUES

log = logging.getLogger(__name__)

KEY = "runtime_settings"

DEFAULT_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"]

# name: (default, type, min, max)   min/max None = unchecked
SCHEMA: dict[str, tuple[Any, type, Any, Any]] = {
    # money the bot may use
    "budget_eur": (10_000.0, float, 10.0, 10_000_000.0),
    "max_position_eur": (2_500.0, float, 5.0, 10_000_000.0),
    # risk
    "risk_per_trade_pct": (1.0, float, 0.05, 1.0),        # hard ceiling 1 %
    "daily_max_loss_pct": (2.0, float, 0.2, 5.0),
    "max_open_positions": (4, int, 1, 20),
    "max_daily_trades": (15, int, 1, 200),
    "min_rr_net": (1.3, float, 1.0, 5.0),                 # reward/risk AFTER costs
    "enter_threshold": (70.0, float, 50.0, 95.0),
    # execution venue & costs
    "fee_venue": ("kraken_futures", str, None, None),
    # limit = maker entry at the signal price (cheaper: 0.02 % vs 0.05 %,
    # no entry slippage); unfilled entries expire after one signal bar
    "entry_order": ("limit", str, None, None),
    "slippage_pct": (0.02, float, 0.0, 0.5),
    "allow_short": (True, bool, None, None),               # futures: short allowed
    # futures only: position leverage (EU retail cap 10x on Kraken Futures).
    # Leverage does NOT change the risk per trade (that is set by the stop),
    # it only lowers the margin blocked per position.
    "leverage": (3.0, float, 1.0, 10.0),
    # adaptive exits
    "adaptive_exits": (True, bool, None, None),
    # defaults re-validated on 2y Kraken Futures data with the 4h strategies
    "reduce_risk_at_r": (0.8, float, 0.2, 3.0),
    "break_even_at_r": (1.0, float, 0.3, 3.0),
    "trail_start_r": (1.5, float, 0.5, 5.0),
    "trail_distance_r": (1.5, float, 0.2, 3.0),
    "giveback_pct": (70.0, float, 20.0, 95.0),
    "giveback_min_r": (2.0, float, 0.5, 5.0),
    "sentiment_exit": (True, bool, None, None),
    "regime_exit": (True, bool, None, None),
    "volume_exit": (True, bool, None, None),
    # exploration (skúšobný režim): trial strategy at a fraction of the risk,
    # auto-off when it loses after fees
    "explore_enabled": (True, bool, None, None),
    "explore_risk_scale": (0.1, float, 0.05, 0.3),
    # AI trader: an LLM decides limit levels on the 15m chart every 30 min
    "ai_trader_enabled": (True, bool, None, None),
    "ai_symbols": (["BTC/USD"], list, None, None),
    "ai_risk_scale": (0.25, float, 0.05, 0.5),
    "ai_model": ("", str, None, None),
    "volume_exit_min_r": (1.0, float, 0.3, 5.0),
    # instruments (applied on next engine start)
    "symbols": (DEFAULT_SYMBOLS, list, None, None),
    "auto_symbols": (True, bool, None, None),   # pick coins by 24h USD volume
    "max_symbols": (15, int, 2, 20),
    # ML
    "ml_auto_activate": (True, bool, None, None),
    # LLM news analysis (one provider active at a time)
    "llm_provider": ("none", str, None, None),             # none | claude | openai | gemini
    "llm_model": ("", str, None, None),
    "llm_daily_budget_usd": (1.0, float, 0.0, 50.0),
}

CHOICES = {
    "fee_venue": list(VENUES),
    "entry_order": ["limit", "market"],
    "llm_provider": ["none", "claude", "openai", "gemini"],
}

RESTART_REQUIRED = {"symbols", "fee_venue", "auto_symbols", "max_symbols"}


def _coerce(name: str, value: Any) -> Any:
    default, typ, lo, hi = SCHEMA[name]
    if typ is bool:
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on", "áno")
        return bool(value)
    if typ is list:
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",")]
        out = [str(v).strip().upper() for v in value if str(v).strip()]
        if not out:
            raise ValueError(f"{name}: zoznam nesmie byť prázdny")
        return out
    if typ is str:
        v = str(value).strip()
        if name in CHOICES and v not in CHOICES[name]:
            raise ValueError(f"{name}: neplatná hodnota {v!r}")
        return v
    v = typ(value)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


class RuntimeSettings:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._values: dict[str, Any] = {k: v[0] for k, v in SCHEMA.items()}
        self._listeners: list[Callable[[dict], None]] = []
        self._load()

    def _load(self) -> None:
        with self._db.session() as s:
            row = s.get(AppSetting, KEY)
            stored = dict(row.value) if row is not None else {}
        migrated = self._migrate_v12(stored)
        if self._migrate_v13(stored):
            migrated = True
        for k, v in stored.items():
            if k in SCHEMA:
                try:
                    self._values[k] = _coerce(k, v)
                except (ValueError, TypeError):
                    log.warning("ignoring invalid stored setting %s=%r", k, v)
        if migrated:
            self._save()
            self._db.audit("system",
                           "Aktualizácia 1.2: burza prepnutá na Kraken Futures "
                           "(Binance od 1. 7. 2026 nefunguje pre účty z EÚ); SHORT povolený.")

    @staticmethod
    def _migrate_v12(stored: dict) -> bool:
        """Settings saved by 1.1 keep the old Binance spot venue; Binance no
        longer serves EU accounts, so move them to Kraken Futures once."""
        if not stored or "leverage" in stored:
            return False                      # fresh install or already 1.2
        changed = False
        if stored.get("fee_venue") in ("binance", "binance_bnb"):
            stored["fee_venue"] = "kraken_futures"
            stored["allow_short"] = True
            changed = True
        return changed

    @staticmethod
    def _migrate_v13(stored: dict) -> bool:
        """1.2.x stores (pre maker-entry era) kept the launch-phase 0.5 %
        risk. The validated optimum is 1 % (about an eighth of Kelly for
        the measured edge); bump it once, only when the user never changed
        the old default."""
        if not stored or "entry_order" in stored:
            return False
        if stored.get("risk_per_trade_pct") == 0.5:
            stored["risk_per_trade_pct"] = 1.0
            return True
        return False

    def _save(self) -> None:
        with self._db.session() as s:
            row = s.get(AppSetting, KEY)
            if row is None:
                s.add(AppSetting(key=KEY, value=dict(self._values)))
            else:
                row.value = dict(self._values)
            s.commit()

    # --- access -------------------------------------------------------------
    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def as_dict(self) -> dict:
        return dict(self._values)

    def describe(self) -> dict:
        return {"values": self.as_dict(), "choices": CHOICES,
                "restart_required": sorted(RESTART_REQUIRED),
                "limits": {k: {"min": v[2], "max": v[3]} for k, v in SCHEMA.items()
                           if v[2] is not None}}

    def on_change(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def update(self, changes: dict) -> dict:
        """Validate, persist and broadcast. Unknown keys are rejected."""
        unknown = [k for k in changes if k not in SCHEMA]
        if unknown:
            raise ValueError(f"neznáme nastavenia: {', '.join(unknown)}")
        new = dict(self._values)
        for k, v in changes.items():
            new[k] = _coerce(k, v)
        if new["max_position_eur"] > new["budget_eur"]:
            new["max_position_eur"] = new["budget_eur"]
        if new["trail_start_r"] < new["break_even_at_r"]:
            new["trail_start_r"] = new["break_even_at_r"]
        changed = {k: new[k] for k in new if new[k] != self._values.get(k)}
        self._values = new
        self._save()
        if changed:
            self._db.audit("system", "Nastavenia zmenené", data=changed)
            for fn in self._listeners:
                try:
                    fn(changed)
                except Exception as exc:  # a listener must never break saving
                    log.warning("settings listener failed: %s", exc)
        return changed
