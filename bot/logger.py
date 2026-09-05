"""Structured logging configuration for console (Azure-compatible) and rotating file."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE_PATH = LOGS_DIR / "bot.log"

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level_name: str = "INFO") -> logging.Logger:
    """
    Initializes structured logging:
    - stdout handler with explicit line-flushing (optimal for Azure App Service log stream)
    - RotatingFileHandler under logs/bot.log (5 MB max, 5 backups)
    """
    # Ensure logs directory exists
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, level_name.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers if setup_logging is called multiple times
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    # 1. Console (stdout) handler - Azure App Service streams stdout in real time
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 2. Rotating File Handler - 5 MB per file, keeps 5 backups
    file_handler = RotatingFileHandler(
        filename=str(LOG_FILE_PATH),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Quieten noisy third-party loggers if not in DEBUG mode
    if numeric_level > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.INFO)
        logging.getLogger("azure").setLevel(logging.WARNING)

    logger = logging.getLogger("bot")
    logger.info("Logging initialized at level %s. Log file: %s", level_name, LOG_FILE_PATH)
    return logger


# Default bot logger instance
logger = logging.getLogger("bot")
