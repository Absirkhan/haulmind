"""Structured logging via structlog. Console-friendly by default, JSON when LOG_JSON=1."""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure structlog + stdlib logging once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # Quiet noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "uvicorn.access", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: structlog.types.Processor
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
