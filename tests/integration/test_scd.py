"""Slowly Changing Dimension Type 2.

The behaviour that makes "what was the price on 14 March?" answerable, and the
database constraints that make the answer provably unambiguous.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from supplylink.db import read_connection, transaction
from supplylink.pipeline import run_pipeline
from tests.integration.feeds import GOOD_ROWS, rows_with

pytestmark = pytest.mark.integration


def run() -> object:
    return run_pipeline("erp_core", trigger="test")


def versions(sku: str = "WM001") -> list:
    with read_connection() as connection:
        return connection.execute(
            text(
                "SELECT version_number, product_name, unit_price, supplier_id, status, "
                "       effective_from, effective_to, is_current, record_hash "
                "FROM core.product_versions WHERE canonical_sku = :s ORDER BY version_number"
            ),
            {"s": sku},
        ).all()


class TestVersioning:
    def test_a_changed_attribute_opens_a_new_version(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Price="899.00"))
        result = run()

        assert result.counters.rows_updated == 1
        assert result.counters.rows_unchanged == 2

        history = versions()
        assert len(history) == 2
        assert history[0].unit_price == Decimal("799.0000")
        assert history[1].unit_price == Decimal("899.0000")

    def test_the_previous_version_is_preserved_not_overwritten(
        self, erp_seeded, write_erp_feed
    ) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Price="899.00"))
        run()

        history = versions()
        assert history[0].is_current is False
        assert history[0].effective_to is not None
        assert history[1].is_current is True
        assert history[1].effective_to is None

    def test_renaming_the_file_does_not_defeat_idempotency(
        self, erp_seeded, write_erp_feed
    ) -> None:
        """The fingerprint is over payload content, not over the filename."""
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(GOOD_ROWS, name="same_content_new_name.csv")
        assert run().status == "SKIPPED"

    def test_an_unchanged_record_opens_no_version(self, erp_seeded, write_erp_feed) -> None:
        """The record-level guarantee, isolated from the batch-level one.

        Forcing past the batch fingerprint means correctness rests entirely on
        the record hash: identical content produces no new version.
        """
        write_erp_feed(GOOD_ROWS)
        run()
        result = run_pipeline("erp_core", trigger="test", force=True)

        assert result.counters.rows_unchanged == 3
        assert result.counters.rows_updated == 0
        assert len(versions()) == 1

    def test_a_changed_measure_opens_no_version(self, erp_seeded, write_erp_feed) -> None:
        """The reason quantity is a fact, not a tracked attribute.

        Versioning it would open a new dimension row nightly for every SKU and
        bury the price and naming changes that actually matter.
        """
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Stock="500"))
        result = run()

        assert result.counters.rows_unchanged == 3
        assert len(versions()) == 1

    def test_the_measure_is_still_recorded_as_a_fact(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Stock="500"))
        run()

        with read_connection() as connection:
            quantities = (
                connection.execute(
                    text(
                        "SELECT quantity_available FROM core.inventory_snapshots "
                        "WHERE canonical_sku = 'WM001' ORDER BY observed_at"
                    )
                )
                .scalars()
                .all()
            )

        assert quantities == [120, 500]

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("Name", "Wireless Mouse Pro"),
            ("Category", "Accessories"),
            ("Supplier", "SUP-007"),
            ("Price", "899.00"),
            ("Status", "DISCONTINUED"),
        ],
    )
    def test_each_tracked_attribute_triggers_a_version(
        self, erp_seeded, write_erp_feed, column: str, value: str
    ) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(**{column: value}))
        result = run()

        assert result.counters.rows_updated == 1, f"a change to {column} must open a version"

    def test_version_numbers_increment(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        for price in ("899.00", "999.00"):
            write_erp_feed(rows_with(Price=price))
            run()

        assert [v.version_number for v in versions()] == [1, 2, 3]

    def test_record_hashes_differ_between_versions(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Price="899.00"))
        run()

        hashes = [v.record_hash for v in versions()]
        assert hashes[0] != hashes[1]


class TestValidityPeriods:
    def test_versions_abut_exactly_with_no_gap(self, erp_seeded, write_erp_feed) -> None:
        """One load timestamp per batch, so a point-in-time query never falls in a hole."""
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Price="899.00"))
        run()

        history = versions()
        assert history[0].effective_to == history[1].effective_from

    def test_point_in_time_returns_exactly_one_version(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Price="899.00"))
        run()

        history = versions()
        boundary = history[0].effective_to

        for moment, expected in (
            (history[0].effective_from + timedelta(microseconds=1), Decimal("799.0000")),
            (boundary, Decimal("899.0000")),  # half-open: the new one is in effect
            (boundary - timedelta(microseconds=1), Decimal("799.0000")),
        ):
            with read_connection() as connection:
                rows = connection.execute(
                    text(
                        "SELECT unit_price FROM core.product_versions "
                        "WHERE canonical_sku = 'WM001' AND effective_from <= :t "
                        "  AND (effective_to > :t OR effective_to IS NULL)"
                    ),
                    {"t": moment},
                ).all()
            assert len(rows) == 1, f"expected exactly one version in effect at {moment}"
            assert rows[0].unit_price == expected

    def test_before_the_first_version_nothing_is_in_effect(
        self, erp_seeded, write_erp_feed
    ) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        first = versions()[0]

        with read_connection() as connection:
            rows = connection.execute(
                text(
                    "SELECT 1 FROM core.product_versions "
                    "WHERE canonical_sku = 'WM001' AND effective_from <= :t "
                    "  AND (effective_to > :t OR effective_to IS NULL)"
                ),
                {"t": first.effective_from - timedelta(seconds=1)},
            ).all()
        assert rows == []


class TestDatabaseEnforcedInvariants:
    """These hold even if the loader is wrong, which is the point of putting them in the schema."""

    def test_two_open_versions_are_impossible(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()

        with pytest.raises(IntegrityError, match="product_versions_current_uk"), transaction() as c:
            c.execute(
                text(
                    "INSERT INTO core.product_versions "
                    "(canonical_sku, version_number, product_name, category, supplier_key, "
                    " supplier_id, unit_price, currency, status, record_hash, effective_from, "
                    " effective_to, source_id, source_precedence, source_record_id, "
                    " raw_record_id, run_id) "
                    "SELECT canonical_sku, 99, product_name, category, supplier_key, supplier_id, "
                    "       unit_price, currency, status, record_hash, effective_from, NULL, "
                    "       source_id, source_precedence, source_record_id, raw_record_id, run_id "
                    "FROM core.product_versions WHERE canonical_sku = 'WM001'"
                )
            )

    def test_overlapping_validity_periods_are_impossible(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()
        write_erp_feed(rows_with(Price="899.00"))
        run()

        with pytest.raises(IntegrityError, match="no_overlap_ex"), transaction() as c:
            c.execute(
                text(
                    "INSERT INTO core.product_versions "
                    "(canonical_sku, version_number, product_name, category, supplier_key, "
                    " supplier_id, unit_price, currency, status, record_hash, effective_from, "
                    " effective_to, source_id, source_precedence, source_record_id, "
                    " raw_record_id, run_id) "
                    "SELECT canonical_sku, 99, product_name, category, supplier_key, supplier_id, "
                    "       unit_price, currency, status, record_hash, "
                    "       effective_from + interval '1 microsecond', "
                    "       effective_to - interval '1 microsecond', "
                    "       source_id, source_precedence, source_record_id, raw_record_id, run_id "
                    "FROM core.product_versions "
                    "WHERE canonical_sku = 'WM001' AND version_number = 1"
                )
            )

    def test_an_empty_validity_period_is_impossible(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run()

        with pytest.raises(IntegrityError, match="validity_ck"), transaction() as c:
            c.execute(
                text(
                    "UPDATE core.product_versions SET effective_to = effective_from "
                    "WHERE canonical_sku = 'WM001'"
                )
            )
