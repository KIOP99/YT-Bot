"""
core/logging_config.py
----------------------
Simple stdlib logging with console + rotating file handlers.
Call `setup_logging()` once at startup in each entrypoint.
Works with Python 3.14+ and all library versions.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Any


def setup_logging() -> None:
    """Configure stdlib logging with console + rotating file output."""
    from core.config import settings

    os.makedirs(os.path.dirname(settings.log_file), exist_ok=True)

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Avoid adding handlers multiple times (e.g. on uvicorn reload)
    if root_logger.handlers:
        return

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    # Rotating file handler (10 MB × 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    console_handler.setFormatter(fmt)
    file_handler.setFormatter(fmt)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers
    for noisy in ("httpcore", "httpx", "urllib3", "googleapiclient", "discord"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    """Return a structured logger bound to the given name."""
    return _BoundLogger(logging.getLogger(name))


class _BoundLogger:
    """Thin wrapper that provides structlog-style keyword-argument logging."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @staticmethod
    def _fmt(msg: str, **kw: Any) -> str:
        if kw:
            kw_str = " | ".join(f"{k}={v!r}" for k, v in kw.items())
            return f"{msg} | {kw_str}"
        return msg

    def debug(self, msg: str, **kw: Any) -> None:
        self._logger.debug(self._fmt(msg, **kw))

    def info(self, msg: str, **kw: Any) -> None:
        self._logger.info(self._fmt(msg, **kw))

    def warning(self, msg: str, **kw: Any) -> None:
        self._logger.warning(self._fmt(msg, **kw))

    def warn(self, msg: str, **kw: Any) -> None:
        self._logger.warning(self._fmt(msg, **kw))

    def error(self, msg: str, **kw: Any) -> None:
        self._logger.error(self._fmt(msg, **kw))

    def exception(self, msg: str, **kw: Any) -> None:
        self._logger.exception(self._fmt(msg, **kw))

    def critical(self, msg: str, **kw: Any) -> None:
        self._logger.critical(self._fmt(msg, **kw))
