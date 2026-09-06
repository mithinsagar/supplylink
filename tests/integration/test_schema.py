"""The schema itself: migrations, drift, and the constraints that carry the design.

Several of SupplyLink's guarantees are enforced by Postgres rather than by
Python. These tests assert the constraints exist *and* that they actually bite,
because a constraint that was silently dropped in a migration would leave the
application looking correct while the invariant it relies on quietly disappeared.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from supplylink.db import read_connection
from supplylink.warehouse.schema import ALL_TABLES

pytestmark = pytest.mark.integration


class TestMigrations:
    def test_every_declared_table_exists(self, db: None) -> None:
        with read_connection() as connection:
            inspector = inspect(connection)
            for table in ALL_TABLES:
                assert inspector.has_table(table.name, schema=table.schema), (
                    f"{table.schema}.{table.name} is missing from the migrated database"
                )

    def test_metadata_matches_the_migrated_database(self, db: None) -> None:
        """Guards the one duplication in the project.

        DDL lives in the migration as readable SQL; ``warehouse.schema`` mirrors
        it for DML. This test turns that duplication from a liability into a
        checked guarantee.
        """
        with read_connection() as connection:
            inspector = inspect(connection)
            for table in ALL_TABLES:
                columns = inspector.get_columns(table.name, schema=table.schema)
                actual = {column["name"] for column in columns}
                declared = {column.name for column in table.columns}
                assert declared == actual, (
                    f"{table.schema}.{table.name} drifted — "
                    f"only in metadata: {declared - actual}; "
                    f"only in database: {actual - declared}"
                )

    def test_views_exist(self, db: None) -> None:
        with read_connection() as connection:
            views = set(inspect(connection).get_view_names(schema="core"))
            assert "products_current" in views
            assert "run_failure_summary" in set(inspect(connection).get_view_names(schema="dq"))

    def test_btree_gist_is_installed(self, db: None) -> None:
        """Without it the SCD exclusion constraint cannot exist."""
        with read_connection() as connection:
            installed = connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
            ).scalar_one_or_none()
        assert installed == 1


class TestScdConstraints:
    """The invariants that make a point-in-time query provably unambiguous."""

    def test_is_current_is_a_generated_column(self, db: None) -> None:
        """Generated, so the flag and the validity window can never disagree."""
        with read_connection() as connection:
            generated = connection.execute(
                text(
                    "SELECT attgenerated FROM pg_attribute "
                    "WHERE attrelid = 'core.product_versions'::regclass AND attname = 'is_current'"
                )
            ).scalar_one()
        assert generated == "s"

    def test_exclusion_constraint_exists(self, db: None) -> None:
        with read_connection() as connection:
            constraint = connection.execute(
                text(
                    "SELECT contype FROM pg_constraint "
                    "WHERE conname = 'product_versions_no_overlap_ex'"
                )
            ).scalar_one_or_none()
        assert constraint == "x", "the GiST exclusion constraint is missing"

    def test_partial_unique_index_on_current_exists(self, db: None) -> None:
        with read_connection() as connection:
            definition = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'product_versions_current_uk'"
                )
            ).scalar_one_or_none()
        assert definition is not None
        assert "WHERE is_current" in definition

    def test_idempotency_index_excludes_replays(self, db: None) -> None:
        with read_connection() as connection:
            definition = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'pipeline_runs_batch_idem_uk'"
                )
            ).scalar_one()
        assert "SUCCEEDED" in definition
        assert "is_replay" in definition


class TestRunAccountingConstraints:
    """Counter identities are enforced by the database, so a loader bug is loud."""

    def _insert_run(self, connection, **overrides) -> None:
        from uuid import uuid4

        connection.execute(
            text(
                "INSERT INTO meta.sources (source_id, display_name, source_type, precedence) "
                "VALUES ('t_source', 'T', 'csv', 50) ON CONFLICT DO NOTHING"
            )
        )
        connection.execute(
            text(
                "INSERT INTO meta.contract_versions "
                "(source_id, version, content_hash, definition, effective_from) "
                "VALUES ('t_source', '1.0.0', :h, '{}'::jsonb, '2026-01-01') "
                "ON CONFLICT DO NOTHING"
            ),
            {"h": "a" * 64},
        )
        contract_version_id = connection.execute(
            text(
                "SELECT contract_version_id FROM meta.contract_versions "
                "WHERE source_id = 't_source'"
            )
        ).scalar_one()

        values = {
            "run_id": uuid4(),
            "cv": contract_version_id,
            "status": "SUCCEEDED",
            "finished_at": datetime.now(UTC),
            "received": 10,
            "valid": 10,
            "quarantined": 0,
            "inserted": 10,
            "updated": 0,
            "unchanged": 0,
            "superseded": 0,
            **overrides,
        }
        connection.execute(
            text(
                "INSERT INTO meta.pipeline_runs "
                "(run_id, source_id, contract_version_id, trigger, status, finished_at, "
                " records_received, records_valid, records_quarantined, "
                " rows_inserted, rows_updated, rows_unchanged, rows_superseded) "
                "VALUES (:run_id, 't_source', :cv, 'test', :status, :finished_at, "
                " :received, :valid, :quarantined, :inserted, :updated, :unchanged, :superseded)"
            ),
            values,
        )

    def test_a_balanced_run_is_accepted(self, connection) -> None:
        self._insert_run(connection)

    def test_intake_must_balance_on_success(self, connection) -> None:
        """received must equal valid + quarantined, or the loader lost records."""
        with pytest.raises(IntegrityError, match="intake_balance"):
            self._insert_run(connection, received=10, valid=8, quarantined=0, inserted=8)

    def test_load_must_balance_on_success(self, connection) -> None:
        with pytest.raises(IntegrityError, match="load_balance"):
            self._insert_run(connection, inserted=3)

    def test_a_failed_run_is_exempt_from_balance(self, connection) -> None:
        """A run that died halfway has partial counts, and that is legitimate."""
        self._insert_run(
            connection, status="FAILED", received=10, valid=4, quarantined=0, inserted=0
        )

    def test_a_running_run_must_have_no_finish_time(self, connection) -> None:
        # Deliberately after started_at, so this exercises finish_ck rather than
        # tripping duration_ck first.
        with pytest.raises(IntegrityError, match="finish_ck"):
            self._insert_run(
                connection,
                status="RUNNING",
                finished_at=datetime.now(UTC) + timedelta(minutes=1),
            )

    def test_a_terminal_run_must_have_a_finish_time(self, connection) -> None:
        with pytest.raises(IntegrityError, match="finish_ck"):
            self._insert_run(connection, finished_at=None)

    def test_finish_cannot_precede_start(self, connection) -> None:
        with pytest.raises(IntegrityError, match="duration_ck"):
            self._insert_run(connection, finished_at=datetime.now(UTC) - timedelta(days=1))


class TestQuarantineConstraint:
    def test_an_error_must_be_attached_to_a_quarantined_record(self, connection) -> None:
        """The severity/quarantine pairing is enforced, so the two cannot disagree."""
        with pytest.raises(IntegrityError, match="quarantine_ck"):
            connection.execute(
                text(
                    "INSERT INTO dq.validation_failures "
                    "(quarantine_id, run_id, source_id, raw_record_id, rule_name, "
                    " rule_definition, expected, message, severity) "
                    "VALUES (NULL, gen_random_uuid(), 'x', 1, 'min', '{}'::jsonb, "
                    "'>= 0', 'm', 'error')"
                )
            )
