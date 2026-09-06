"""Field-level validation rules.

Each rule is a small pure function over ``(field, value, spec)`` returning a
:class:`~supplylink.validation.results.ValidationFailure` or ``None``. Rules do
not know about records, batches, the database or the run — which is what makes
them trivially unit-testable and safe to apply in any order.

Set-scoped rules (``unique``, ``references``) cannot be expressed this way,
because they depend on other records or on warehouse state. They live in
:mod:`supplylink.validation.engine` and run as a second pass.

**Null handling follows SQL.** If a value is null, only ``required`` has an
opinion. A null price is not "less than the minimum" — it is absent, and saying
so twice produces two failures for one problem and makes the top-failure ranking
lie about what is actually wrong with the feed.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from supplylink.validation.results import ValidationFailure, render_actual

if TYPE_CHECKING:
    from supplylink.contracts.model import FieldValidation

# Compiled patterns are cached across records; a batch applies the same handful
# of expressions tens of thousands of times.
_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _pattern(expression: str) -> re.Pattern[str]:
    compiled = _PATTERN_CACHE.get(expression)
    if compiled is None:
        compiled = _PATTERN_CACHE[expression] = re.compile(expression)
    return compiled


def check_required(field: str, value: Any, spec: FieldValidation) -> ValidationFailure | None:
    if not spec.required or value is not None:
        return None
    return ValidationFailure(
        field_name=field,
        rule_name="required",
        rule_definition={"required": True},
        expected="a non-null value",
        actual_value=None,
        message=f"{field} is required but was not supplied",
        severity=spec.severity,
    )


def check_min(field: str, value: Any, spec: FieldValidation) -> ValidationFailure | None:
    if spec.min is None or value is None:
        return None
    numeric = _as_decimal(value)
    if numeric is None or numeric >= spec.min:
        return None
    return ValidationFailure(
        field_name=field,
        rule_name="min",
        rule_definition={"min": str(spec.min)},
        expected=f">= {spec.min}",
        actual_value=render_actual(value),
        message=f"{field} must be greater than or equal to {spec.min}",
        severity=spec.severity,
    )


def check_max(field: str, value: Any, spec: FieldValidation) -> ValidationFailure | None:
    if spec.max is None or value is None:
        return None
    numeric = _as_decimal(value)
    if numeric is None or numeric <= spec.max:
        return None
    return ValidationFailure(
        field_name=field,
        rule_name="max",
        rule_definition={"max": str(spec.max)},
        expected=f"<= {spec.max}",
        actual_value=render_actual(value),
        message=f"{field} must be less than or equal to {spec.max}",
        severity=spec.severity,
    )


def check_min_length(field: str, value: Any, spec: FieldValidation) -> ValidationFailure | None:
    if spec.min_length is None or value is None:
        return None
    text = str(value)
    if len(text) >= spec.min_length:
        return None
    return ValidationFailure(
        field_name=field,
        rule_name="min_length",
        rule_definition={"min_length": spec.min_length},
        expected=f"at least {spec.min_length} characters",
        actual_value=render_actual(value),
        message=f"{field} must be at least {spec.min_length} characters (got {len(text)})",
        severity=spec.severity,
    )


def check_max_length(field: str, value: Any, spec: FieldValidation) -> ValidationFailure | None:
    if spec.max_length is None or value is None:
        return None
    text = str(value)
    if len(text) <= spec.max_length:
        return None
    return ValidationFailure(
        field_name=field,
        rule_name="max_length",
        rule_definition={"max_length": spec.max_length},
        expected=f"at most {spec.max_length} characters",
        actual_value=render_actual(value),
        message=f"{field} must be at most {spec.max_length} characters (got {len(text)})",
        severity=spec.severity,
    )


def check_regex(field: str, value: Any, spec: FieldValidation) -> ValidationFailure | None:
    if spec.regex is None or value is None:
        return None
    if _pattern(spec.regex).fullmatch(str(value)):
        return None
    return ValidationFailure(
        field_name=field,
        rule_name="regex",
        rule_definition={"regex": spec.regex},
        expected=f"a full match for /{spec.regex}/",
        actual_value=render_actual(value),
        message=f"{field} does not match the required format /{spec.regex}/",
        severity=spec.severity,
    )


def check_allowed_values(field: str, value: Any, spec: FieldValidation) -> ValidationFailure | None:
    if spec.allowed_values is None or value is None:
        return None
    if str(value) in spec.allowed_values:
        return None
    allowed = ", ".join(spec.allowed_values)
    return ValidationFailure(
        field_name=field,
        rule_name="allowed_values",
        rule_definition={"allowed_values": list(spec.allowed_values)},
        expected=f"one of [{allowed}]",
        actual_value=render_actual(value),
        message=f"{field} must be one of [{allowed}]",
        severity=spec.severity,
    )


#: Applied in this order. ``required`` runs first because a null value makes
#: every other rule inapplicable.
FIELD_RULES = (
    check_required,
    check_min_length,
    check_max_length,
    check_regex,
    check_allowed_values,
    check_min,
    check_max,
)


def validate_field(field: str, value: Any, spec: FieldValidation) -> list[ValidationFailure]:
    """Apply every configured rule for one field.

    All applicable rules run — the first failure does not stop the rest — so a
    value that is both too long and badly formatted reports both problems in one
    pass instead of over two runs.
    """
    return [
        failure
        for failure in (rule(field, value, spec) for rule in FIELD_RULES)
        if failure is not None
    ]


def _as_decimal(value: Any) -> Decimal | None:
    """Coerce for numeric comparison, or ``None`` if the value is not numeric.

    A non-numeric value reaching a bound check means coercion already failed and
    already produced a ``type`` failure; reporting it again as a range violation
    would double-count one problem.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    return None
