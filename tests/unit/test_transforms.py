"""Transform behaviour, including the source-inconsistency cases the demo relies on."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from supplylink.errors import MappingError
from supplylink.transformation.functions import (
    available_transforms,
    collapse_whitespace,
    currency_normalise,
    get_transform,
    nullify_placeholders,
    register,
    sku_normalise,
    supplier_id_normalise,
    title_case,
    to_boolean,
    to_date,
    to_decimal,
    to_integer,
)


class TestSkuNormalisation:
    @pytest.mark.parametrize(
        "raw",
        # The last two use an en dash and a non-ASCII separator, which real
        # spreadsheet exports do produce.
        ["WM-001", "wm001", "wm_001", "  WM 001 ", "W.M-001", "wm\u2013001"],
    )
    def test_every_dialect_converges(self, raw: str) -> None:
        """The claim the whole canonical model rests on: one identity, many spellings."""
        assert sku_normalise(raw) == "WM001"

    def test_a_sku_with_no_alphanumerics_is_a_record_error(self) -> None:
        with pytest.raises(MappingError) as caught:
            sku_normalise("---")
        assert caught.value.field == "canonical_sku"


class TestSupplierNormalisation:
    @pytest.mark.parametrize("raw", ["SUP42", "sup-42", "SUP_042", "42", "supplier 42"])
    def test_zero_pads_to_three_digits(self, raw: str) -> None:
        assert supplier_id_normalise(raw) == "SUP-042"

    def test_rejects_a_reference_with_no_digits(self) -> None:
        with pytest.raises(MappingError):
            supplier_id_normalise("UNKNOWN")


class TestNumericCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("799", Decimal("799")),
            ("799.00", Decimal("799.00")),
            ("1\u00a0499.50", Decimal("1499.50")),  # non-breaking space thousands separator
            ("₹2499", Decimal("2499")),
            (799, Decimal("799")),
        ],
    )
    def test_parses_the_forms_feeds_actually_send(self, raw: object, expected: Decimal) -> None:
        assert to_decimal(raw) == expected

    def test_refuses_to_guess_at_nonsense(self) -> None:
        """A silent zero price is far more damaging than a quarantined record."""
        with pytest.raises(MappingError):
            to_decimal("n/a")

    def test_booleans_are_not_numbers(self) -> None:
        with pytest.raises(MappingError):
            to_decimal(True)
        with pytest.raises(MappingError):
            to_integer(False)

    def test_fractional_quantities_are_rejected_not_truncated(self) -> None:
        """120.7 units on hand means the source is wrong; rounding hides that."""
        with pytest.raises(MappingError):
            to_integer("120.7")
        assert to_integer("120.0") == 120


class TestStringHygiene:
    def test_collapse_whitespace_prevents_phantom_renames(self) -> None:
        assert collapse_whitespace("Wireless   Mouse") == "Wireless Mouse"

    def test_title_case_preserves_existing_capitals(self) -> None:
        """str.title() would produce 'Usb-C Hub' and open a spurious SCD version."""
        assert title_case("USB-C Hub") == "USB-C Hub"
        assert title_case("wireless mouse") == "Wireless Mouse"

    @pytest.mark.parametrize("token", ["", "  ", "NULL", "n/a", "N/A", "-", "nan", "None"])
    def test_placeholder_tokens_become_real_nulls(self, token: str) -> None:
        assert nullify_placeholders(token) is None

    def test_a_genuine_value_passes_through(self) -> None:
        assert nullify_placeholders("Electronics") == "Electronics"

    def test_currency_normalisation_strips_punctuation(self) -> None:
        assert currency_normalise(" inr ") == "INR"


class TestOtherCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-03-14", date(2026, 3, 14)),
            ("14/03/2026", date(2026, 3, 14)),
            ("20260314", date(2026, 3, 14)),
        ],
    )
    def test_date_formats(self, raw: str, expected: date) -> None:
        assert to_date(raw) == expected

    def test_unknown_date_format_is_rejected_not_guessed(self) -> None:
        with pytest.raises(MappingError):
            to_date("March 14th")

    @pytest.mark.parametrize(("raw", "expected"), [("yes", True), ("N", False), ("1", True)])
    def test_boolean_forms(self, raw: str, expected: bool) -> None:
        assert to_boolean(raw) is expected


class TestRegistry:
    @pytest.mark.parametrize("name", available_transforms())
    def test_every_transform_is_null_safe(self, name: str) -> None:
        """None in, None out — nullability is the `required` rule's job, not every transform's."""
        assert get_transform(name)(None) is None

    def test_registering_a_duplicate_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="already registered"):

            @register("trim")
            def _duplicate(value: object) -> object:  # pragma: no cover
                return value

    def test_unknown_transform_raises(self) -> None:
        with pytest.raises(KeyError):
            get_transform("does_not_exist")
