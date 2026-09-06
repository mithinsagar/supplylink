"""Determinism of the hashing layer.

Everything that makes SCD-2 and reconciliation trustworthy rests on these
properties, so they are asserted directly rather than inferred from higher-level
behaviour.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from supplylink.hashing import (
    NULL_SENTINEL,
    batch_fingerprint,
    canonical_row,
    canonical_value,
    checksum_set,
    hash_mapping,
    hash_row,
    sha256_json,
    sha256_text,
)


class TestCanonicalValue:
    def test_null_is_distinguishable_from_empty_string(self) -> None:
        """The distinction reconciliation exists to catch must survive hashing."""
        assert canonical_value(None) == NULL_SENTINEL
        assert canonical_value("") == ""
        assert canonical_value(None) != canonical_value("")

    @pytest.mark.parametrize(
        "value",
        [Decimal("799"), Decimal("799.0"), Decimal("799.00"), Decimal("799.0000"), 799.0],
    )
    def test_equivalent_decimals_render_identically(self, value: object) -> None:
        assert canonical_value(value) == "799.0000"

    def test_integers_are_not_decimals(self) -> None:
        """Type-faithful by design: a quantity of 799 is not a price of 799.0000."""
        assert canonical_value(799) == "799"
        assert canonical_value(Decimal("799")) == "799.0000"

    def test_booleans_are_not_integers(self) -> None:
        assert canonical_value(True) == "true"
        assert canonical_value(1) == "1"

    def test_rounding_matches_postgres_half_away_from_zero(self) -> None:
        """Python's default ROUND_HALF_EVEN would give 0.0000 and break the checksum."""
        assert canonical_value(Decimal("0.00005")) == "0.0001"
        assert canonical_value(Decimal("1.00005")) == "1.0001"

    def test_datetimes_normalise_to_utc(self) -> None:
        aware = datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
        naive = datetime(2026, 3, 14, 9, 30)  # deliberately naive
        assert canonical_value(aware) == canonical_value(naive) == "2026-03-14T09:30:00"


class TestRowHashing:
    def test_column_order_is_part_of_the_hash(self) -> None:
        assert hash_row(["A", "B"]) != hash_row(["B", "A"])

    def test_null_does_not_shift_later_fields(self) -> None:
        """A null must not collapse and make two different rows hash alike."""
        assert hash_row(["A", None, "B"]) != hash_row(["A", "B", None])

    def test_separator_prevents_field_boundary_collisions(self) -> None:
        assert hash_row(["AB", "C"]) != hash_row(["A", "BC"])
        assert "\x1f" in canonical_row(["A", "B"])

    def test_missing_key_hashes_as_null(self) -> None:
        columns = ["sku", "price"]
        assert hash_mapping({"sku": "X"}, columns) == hash_mapping(
            {"sku": "X", "price": None}, columns
        )


class TestChecksumSet:
    def test_is_order_independent(self) -> None:
        digests = [hash_row(["A"]), hash_row(["B"]), hash_row(["C"])]
        assert checksum_set(digests) == checksum_set(list(reversed(digests)))

    def test_detects_a_changed_row(self) -> None:
        before = [hash_row(["A", Decimal("1")]), hash_row(["B", Decimal("2")])]
        after = [hash_row(["A", Decimal("1")]), hash_row(["B", Decimal("2.01")])]
        assert checksum_set(before) != checksum_set(after)

    def test_empty_set_has_a_stable_checksum(self) -> None:
        assert checksum_set([]) == checksum_set([]) == sha256_text("")

    def test_empty_set_differs_from_any_populated_set(self) -> None:
        assert checksum_set([]) != checksum_set([hash_row([""])])


class TestBatchFingerprint:
    def test_is_order_sensitive(self) -> None:
        """A redelivery with rows reordered is a different delivery."""
        hashes = [sha256_text("a"), sha256_text("b")]
        assert batch_fingerprint(hashes) != batch_fingerprint(list(reversed(hashes)))

    def test_identical_batches_match(self) -> None:
        hashes = [sha256_text("a"), sha256_text("b")]
        assert batch_fingerprint(hashes) == batch_fingerprint(list(hashes))


class TestJsonHashing:
    def test_key_order_does_not_matter(self) -> None:
        assert sha256_json({"a": 1, "b": 2}) == sha256_json({"b": 2, "a": 1})

    def test_value_change_does_matter(self) -> None:
        assert sha256_json({"a": 1}) != sha256_json({"a": 2})

    def test_nesting_is_respected(self) -> None:
        assert sha256_json({"a": {"b": 1}}) != sha256_json({"a": {"b": 2}})


def test_hashes_are_stable_across_interpreter_processes() -> None:
    """The property Python's builtin hash() lacks.

    Run in a subprocess with a different PYTHONHASHSEED: a persisted digest is
    worthless if it changes between processes, and this is the regression that
    would silently make every stored record_hash and checksum meaningless.
    """
    script = (
        "from decimal import Decimal;"
        "from supplylink.hashing import hash_row;"
        "print(hash_row(['WM001', Decimal('799.00'), None, 120]))"
    )
    expected = hash_row(["WM001", Decimal("799.00"), None, 120])

    for seed in ("0", "12345"):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        assert result.stdout.strip() == expected
