"""콘솔 + 일자별 파일 로그."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

LOGGER_NAME = "strange"
_FORMAT = "[%(asctime)s] %(levelname)-7s %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if getattr(logger, "_configured", False):
        return logger

    logger.setLevel(level.upper())
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, _DATE_FORMAT)

    # Windows 콘솔(cp949)에서도 한글/특수문자가 깨지지 않도록 UTF-8 로 재설정
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)

    logger._configured = True  # type: ignore[attr-defined]
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log_step_error(logger: logging.Logger, step: str, exc: BaseException, attempt: int, max_attempts: int) -> None:
    logger.error(
        "ERROR | STEP=%s | EXCEPTION=%s: %s | RETRY COUNT=%d/%d",
        step, type(exc).__name__, exc, attempt, max_attempts,
    )
