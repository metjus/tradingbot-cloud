"""Training CLI: ``python -m argus.ml --symbols BTC/USD,ETH/USD --days 60``.

Downloads history, backtests all strategies to harvest labelled signals,
walk-forward validates a candidate setup-scorer and registers it.
Activation (``--activate N``) is a separate, deliberate step.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from ..config import settings
from ..db.database import Database
from ..monitoring.logging_setup import setup_logging
from .registry import ModelRegistry
from .training import train_from_history


def main() -> None:
    ap = argparse.ArgumentParser(prog="argus.ml")
    ap.add_argument("--symbols", default="BTC/USD,ETH/USD")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--activate", type=int, default=None,
                    help="activate setup-scorer version N (no training)")
    ap.add_argument("--rollback", action="store_true",
                    help="roll back setup-scorer to the previous archived version")
    ap.add_argument("--list", action="store_true", help="list registered models")
    args = ap.parse_args()

    settings.ensure_dirs()
    setup_logging(settings.data_dir, logging.INFO)
    db = Database(settings.db_path)
    registry = ModelRegistry(db, settings.data_dir / "models")

    if args.list:
        print(json.dumps(registry.list_versions(), indent=2))
        return
    if args.activate is not None:
        registry.activate("setup-scorer", args.activate)
        print(f"activated setup-scorer v{args.activate}")
        return
    if args.rollback:
        v = registry.rollback("setup-scorer")
        print(f"rolled back to v{v}" if v else "no archived version to roll back to")
        return

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    out = asyncio.run(train_from_history(db, registry, symbols, days=args.days))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
