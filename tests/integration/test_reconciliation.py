"""Reconciliation, and specifically the case volume checks cannot see.

The test that matters most here is
:meth:`TestContentReconciliation.test_detects_corruption_that_row_counts_miss`.
Everything else is supporting evidence.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from supplylink.db import read_connection, transaction
from supplylink.pipeline import run_pipeline
from supplylink.reconciliation.checksums import (
    render_target_checksum_sql,
    source_checksum,
    target_checksum,
)
from supplylink.reconciliation.metrics import CheckStatus
from tests.integration.feeds import GOOD_ROWS, rows_with

pytestmark = pytest.mark.integration


def run() -> object:
    return run_pipeline("erp_core", trigger="test")


def check(result, name: str, dimension: str = "*"):
    return next(
        c for c in result.reconciliation.checks if c.check_name == name and c.dimension == dimension
    )


class TestContentReconciliation:
    def test_python_and_postgres_agree_on_a_clean_load(self, erp_seeded, write_erp_feed) -> None:
        """Two independent implementations of the same canonicalisation.

        Comparing a checksum against one computed by the same code over the same
        objects proves only that the objects did not change. Agreement between
        Python and Postgres proves the data survived mapping, the driver, the
        type system and storage.
        """
        write_erp_feed(GOOD_ROWS)
        result = run()

        checksum = check(result, "content_checksum")
        assert checksum.status is CheckStatus.PASS
        assert checksum.source_value == checksum.target_value
        assert checksum.kind.value == "content"

    def test_detects_corruption_that_row_counts_miss(self, erp_seeded, write_erp_feed) -> None:
        """The whole argument for content reconciliation.

        A price is altered directly in the warehouse. Every row is still present,
        so every volume check passes — and the content checksum still catches it.
        """
        write_erp_feed(GOOD_ROWS)
        result = run()
        assert check(result, "content_checksum").status is CheckStatus.PASS

        with transaction() as connection:
            connection.execute(
                text(
                    "UPDATE core.product_versions SET unit_price = unit_price + 1 "
                    "WHERE canonical_sku = 'WM001' AND is_current"
                )
            )

        from supplylink.config import get_settings
        from supplylink.contracts.loader import ContractRegistry

        contract = ContractRegistry.discover(get_settings().contracts_dir).get("erp_core")
        columns = contract.reconciliation.checksum_columns
        skus = ["WM001", "KB002", "MN003"]

        with read_connection() as connection:
            row_count, corrupted_digest = target_checksum(connection, skus, columns)

        assert row_count == 3, "the row count is unchanged — volume reconciliation sees nothing"
        assert corrupted_digest != check(result, "content_checksum").target_value, (
            "the content checksum must detect a value that changed underneath us"
        )

    def test_source_checksum_is_order_independent(self, erp_seeded, write_erp_feed) -> None:
        from supplylink.config import get_settings
        from supplylink.contracts.loader import ContractRegistry
        from supplylink.ingestion import build_adapter
        from supplylink.transformation.mapper import map_record

        write_erp_feed(GOOD_ROWS)
        contract = ContractRegistry.discover(get_settings().contracts_dir).get("erp_core")
        records = [
            map_record(r.payload, contract, ordinal=r.ordinal)
            for r in build_adapter(contract).extract().records
        ]
        columns = contract.reconciliation.checksum_columns

        assert source_checksum(records, columns) == source_checksum(
            list(reversed(records)), columns
        )

    def test_generated_sql_only_admits_canonical_columns(self) -> None:
        from supplylink.errors import ReconciliationError

        with pytest.raises(ReconciliationError, match="unknown canonical columns"):
            render_target_checksum_sql(["canonical_sku", "; DROP TABLE core.products; --"])

    def test_generated_sql_binds_the_sku_list(self) -> None:
        sql = render_target_checksum_sql(["canonical_sku", "unit_price"])
        assert "ANY(:skus)" in sql
        assert "to_char(pv.unit_price" in sql


class TestVolumeReconciliation:
    def test_row_count_matches_after_a_clean_load(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = run()

        row_count = check(result, "row_count")
        assert row_count.status is CheckStatus.PASS
        assert row_count.source_value == row_count.target_value == "3"

    def test_quarantine_rate_is_computed(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(rows_with(Stock="-12"))
        result = run()

        rate = check(result, "quarantine_rate_pct")
        assert rate.delta_pct is not None
        assert float(rate.delta_pct) == pytest.approx(33.3333, abs=0.01)

    def test_duplicate_rate_reflects_duplicate_keys(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed([*GOOD_ROWS, dict(GOOD_ROWS[0])])
        result = run()

        assert check(result, "duplicate_rate_pct").details["duplicate_records"] == 1

    def test_orphan_rate_reflects_unknown_references(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(rows_with(Supplier="SUP-999"))
        result = run()

        assert check(result, "orphan_rate_pct").details["orphaned_records"] == 1

    def test_distinct_key_count_is_informational(self, erp_seeded, write_erp_feed) -> None:
        """Reported but not thresholded — a feed with legitimate repeats is not wrong."""
        write_erp_feed(GOOD_ROWS)
        result = run()

        distinct = check(result, "distinct_key_count")
        assert distinct.status is CheckStatus.PASS
        assert distinct.target_value == "3"


class TestNullRateBaseline:
    def test_first_run_has_no_baseline_and_says_so(self, erp_seeded, write_erp_feed) -> None:
        """Reporting PASS is honest; implying a comparison happened would not be."""
        write_erp_feed(GOOD_ROWS)
        result = run()

        null_check = check(result, "null_rate_pct", "product_name")
        assert null_check.status is CheckStatus.PASS
        assert "first run" in null_check.details["baseline"]

    def test_second_run_compares_against_the_previous(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Price="899.00"))
        result = run()

        null_check = check(result, "null_rate_pct", "product_name")
        assert null_check.details["baseline"] == "previous successful run"
        assert null_check.target_value is not None


class TestPersistence:
    def test_every_check_is_stored(self, erp_seeded, write_erp_feed) -> None:
        """A reconciliation that is printed and forgotten cannot answer 'when did this start?'."""
        write_erp_feed(GOOD_ROWS)
        result = run()

        with read_connection() as connection:
            stored = connection.execute(
                text(
                    "SELECT check_name, check_kind, status FROM dq.reconciliation_results "
                    "WHERE run_id = :r"
                ),
                {"r": result.run_id},
            ).all()

        assert len(stored) == len(result.reconciliation.checks)
        assert {row.check_kind for row in stored} == {"volume", "content"}

    def test_the_run_records_the_overall_verdict(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = run()

        with read_connection() as connection:
            status = connection.execute(
                text("SELECT reconciliation_status FROM meta.pipeline_runs WHERE run_id = :r"),
                {"r": result.run_id},
            ).scalar_one()

        assert status == result.reconciliation.status.value

    def test_overall_status_is_the_worst_check(self, erp_seeded, write_erp_feed) -> None:
        """Averaging a failure away is how bad data ships."""
        write_erp_feed(rows_with(Supplier="SUP-999"))
        result = run()

        worst = max(c.status.rank for c in result.reconciliation.checks)
        assert result.reconciliation.status.rank == worst
        assert result.reconciliation.status is not CheckStatus.PASS
