"""Engine entry point as a regular module.

``python -m argus`` and the packaged EXE (``ARGUS.exe --engine``) both call
:func:`run`. It lives here rather than in ``argus/__main__.py`` because
PyInstaller does not bundle ``__main__`` submodules.
"""

from __future__ import annotations

import asyncio
import logging

from .config import settings
from .engine import Engine
from .monitoring.logging_setup import setup_logging


def run() -> None:
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
