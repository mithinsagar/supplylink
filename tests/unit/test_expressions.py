"""The business-rule expression sandbox.

The security tests matter as much as the semantic ones: contracts are files
plausibly edited by someone outside the core team, and a rule expression that
could execute arbitrary code would make every contract a remote-code-execution
vector.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from supplylink.errors import ContractError
from supplylink.validation.expressions import Expression


class TestEvaluation:
    def test_boolean_logic(self) -> None:
        rule = Expression("quantity_available == 0 or unit_price > 0")
        assert rule.evaluate({"quantity_available": 0, "unit_price": Decimal("0")})
        assert rule.evaluate({"quantity_available": 10, "unit_price": Decimal("799")})
        assert not rule.evaluate({"quantity_available": 10, "unit_price": Decimal("0")})

    def test_chained_comparison(self) -> None:
        assert Expression("0 < unit_price <= 100000").evaluate({"unit_price": Decimal("799")})
        assert not Expression("0 < unit_price <= 100").evaluate({"unit_price": Decimal("799")})

    def test_membership(self) -> None:
        rule = Expression("currency in ('INR', 'USD', 'EUR')")
        assert rule.evaluate({"currency": "INR"})
        assert not rule.evaluate({"currency": "RUB"})

    def test_mixed_decimal_and_int_comparison(self) -> None:
        """Prices are Decimal and quantities int; comparing them must not raise."""
        assert Expression("unit_price > quantity_available").evaluate(
            {"unit_price": Decimal("799.50"), "quantity_available": 120}
        )

    def test_decimal_and_float_literal_comparison(self) -> None:
        """A float literal in a rule must not raise against a Decimal field."""
        assert Expression("unit_price > 100.5").evaluate({"unit_price": Decimal("799.50")})

    def test_helper_functions(self) -> None:
        assert Expression("len(product_name) >= 3").evaluate({"product_name": "Mouse"})
        assert Expression("not_null(supplier_id)").evaluate({"supplier_id": "SUP-001"})
        assert Expression("is_null(supplier_id)").evaluate({"supplier_id": None})

    def test_conditional_expression(self) -> None:
        rule = Expression("unit_price > 0 if status == 'ACTIVE' else True")
        assert rule.evaluate({"status": "DISCONTINUED", "unit_price": Decimal("0")})
        assert not rule.evaluate({"status": "ACTIVE", "unit_price": Decimal("0")})

    def test_identifiers_are_reported(self) -> None:
        rule = Expression("quantity_available == 0 or unit_price > 0")
        assert rule.identifiers == frozenset({"quantity_available", "unit_price"})


class TestNullSemantics:
    def test_comparison_with_null_is_false_not_an_error(self) -> None:
        """SQL semantics. Nullability is the `required` rule's job, not every rule's."""
        assert Expression("unit_price > 0").evaluate({"unit_price": None}) is False

    def test_an_unmapped_field_reads_as_null(self) -> None:
        """Lets one rule serve feeds that differ in which optional fields they carry."""
        assert Expression("unit_price > 0").evaluate({}) is False

    def test_null_membership_is_still_evaluated(self) -> None:
        assert Expression("currency in ('INR',)").evaluate({"currency": None}) is False


class TestSandbox:
    @pytest.mark.parametrize(
        "hostile",
        [
            "__import__('os').system('id')",
            "open('/etc/passwd').read()",
            "unit_price.__class__.__mro__",
            "[x for x in range(10)]",
            "(lambda: 1)()",
            "globals()",
            "eval('1+1')",
            "unit_price['key']",
            "{'a': 1}",
            "(x := 5)",
        ],
    )
    def test_dangerous_constructs_are_rejected_at_load_time(self, hostile: str) -> None:
        """Rejected when the contract loads, not when a record hits the rule."""
        with pytest.raises(ContractError):
            Expression(hostile)

    def test_syntax_errors_name_the_expression(self) -> None:
        with pytest.raises(ContractError, match="not valid syntax"):
            Expression("unit_price >")

    def test_keyword_arguments_are_rejected(self) -> None:
        with pytest.raises(ContractError):
            Expression("round(unit_price, ndigits=2) > 0")

    def test_error_message_lists_available_functions(self) -> None:
        with pytest.raises(ContractError, match="Available:"):
            Expression("sqrt(unit_price) > 0")


def test_expression_is_reusable_across_records() -> None:
    """Parsed once at contract load, evaluated per record — so it must be stateless."""
    rule = Expression("unit_price > 0")
    for _ in range(3):
        assert rule.evaluate({"unit_price": Decimal("1")})
        assert not rule.evaluate({"unit_price": Decimal("0")})
