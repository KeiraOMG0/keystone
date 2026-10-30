"""
Structured logging setup for the bot.

Exposes:
    setup_logging(level)  — configure the root logger once at startup
    get_logger(name)      — retrieve a named child logger
"""

import logging
import os
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with handlers for stdout and a log file.

    A bot_startup.log file handler is attached (in addition to stdout) so
    that startup failures are captured even when running as a frozen exe
    launched by double-click, where the console window can close before
    the operator reads the output. It is skipped when KICKBOT_SUPERVISED=1
    (set by supervisor.py, which already saves stdout to logs/supervisor/bot.log).

    Args:
        level: A logging level string, e.g. "DEBUG", "INFO", "WARNING".
               Falls back to INFO if the value is not recognised.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # sys.executable's directory when frozen by PyInstaller, else the script dir.
    base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
    log_path = os.path.join(base_dir, "bot_startup.log")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(numeric_level)
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    # Avoid duplicate handlers if setup_logging is called more than once.
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)

    # Under supervisor.py stdout already lands in logs/supervisor/bot.log (rotated
    # weekly). A FileHandler would only duplicate it and hold bot_startup.log open,
    # which blocks that file's rotation on Windows, so it is skipped there.
    if os.environ.get("KICKBOT_SUPERVISED") != "1":
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # ERROR+ records also go to the in-memory ring behind the owner-only Debug
    # page's Errors tab (core/error_ring.py).
    from core.error_ring import ErrorRingHandler

    root_logger.addHandler(ErrorRingHandler())

    # Silence chatty discord.py internals unless the operator wants DEBUG.
    if numeric_level > logging.DEBUG:
        for noisy in ("discord.gateway", "discord.client"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (child of the root logger configured by setup_logging).

    Args:
        name: Typically the module name, e.g. "main" or "cogs.kick".

    Returns:
        A :class:`logging.Logger` instance.
    """
    return logging.getLogger(name)
