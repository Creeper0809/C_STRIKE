from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
KST = timezone(timedelta(hours=9), name="KST")


class _KSTFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(sep=" ", timespec="seconds")


class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def configure_logging(base_dir: Path) -> Path:
    global _CONFIGURED

    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    if _CONFIGURED:
        return logs_dir

    formatter = _KSTFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    app_handler = RotatingFileHandler(
        logs_dir / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    app_handler.setLevel(logging.INFO)
    app_handler.addFilter(_MaxLevelFilter(logging.WARNING))
    app_handler.setFormatter(formatter)

    error_handler = RotatingFileHandler(
        logs_dir / "error.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(app_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(stream_handler)

    access_logger = logging.getLogger("ops.access")
    access_logger.setLevel(logging.INFO)
    access_logger.handlers.clear()
    access_logger.propagate = False

    access_handler = RotatingFileHandler(
        logs_dir / "access.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    access_handler.setLevel(logging.INFO)
    access_handler.setFormatter(formatter)
    access_logger.addHandler(access_handler)

    _CONFIGURED = True
    return logs_dir
