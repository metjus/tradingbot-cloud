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
    log = logging.getLogger("argus")
    log.info("starting ARGUS engine (data dir %s)", settings.data_dir)
    try:
        engine = Engine(settings)
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except BaseException:
        log.exception("ARGUS engine crashed")
        raise


if __name__ == "__main__":
    main()
