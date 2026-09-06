"""End-to-end pipeline behaviour against a real database."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from supplylink.db import read_connection
from supplylink.errors import IngestionError
from supplylink.pipeline import overall_status, run_pipeline
from tests.integration.feeds import GOOD_ROWS, rows_with

pytestmark = pytest.mark.integration


def run(**kwargs) -> object:
    return run_pipeline("erp_core", trigger="test", **kwargs)


class TestHappyPath:
    def test_a_clean_feed_loads_completely(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = run()

        assert result.status == "SUCCEEDED"
        assert result.counters.records_received == 3
        assert result.counters.records_valid == 3
        assert result.counters.records_quarantined == 0
        assert result.counters.rows_inserted == 3

    def test_the_canonical_model_holds_normalised_values(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()

        with read_connection() as connection:
            row = connection.execute(
                text("SELECT * FROM core.products_current WHERE canonical_sku = 'WM001'")
            ).one()

        assert row.canonical_sku == "WM001"  # from 'WM-001'
        assert row.category == "ELECTRONICS"  # from 'Electronics'
        assert row.supplier_id == "SUP-042"
        assert row.unit_price == Decimal("799.0000")
        assert row.quantity_available == 120

    def test_every_stage_is_recorded(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = run()

        with read_connection() as connection:
            stages = dict(
                connection.execute(
                    text("SELECT stage, status FROM meta.run_stages WHERE run_id = :r"),
                    {"r": result.run_id},
                ).all()
            )

        assert stages["ingestion"] == "SUCCEEDED"
        assert stages["validation"] == "SUCCEEDED"
        assert stages["canonical_load"] == "SUCCEEDED"
        assert stages["reconciliation"] == "SUCCEEDED"

    def test_raw_records_preserve_the_source_payload(self, erp_seeded, write_erp_feed) -> None:
        """Staging is the replay source and the lineage anchor."""
        write_erp_feed(GOOD_ROWS)
        result = run()

        with read_connection() as connection:
            payload = connection.execute(
                text("SELECT payload FROM staging.raw_records WHERE run_id = :r AND ordinal = 0"),
                {"r": result.run_id},
            ).scalar_one()

        assert payload["SKU"] == "WM-001", "the raw payload must not be normalised"


class TestQuarantine:
    def test_bad_records_are_quarantined_and_good_ones_still_load(
        self, erp_seeded, write_erp_feed
    ) -> None:
        """Rejecting a whole batch for one bad row destroys more value than it protects."""
        write_erp_feed(rows_with(Stock="-12"))
        result = run()

        assert result.counters.records_received == 3
        assert result.counters.records_quarantined == 1
        assert result.counters.rows_inserted == 2

    def test_the_rejected_payload_is_preserved_whole(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(rows_with(Stock="-12"))
        result = run()

        with read_connection() as connection:
            row = connection.execute(
                text(
                    "SELECT source_record_id, ordinal, raw_payload, failure_count "
                    "FROM dq.quarantined_records WHERE run_id = :r"
                ),
                {"r": result.run_id},
            ).one()

        assert row.source_record_id == "WM-001"
        assert row.ordinal == 0
        assert row.raw_payload["Stock"] == "-12"
        assert row.failure_count >= 1

    def test_each_rule_violation_is_its_own_row(self, erp_seeded, write_erp_feed) -> None:
        """The grain that makes 'top failure categories' a plain GROUP BY."""
        write_erp_feed(rows_with(Stock="-12", Currency="RUPEE"))
        result = run()

        with read_connection() as connection:
            rules = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT rule_name FROM dq.validation_failures "
                        "WHERE run_id = :r AND severity = 'error'"
                    ),
                    {"r": result.run_id},
                ).all()
            }

        assert {"min", "allowed_values"} <= rules

    def test_an_unknown_supplier_fails_referential_integrity(
        self, erp_seeded, write_erp_feed
    ) -> None:
        write_erp_feed(rows_with(Supplier="SUP-999"))
        result = run()

        with read_connection() as connection:
            failure = connection.execute(
                text(
                    "SELECT rule_name, actual_value FROM dq.validation_failures "
                    "WHERE run_id = :r AND rule_name = 'references'"
                ),
                {"r": result.run_id},
            ).one()

        assert failure.actual_value == "SUP-999"
        assert result.counters.records_quarantined == 1

    def test_a_duplicate_key_keeps_the_first_occurrence(self, erp_seeded, write_erp_feed) -> None:
        """Deterministic given the ordinal, so a replay produces the same outcome."""
        rows = [*GOOD_ROWS, {**GOOD_ROWS[0], "Stock": "999"}]
        write_erp_feed(rows)
        result = run()

        assert result.counters.records_quarantined == 1
        with read_connection() as connection:
            quantity = connection.execute(
                text(
                    "SELECT quantity_available FROM core.products_current "
                    "WHERE canonical_sku = 'WM001'"
                )
            ).scalar_one()
        assert quantity == 120, "the first occurrence must win"

    def test_a_warning_does_not_quarantine_the_record(self, erp_seeded, write_erp_feed) -> None:
        """A discontinued item with stock is a real signal, but the record is well formed."""
        write_erp_feed(rows_with(Status="DISCONTINUED", Stock="40"))
        result = run()

        assert result.counters.records_quarantined == 0
        with read_connection() as connection:
            warnings = (
                connection.execute(
                    text(
                        "SELECT rule_name FROM dq.validation_failures "
                        "WHERE run_id = :r AND severity = 'warning'"
                    ),
                    {"r": result.run_id},
                )
                .scalars()
                .all()
            )
        assert "discontinued_items_should_be_empty" in warnings

    def test_a_warning_row_has_no_quarantine_link(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(rows_with(Status="DISCONTINUED", Stock="40"))
        result = run()
        with read_connection() as connection:
            quarantine_id = connection.execute(
                text(
                    "SELECT quarantine_id FROM dq.validation_failures "
                    "WHERE run_id = :r AND severity = 'warning' LIMIT 1"
                ),
                {"r": result.run_id},
            ).scalar_one()
        assert quarantine_id is None


class TestIdempotency:
    def test_the_same_payload_twice_is_skipped(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        first = run()
        second = run()

        assert first.status == "SUCCEEDED"
        assert second.status == "SKIPPED"
        assert second.skipped_reason is not None

    def test_a_skipped_run_writes_nothing(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        run()

        with read_connection() as connection:
            versions = connection.execute(
                text("SELECT count(*) FROM core.product_versions")
            ).scalar_one()
        assert versions == 3, "a duplicate delivery must not create new versions"

    def test_force_reloads_and_is_marked_a_replay(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        replay = run(force=True)

        assert replay.status == "SUCCEEDED"
        assert replay.counters.rows_unchanged == 3, "identical content opens no new version"

        with read_connection() as connection:
            is_replay = connection.execute(
                text("SELECT is_replay FROM meta.pipeline_runs WHERE run_id = :r"),
                {"r": replay.run_id},
            ).scalar_one()
        assert is_replay is True

    def test_record_level_idempotency_survives_a_partial_overlap(
        self, erp_seeded, write_erp_feed
    ) -> None:
        """The real guarantee.

        A file that only partly overlaps a previous one has a different batch
        fingerprint, so the batch guard does not fire. Correctness then rests
        entirely on the record hash — and it holds.
        """
        write_erp_feed(GOOD_ROWS)
        run()

        write_erp_feed([*GOOD_ROWS, {**GOOD_ROWS[0], "SKU": "NEW-001"}])
        second = run()

        assert second.counters.rows_unchanged == 3
        assert second.counters.rows_inserted == 1
        assert second.counters.rows_updated == 0


class TestRunLevelFailures:
    def test_a_missing_file_fails_the_run(self, erp_seeded, isolated_data_dir) -> None:
        with pytest.raises(IngestionError, match="no files matched"):
            run()

    def test_a_failed_run_is_still_recorded(self, erp_seeded, isolated_data_dir) -> None:
        """A crash that erases its own evidence is the worst outcome for an audit trail."""
        with pytest.raises(IngestionError):
            run()

        with read_connection() as connection:
            row = connection.execute(
                text(
                    "SELECT status, error_type, error_message FROM meta.pipeline_runs "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            ).one()

        assert row.status == "FAILED"
        assert row.error_type == "IngestionError"
        assert "no files matched" in row.error_message

    def test_a_missing_header_fails_the_run_not_the_records(
        self, erp_seeded, isolated_data_dir
    ) -> None:
        """The file is not the file the contract describes."""
        landing = isolated_data_dir / "samples" / "erp"
        (landing / "wrong_shape.csv").write_text("SKU,Name\nWM-001,Mouse\n", encoding="utf-8")

        with pytest.raises(IngestionError, match="missing required headers"):
            run()

    def test_nothing_is_loaded_when_the_run_fails(self, erp_seeded, isolated_data_dir) -> None:
        landing = isolated_data_dir / "samples" / "erp"
        (landing / "wrong_shape.csv").write_text("SKU,Name\nWM-001,Mouse\n", encoding="utf-8")
        with pytest.raises(IngestionError):
            run()

        with read_connection() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM core.product_versions")).scalar_one()
                == 0
            )


class TestOverallStatus:
    def test_a_warning_reconciliation_is_not_reported_as_clean(
        self, erp_seeded, write_erp_feed
    ) -> None:
        """Reporting a degrading feed as SUCCEEDED is how it goes unnoticed for a month."""
        write_erp_feed(rows_with(Supplier="SUP-999"))
        result = run()

        assert result.status == "SUCCEEDED"
        assert overall_status(result) in {
            "SUCCEEDED_WITH_WARNINGS",
            "SUCCEEDED_WITH_FAILURES",
        }
