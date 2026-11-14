"""Logging: console + rotating file in the data directory."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(data_dir: Path, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(console)

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fileh = logging.handlers.RotatingFileHandler(
        log_dir / "argus.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(fileh)
