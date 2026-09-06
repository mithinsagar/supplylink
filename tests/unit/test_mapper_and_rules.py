"""Mapping and field-level validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from supplylink.contracts.model import FieldValidation
from supplylink.transformation.mapper import map_record
from supplylink.validation.rules import validate_field

ERP_ROW = {
    "SKU": "WM-001",
    "Name": "Wireless  Mouse",
    "Category": "Electronics",
    "Price": "799.00",
    "Currency": "INR",
    "Stock": "120",
    "Supplier": "SUP-042",
    "Status": "ACTIVE",
}
ACME_ROW = {
    "product_code": "wm001",
    "product_name": "wireless mouse",
    "category": "electronics",
    "unit_price": "799",
    "currency": "INR",
    "qty": "120",
    "supplier_code": "SUP42",
}


class TestCanonicalConvergence:
    def test_two_dialects_produce_the_same_identity(self, erp_contract, acme_contract) -> None:
        """The central claim: 'WM-001' from the ERP and 'wm001' from ACME are one product."""
        erp = map_record(ERP_ROW, erp_contract, ordinal=0)
        acme = map_record(ACME_ROW, acme_contract, ordinal=0)

        assert erp.values["canonical_sku"] == acme.values["canonical_sku"] == "WM001"
        assert erp.values["category"] == acme.values["category"] == "ELECTRONICS"
        assert erp.values["supplier_id"] == acme.values["supplier_id"] == "SUP-042"
        assert erp.values["quantity_available"] == acme.values["quantity_available"] == 120

    def test_prices_converge_after_canonicalisation(self, erp_contract, acme_contract) -> None:
        """'799.00' and '799' are the same price once hashed."""
        from supplylink.hashing import canonical_value

        erp = map_record(ERP_ROW, erp_contract, ordinal=0)
        acme = map_record(ACME_ROW, acme_contract, ordinal=0)
        assert canonical_value(erp.values["unit_price"]) == canonical_value(
            acme.values["unit_price"]
        )

    def test_source_record_id_is_kept_unnormalised(self, erp_contract) -> None:
        """It exists to point back at the original file, so it must match what is there."""
        assert map_record(ERP_ROW, erp_contract, ordinal=0).source_record_id == "WM-001"


class TestMappingBehaviour:
    def test_coercion_produces_contract_declared_types(self, erp_contract) -> None:
        """The invariant the record hash depends on."""
        values = map_record(ERP_ROW, erp_contract, ordinal=0).values
        assert isinstance(values["unit_price"], Decimal)
        assert isinstance(values["quantity_available"], int)
        assert isinstance(values["canonical_sku"], str)

    def test_a_constant_mapping_needs_no_source_column(self, acme_contract) -> None:
        """ACME's feed carries no lifecycle column, so every row is asserted ACTIVE."""
        assert map_record(ACME_ROW, acme_contract, ordinal=0).values["status"] == "ACTIVE"

    def test_default_applies_when_the_source_value_is_absent(self, erp_contract) -> None:
        row = {**ERP_ROW, "Currency": ""}
        assert map_record(row, erp_contract, ordinal=0).values["currency"] == "INR"

    def test_an_unknown_lookup_value_becomes_null_not_itself(self, erp_contract) -> None:
        """An unrecognised code must not enter the warehouse as itself."""
        record = map_record({**ERP_ROW, "Category": "Widgets"}, erp_contract, ordinal=0)
        assert record.values["category"] is None
        assert any(f.rule_name == "required" for f in _validate(record, erp_contract))

    def test_whitespace_padding_does_not_change_the_name(self, erp_contract) -> None:
        padded = map_record({**ERP_ROW, "Name": "Wireless     Mouse"}, erp_contract, ordinal=0)
        assert padded.values["product_name"] == "Wireless Mouse"


