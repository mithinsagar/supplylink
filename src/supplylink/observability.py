"""Structured logging with ambient pipeline context.

Every log line emitted while a run is in flight carries ``run_id``, ``source_id``
and ``stage`` without any call site having to pass them. The context lives in
:mod:`contextvars`, so it survives across module boundaries and is safe under
concurrency.

Two renderers are available: ``console`` (aligned key=value, for humans) and
``json`` (one JSON object per line, for log shipping). Neither uses ``print``.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from supplylink.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar("supplylink_log_context", default=_EMPTY)

# Attributes present on every LogRecord; anything else was supplied via `extra`.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


def get_log_context() -> dict[str, Any]:
    """The context fields currently bound to this execution."""
    return dict(_CONTEXT.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields onto every log record emitted inside the block.

    Nested blocks merge, and unbinding is exception-safe::

        with log_context(run_id=run_id, source_id="acme"):
            with log_context(stage="validation"):
                log.info("records validated", extra={"valid": 14200})
    """
    token: Token[Mapping[str, Any]] = _CONTEXT.set(MappingProxyType({**_CONTEXT.get(), **fields}))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class _ContextFilter(logging.Filter):
    """Copies the ambient context onto each record before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _CONTEXT.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED and not key.startswith("_")
    }


class ConsoleFormatter(logging.Formatter):
    """``time level logger message key=value …`` — dense but readable."""

    _ORDER = ("run_id", "source_id", "stage")

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
            f"{record.name:<28} {record.getMessage()}"
        )
        extras = _record_extras(record)
        ordered = [(k, extras.pop(k)) for k in self._ORDER if k in extras]
        ordered += sorted(extras.items())
        if ordered:
            base += "  " + " ".join(f"{k}={_scalar(v)}" for k, v in ordered)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_record_extras(record),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def _scalar(value: Any) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Install SupplyLink's handler on the root logger. Idempotent."""
    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    resolved_fmt = fmt or settings.log_format

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if resolved_fmt == "json" else ConsoleFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # SQLAlchemy echoes every statement at INFO; that is opt-in via SUPPLYLINK_DB_ECHO.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """A module logger. Use ``get_logger(__name__)``."""
    return logging.getLogger(name)
