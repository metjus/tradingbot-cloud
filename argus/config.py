"""Application configuration.

Values come from (in priority order): environment variables with the
``ARGUS_`` prefix, an optional ``.env`` file, and the defaults below.
Secrets (broker API keys) are never written to the database or synced to
the cloud layer; on Windows they will be stored via DPAPI in a later phase,
for now they are read from the environment only.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA", str(Path.home()))
        return Path(base) / "ARGUS"
    return Path(os.environ.get("ARGUS_HOME", Path.cwd() / "data"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARGUS_", env_file=".env", extra="ignore")

    # --- storage ---
    data_dir: Path = Field(default_factory=default_data_dir)

    # --- local API (UI <-> engine), loopback only ---
    api_host: str = "127.0.0.1"
    api_port: int = 7421

    # --- market data ---
    crypto_provider: str = "kraken"  # kraken | binance
    crypto_symbols: list[str] = ["BTC/USD", "ETH/USD"]
    stock_symbols: list[str] = []
    candle_poll_seconds: float = 10.0  # REST fallback poll interval
    stale_after_seconds: float = 90.0  # data older than this => STALE health

    # --- Alpaca (optional; paper by default) ---
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True

    # --- runtime mode (live trading stays locked until explicitly enabled
    #     AND confirmed in the UI; phase 3 has no trading at all) ---
    trading_mode: str = "paper"  # paper | live

    # --- decision & risk (hard limits enforced by RiskSupervisor) ---
    paper_equity: float = 10_000.0
    enter_threshold: float = 70.0   # decision score needed to enter
    risk_per_trade_pct: float = 0.5
    daily_max_loss_pct: float = 2.0
    max_open_positions: int = 4
    max_daily_trades: int = 15

    @property
    def db_path(self) -> Path:
        return self.data_dir / "argus.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(exist_ok=True)
        (self.data_dir / "models").mkdir(exist_ok=True)


settings = Settings()
