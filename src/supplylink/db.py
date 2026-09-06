"""Database access.

SupplyLink uses **SQLAlchemy Core**, not the ORM. The workload is set-based —
bulk staging inserts, an SCD merge, aggregate reconciliation queries — where the
ORM's identity map and unit-of-work buy nothing and hide the SQL that is the
interesting part of this project. Core keeps statements composable and
parameterised while leaving them legible.

Analytical statements (reconciliation, point-in-time lookups, reporting) live as
files under ``sql/`` and are loaded through :func:`load_sql`. Keeping them in
``.sql`` files means they can be run in ``psql`` and reviewed by a data engineer
who does not read Python.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError

from supplylink.config import get_settings
from supplylink.errors import PipelineError
from supplylink.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, TextClause

log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine.

    ``pool_pre_ping`` costs one round trip per checkout and removes the entire
    class of "stale connection after the database restarted" failures, which is
    exactly what happens when Postgres and the app start together under Compose.
    """
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=True,
        echo=settings.db_echo,
        future=True,
    )

    timeout_ms = settings.db_statement_timeout_ms

    @event.listens_for(engine, "connect")
    def _set_session_defaults(dbapi_connection: Any, _record: Any) -> None:
        """A runaway query should fail the run, not wedge the pipeline forever."""
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {timeout_ms}")
            cursor.execute("SET TIME ZONE 'UTC'")

    return engine


def dispose_engine() -> None:
    """Close pooled connections and drop the cached engine (tests, CLI shutdown)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


@contextmanager
def transaction() -> Iterator[Connection]:
    """A connection wrapped in one transaction: commit on success, roll back on error.

    This is the only sanctioned way to write. Transaction boundaries are a
    deliberate part of the pipeline's design rather than an accident of where
    ``commit()`` happened to be called — see ``docs/pipeline-lifecycle.md``.
    """
    engine = get_engine()
    with engine.begin() as connection:
        yield connection


@contextmanager
def read_connection() -> Iterator[Connection]:
    """A read-only connection. Rolled back on exit so nothing can leak a write."""
    engine = get_engine()
    with engine.connect() as connection:
        yield connection


def healthcheck() -> tuple[bool, str]:
    """``(ok, detail)`` for the ``/health`` endpoint and the CLI."""
    try:
        with read_connection() as connection:
            version = connection.execute(text("SELECT version()")).scalar_one()
        return True, str(version).split(",", 1)[0]
    except SQLAlchemyError as exc:  # pragma: no cover - exercised via failure tests
        return False, type(exc).__name__


@lru_cache(maxsize=64)
def load_sql(name: str) -> TextClause:
    """Load ``sql/<name>.sql`` as a bound-parameter statement.

    Files use SQLAlchemy's colon-prefixed placeholders, so every value is bound
    by the driver and never interpolated into the statement text.

    One trap worth knowing: SQLAlchemy scans the whole file for placeholders,
    **including SQL comments**. A colon-prefixed word written casually in a
    comment becomes a required bind parameter and the statement then fails with
    a confusing "a value is required for bind parameter" at runtime. Only name
    real parameters that way.
    """
    if not name.replace("_", "").replace("/", "").isalnum():
        raise PipelineError(f"unsafe SQL resource name: {name!r}")
    path = Path(get_settings().sql_dir) / f"{name}.sql"
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"cannot read SQL resource {path}") from exc
    return text(body)