class TestFailureCollection:
    def test_all_failures_are_collected_not_just_the_first(self, acme_contract) -> None:
        """Fixing one and rediscovering the next on the following run wastes a cycle."""
        broken = {**ACME_ROW, "product_code": "", "unit_price": "abc"}
        record = map_record(broken, acme_contract, ordinal=7)
        assert {f.rule_name for f in record.failures} == {"transform", "type"}

    def test_mapping_never_raises_for_bad_data(self, acme_contract) -> None:
        record = map_record({"product_code": "###"}, acme_contract, ordinal=0)
        assert not record.is_valid

    def test_a_failure_records_the_original_value(self, acme_contract) -> None:
        record = map_record({**ACME_ROW, "unit_price": "abc"}, acme_contract, ordinal=0)
        failure = next(f for f in record.failures if f.field_name == "unit_price")
        assert failure.actual_value == "abc"
        assert failure.rule_definition == {"type": "decimal"}

    def test_ordinal_is_preserved_for_error_reporting(self, acme_contract) -> None:
        assert map_record(ACME_ROW, acme_contract, ordinal=42).ordinal == 42


def _validate(record, contract):
    failures = []
    for field, spec in contract.validation.items():
        failures.extend(validate_field(field, record.values.get(field), spec))
    return failures


class TestFieldRules:
    def test_required(self) -> None:
        spec = FieldValidation(required=True)
        assert validate_field("f", None, spec)[0].rule_name == "required"
        assert validate_field("f", "x", spec) == []

    def test_null_only_triggers_required(self) -> None:
        """SQL semantics: a null price is absent, not 'below the minimum'."""
        spec = FieldValidation(required=True, min=Decimal(0), max_length=5, regex="^x$")
        rules = {f.rule_name for f in validate_field("f", None, spec)}
        assert rules == {"required"}

    def test_a_null_optional_field_produces_nothing(self) -> None:
        assert validate_field("f", None, FieldValidation(min=Decimal(0))) == []

    def test_min_and_max(self) -> None:
        spec = FieldValidation(min=Decimal(0), max=Decimal(100))
        assert validate_field("f", Decimal(-1), spec)[0].rule_name == "min"
        assert validate_field("f", Decimal(101), spec)[0].rule_name == "max"
        assert validate_field("f", Decimal(50), spec) == []

    def test_lengths(self) -> None:
        spec = FieldValidation(min_length=2, max_length=4)
        assert validate_field("f", "a", spec)[0].rule_name == "min_length"
        assert validate_field("f", "abcde", spec)[0].rule_name == "max_length"

    def test_regex_must_match_fully(self) -> None:
        spec = FieldValidation(regex="^[A-Z]{3}$")
        assert validate_field("f", "ABCD", spec)[0].rule_name == "regex"
        assert validate_field("f", "ABC", spec) == []

    def test_allowed_values(self) -> None:
        spec = FieldValidation(allowed_values=["INR", "USD"])
        failure = validate_field("f", "RUB", spec)[0]
        assert failure.rule_name == "allowed_values"
        assert "INR" in failure.expected

    def test_every_applicable_rule_runs(self) -> None:
        """A value both too long and badly formatted reports both in one pass."""
        spec = FieldValidation(max_length=2, regex="^[0-9]+$")
        assert {f.rule_name for f in validate_field("f", "abcd", spec)} == {
            "max_length",
            "regex",
        }

    def test_a_non_numeric_value_is_not_double_reported(self) -> None:
        """Coercion already produced a `type` failure; a range failure would double-count."""
        assert validate_field("f", "abc", FieldValidation(min=Decimal(0))) == []

    def test_severity_is_carried_through(self) -> None:
        spec = FieldValidation(required=True, severity="warning")
        failure = validate_field("f", None, spec)[0]
        assert failure.severity == "warning"
        assert not failure.is_error


def test_actual_value_is_truncated_when_pathological() -> None:
    """A multi-megabyte cell must not be copied into every failure row it triggers."""
    from supplylink.validation.results import MAX_ACTUAL_VALUE_CHARS

    failure = validate_field("f", "x" * 10_000, FieldValidation(max_length=5))[0]
    stored = failure.as_row()["actual_value"]
    assert len(stored) < MAX_ACTUAL_VALUE_CHARS + 40
    assert "10000 chars" in stored


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_falsy_strings_are_present_not_missing(value: str) -> None:
    """An empty string is a value; only None is absence."""
    assert validate_field("f", value, FieldValidation(required=True)) == []
