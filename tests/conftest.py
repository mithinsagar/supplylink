"""Shared test fixtures.

Integration tests run against a **real PostgreSQL database**, not a mock and not
SQLite. That is not thoroughness for its own sake: the most important guarantees
in this system are enforced by Postgres — a GiST exclusion constraint on SCD
validity periods, a partial unique index for idempotency, a stored generated
column, ``NUMERIC`` rounding semantics that the checksum depends on. A test
double would assert that the Python is self-consistent while proving nothing
about any of them.

Isolation is by truncation rather than a wrapping transaction, because the
pipeline deliberately commits several transactions per run (see
``docs/pipeline-lifecycle.md``) and a test-level transaction would hide exactly
the boundaries worth testing.

Application imports inside fixtures are deliberate, not sloppy: settings are
cached on first import, so anything importing ``supplylink.config`` before
:func:`pytest_configure` has set the environment would pin the suite to the
developer's own database instead of the test one.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Connection

from tests.integration.feeds import ERP_HEADERS

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://supplylink:supplylink_local_dev@localhost:5432/supplylink_test"
)

#: Truncated between tests, children before parents so foreign keys stay satisfied.
TABLES_IN_DEPENDENCY_ORDER = (
    "dq.quality_reports",
    "dq.reconciliation_results",
    "dq.validation_failures",
    "dq.quarantined_records",
    "core.inventory_snapshots",
    "core.product_versions",
    "core.products",
    "staging.raw_records",
    "meta.run_stages",
    "meta.pipeline_runs",
    "meta.contract_versions",
    "meta.sources",
    "core.suppliers",
)


def pytest_configure(config: pytest.Config) -> None:
    """Point the application at the test database before anything imports settings."""
    os.environ.setdefault(
        "SUPPLYLINK_DATABASE_URL",
        os.environ.get("SUPPLYLINK_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
    )
    os.environ.setdefault("SUPPLYLINK_CONTRACTS_DIR", str(REPO_ROOT / "contracts"))
    os.environ.setdefault("SUPPLYLINK_DATA_DIR", str(REPO_ROOT / "data"))
    os.environ.setdefault("SUPPLYLINK_SQL_DIR", str(REPO_ROOT / "sql"))
    # Never let a developer's real key be spent by the test suite.
    os.environ["SUPPLYLINK_LLM_ENABLED"] = "false"
    os.environ.pop("SUPPLYLINK_LLM_API_KEY", None)

    # Pin the render width. Rich truncates table cells to fit the terminal, so
    # without this the CLI tests pass or fail depending on how wide the
    # developer's window happens to be — and CI's width is anyone's guess. Set
    # here because the console is constructed at import time.
    os.environ["COLUMNS"] = "160"


@pytest.fixture(scope="session")
def database_url() -> str:
    from supplylink.config import get_settings

    return get_settings().database_url


@pytest.fixture(scope="session")
def _migrated_database(database_url: str) -> Iterator[None]:
    """Migrate the test database to head once per session.

    Downgrades first, so a schema left behind by an interrupted run cannot make
    the suite pass or fail for the wrong reason.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import OperationalError

    from supplylink.db import dispose_engine, get_engine

    try:
        with get_engine().connect():
            pass
    except OperationalError as exc:  # pragma: no cover - environment problem
        pytest.skip(
            f"PostgreSQL is not reachable at {database_url.rsplit('@', maxsplit=1)[-1]}: {exc}. "
            "Start it with `make db-up` or set SUPPLYLINK_TEST_DATABASE_URL."
        )

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["configure_logger"] = False

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    dispose_engine()
    yield
    dispose_engine()


@pytest.fixture
def db(_migrated_database: None) -> Iterator[None]:
    """A clean database for each test."""
    _truncate_all()
    yield
    _truncate_all()


def _truncate_all() -> None:
    from sqlalchemy import text

    from supplylink.db import transaction

    statement = text(
        "TRUNCATE TABLE " + ", ".join(TABLES_IN_DEPENDENCY_ORDER) + " RESTART IDENTITY CASCADE"
    )
    with transaction() as connection:
        connection.execute(statement)


@pytest.fixture
def connection(db: None) -> Iterator[Connection]:
    """A committing connection for tests that set up state directly."""
    from supplylink.db import transaction

    with transaction() as conn:
        yield conn


@pytest.fixture
def seeded(db: None) -> None:
    """Supplier reference data loaded.

    Almost every integration test needs this: referential-integrity validation
    checks against ``core.suppliers``, so without it every record in every feed
    would be quarantined for an unknown supplier.
    """
    from supplylink.db import transaction
    from supplylink.warehouse.reference import seed_suppliers

    with transaction() as conn:
        seed_suppliers(conn)


@pytest.fixture
def contracts_dir() -> Path:
    return REPO_ROOT / "contracts"


@pytest.fixture
def erp_contract():
    from supplylink.contracts.loader import load_contract

    return load_contract(REPO_ROOT / "contracts" / "erp.yaml")


@pytest.fixture
def acme_contract():
    from supplylink.contracts.loader import load_contract

    return load_contract(REPO_ROOT / "contracts" / "suppliers" / "acme.yaml")


@pytest.fixture
def sample_data_dir(tmp_path: Path) -> Path:
    """An isolated data directory a test can write CSV fixtures into."""
    (tmp_path / "samples").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the application's data directory at a temporary tree.

    Also copies in the supplier reference data, so ``supplylink db seed`` and the
    ``seeded`` fixture keep working.
    """
    from supplylink.config import reset_settings_cache

    (tmp_path / "samples" / "erp").mkdir(parents=True)
    (tmp_path / "reference").mkdir(parents=True)
    (tmp_path / "reference" / "suppliers.csv").write_text(
        (REPO_ROOT / "data" / "reference" / "suppliers.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setenv("SUPPLYLINK_DATA_DIR", str(tmp_path))
    reset_settings_cache()
    yield tmp_path
    reset_settings_cache()


@pytest.fixture
def write_erp_feed(isolated_data_dir: Path) -> Callable[[list[dict[str, str]], str], Path]:
    """Write an ERP-shaped CSV into the isolated landing zone.

    Replaces any previous file, mirroring a real landing zone where each night's
    extract supersedes the last.
    """

    def _write(rows: list[dict[str, str]], name: str = "erp_extract.csv") -> Path:
        landing = isolated_data_dir / "samples" / "erp"
        for existing in landing.glob("*.csv"):
            existing.unlink()
        path = landing / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ERP_HEADERS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    return _write


@pytest.fixture
def erp_seeded(isolated_data_dir: Path, db: None) -> None:
    """Supplier reference data loaded from the isolated directory."""
    from supplylink.db import transaction
    from supplylink.warehouse.reference import seed_suppliers

    with transaction() as connection:
        seed_suppliers(connection)


@pytest.fixture
def frozen_now():
    """A fixed instant, for asserting on SCD validity boundaries."""
    from datetime import UTC, datetime

    return datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
