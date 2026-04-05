"""
Structured logging setup for the prediction arbitrage bot.

Configures structlog with a JSON renderer to stdout, optional rotating file
handler, and consistent field set on every log record.

No secret values are ever passed to any logger.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any

import structlog


def setup_logging(
    log_level: str = "INFO",
    log_file: str | None = None,
    max_bytes: int = 10_485_760,  # 10 MB
    backup_count: int = 5,
) -> None:
    """
    Configure structlog with JSON renderer to stdout.

    Args:
        log_level:    Logging level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file:     Optional path to a rotating log file. If None, file logging
                      is disabled.
        max_bytes:    Maximum size of each log file before rotation (bytes).
        backup_count: Number of rotated log files to retain.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # ------------------------------------------------------------------
    # stdlib root logger — structlog bridges into this
    # ------------------------------------------------------------------
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove any pre-existing handlers to avoid duplicate output
    root_logger.handlers.clear()

    # Stdout handler (always present)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(level)
    root_logger.addHandler(stdout_handler)

    # Optional rotating file handler
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        root_logger.addHandler(file_handler)

    # ------------------------------------------------------------------
    # structlog configuration
    # ------------------------------------------------------------------
    shared_processors: list[Any] = [
        # Add ISO 8601 timestamp
        structlog.processors.TimeStamper(fmt="iso"),
        # Add log level string
        structlog.stdlib.add_log_level,
        # Add logger name as "component"
        structlog.stdlib.add_logger_name,
        # Render stack info if present
        structlog.processors.StackInfoRenderer(),
        # Format exception info into the event dict
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            # Bridge structlog events into stdlib logging
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # stdlib formatter that renders the final JSON output
    formatter = structlog.stdlib.ProcessorFormatter(
        # Foreign (stdlib) log records go through these processors first
        foreign_pre_chain=shared_processors,
        # Final renderer: JSON to stdout
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _add_message_field,
            structlog.processors.JSONRenderer(),
        ],
    )

    # Apply the formatter to all handlers
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)


def _add_message_field(
    logger: Any,
    method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """
    Ensure the ``message`` field is always present in the event dict.

    structlog uses ``event`` as the primary message key; we copy it to
    ``message`` so downstream consumers that expect ``message`` still work.
    """
    if "message" not in event_dict:
        event_dict["message"] = event_dict.get("event", "")
    return event_dict


def set_log_level(log_level: str) -> None:
    """
    Dynamically update the log level on all handlers without restart.

    Args:
        log_level: New log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)
