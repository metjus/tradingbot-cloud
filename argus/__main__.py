"""Entry point: ``python -m argus``."""

from __future__ import annotations

import asyncio
import logging

from .config import settings
from .engine import Engine
from .monitoring.logging_setup import setup_logging


def main() -> None:
    settings.ensure_dirs()
    setup_logging(settings.data_dir, logging.INFO)
    engine = Engine(settings)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
