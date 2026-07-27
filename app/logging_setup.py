from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(*, log_dir: Path | None = None) -> None:
    """Configure app loggers: console + dedicated POC log file."""
    root = Path(__file__).resolve().parents[2]
    target_dir = log_dir or root / ".run" / "logs"
    target_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app_logger = logging.getLogger("app")
    if app_logger.handlers:
        return

    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    app_logger.addHandler(console)

    poc_file = logging.FileHandler(target_dir / "poc.log", encoding="utf-8")
    poc_file.setFormatter(formatter)
    app_logger.addHandler(poc_file)
