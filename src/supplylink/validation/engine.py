"""The validation engine.

Validation runs in three passes over an already-mapped batch, in this order:

1. **Field rules** — per record, per field, pure. ``required``, bounds, length,
   regex, allowed values.
2. **Business rules** — per record, cross-field, evaluated in the restricted
   expression sandbox.
3. **Set rules** — ``unique`` across the batch, and ``references`` against
   warehouse state.

Set rules are last because they need the whole batch, and they are the reason
this is an engine rather than a loop. Uniqueness needs every record's key before
it can name a duplicate, and referential integrity is resolved with **one query
per referenced column** — all distinct values checked in a single round trip
rather than a lookup per record. On a 15,000-row batch that is the difference
between one query and fifteen thousand.

Duplicate policy: the first occurrence of a key is kept and every later one is
quarantined. Some pipelines keep the last, some reject all. Keeping the first is
deterministic given the ordinal, which means re-running the same file produces
the same outcome — and reproducibility matters more here than which arbitrary
row wins.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from sqlalchemy import bindparam, text

from supplylink.canonical import BUSINESS_KEY
from supplylink.observability import get_logger
from supplylink.validation.references import is_referenceable
from supplylink.validation.results import ValidationFailure, render_actual
from supplylink.validation.rules import validate_field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection

    from supplylink.contracts.model import Contract
    from supplylink.validation.results import MappedRecord

log = get_logger(__name__)


def validate_batch(
    records: Sequence[MappedRecord],
    contract: Contract,
    connection: Connection | None = None,
) -> None:
    """Validate a mapped batch in place, appending failures to each record.

    ``connection`` may be ``None`` for pure in-memory validation (unit tests,
    ``supplylink validate --dry-run``); referential checks are then skipped and
    that fact is logged rather than passing silently.
    """
    _apply_field_rules(records, contract)
    _apply_business_rules(records, contract)
    _apply_uniqueness(records, contract)

    if connection is not None:
        _apply_referential_integrity(records, contract, connection)
    elif _has_reference_rules(contract):
        log.warning(
            "referential integrity skipped: no database connection supplied",
            extra={"source_id": contract.source_id, "stage": "validation"},
        )


def _apply_field_rules(records: Sequence[MappedRecord], contract: Contract) -> None:
    for record in records:
        for field_name, spec in contract.validation.items():
            for failure in validate_field(field_name, record.values.get(field_name), spec):
                record.add(failure)


def _apply_business_rules(records: Sequence[MappedRecord], contract: Contract) -> None:
    compiled = contract.compiled_rules()
    if not compiled:
        return
    for record in records:
        for rule, expression in compiled:
            if expression.evaluate(record.values):
                continue
            record.add(
                ValidationFailure(
                    field_name=None,
                    rule_name=rule.name,
                    rule_definition={"expr": rule.expr},
                    expected=rule.expr,
                    actual_value=_render_rule_context(record, expression.identifiers),
                    message=rule.message,
                    severity=rule.severity,
                )
            )


def _render_rule_context(record: MappedRecord, identifiers: frozenset[str]) -> str:
    """The values the rule actually saw.

    A cross-field failure is unreadable without them: "stocked items must be
    priced" tells you nothing, whereas ``quantity_available=40, unit_price=0``
    tells you everything.
    """
    return ", ".join(f"{name}={record.values.get(name)!r}" for name in sorted(identifiers))


def _apply_uniqueness(records: Sequence[MappedRecord], contract: Contract) -> None:
    unique_fields = [field_name for field_name, spec in contract.validation.items() if spec.unique]
    if not unique_fields:
        return

    for field_name in unique_fields:
        spec = contract.validation[field_name]
        first_seen: dict[str, int] = {}
        for record in records:
            value = record.values.get(field_name)
            if value is None:
                continue  # absent values are the `required` rule's business
            key = str(value)
            original_ordinal = first_seen.get(key)
            if original_ordinal is None:
                first_seen[key] = record.ordinal
                continue
            record.add(
                ValidationFailure(
                    field_name=field_name,
                    rule_name="unique",
                    rule_definition={"unique": True, "scope": "batch"},
                    expected=f"{field_name} unique within the batch",
                    actual_value=render_actual(value),
                    message=(
                        f"duplicate {field_name} {value!r}; first seen at record {original_ordinal}"
                    ),
                    severity=spec.severity,
                )
            )


def _has_reference_rules(contract: Contract) -> bool:
    return any(spec.references is not None for spec in contract.validation.values())


def _apply_referential_integrity(
    records: Sequence[MappedRecord],
    contract: Contract,
    connection: Connection,
) -> None:
    """Check foreign-key style references with one query per referenced column."""
    by_target: dict[tuple[str, str], list[str]] = defaultdict(list)
    for field_name, spec in contract.validation.items():
        if spec.references is not None:
            by_target[(spec.references.table, spec.references.column)].append(field_name)

    for (table, column), fields in by_target.items():
        candidates = {
            str(record.values[field_name])
            for record in records
            for field_name in fields
            if record.values.get(field_name) is not None
        }
        if not candidates:
            continue

        known = _fetch_existing(connection, table, column, sorted(candidates))
        missing = candidates - known
        log.debug(
            "referential integrity resolved",
            extra={
                "target": f"{table}.{column}",
                "candidates": len(candidates),
                "missing": len(missing),
                "stage": "validation",
            },
        )
        if not missing:
            continue

        for field_name in fields:
            spec = contract.validation[field_name]
            for record in records:
                value = record.values.get(field_name)
                if value is None or str(value) not in missing:
                    continue
                record.add(
                    ValidationFailure(
                        field_name=field_name,
                        rule_name="references",
                        rule_definition={"table": table, "column": column},
                        expected=f"an existing {table}.{column}",
                        actual_value=render_actual(value),
                        message=(f"{field_name} {value!r} does not exist in {table}.{column}"),
                        severity=spec.severity,
                    )
                )


def _fetch_existing(
    connection: Connection,
    table: str,
    column: str,
    values: list[str],
) -> set[str]:
    """Which of ``values`` exist in ``table.column``.

    ``table`` and ``column`` are interpolated because SQL identifiers cannot be
    bound as parameters. They are safe because they are constrained twice over:
    the contract model restricts them to a regex, and
    :func:`~supplylink.validation.references.is_referenceable` restricts them to
    a finite allow-list. The assertion below is the last line of defence and
    should be unreachable. The *values* — the untrusted part — are always bound.
    """
    if not is_referenceable(table, column):
        raise ValueError(f"refusing to query non-referenceable target {table}.{column}")

    statement = text(
        f"SELECT {column} AS value FROM {table} WHERE {column} = ANY(:values)"  # noqa: S608
    ).bindparams(bindparam("values", value=values, expanding=False))

    rows = connection.execute(statement).scalars().all()
    return {str(row) for row in rows}


def summarise_failures(records: Sequence[MappedRecord]) -> dict[str, Any]:
    """Aggregate counts for logging and for the quality report's input metrics."""
    by_rule: dict[str, int] = defaultdict(int)
    by_field: dict[str, int] = defaultdict(int)
    errors = warnings = quarantined = 0

    for record in records:
        if not record.is_valid:
            quarantined += 1
        for failure in record.failures:
            by_rule[failure.rule_name] += 1
            by_field[failure.field_name or "<record>"] += 1
            if failure.is_error:
                errors += 1
            else:
                warnings += 1

    return {
        "records": len(records),
        "quarantined": quarantined,
        "valid": len(records) - quarantined,
        "errors": errors,
        "warnings": warnings,
        "by_rule": dict(sorted(by_rule.items(), key=lambda item: -item[1])),
        "by_field": dict(sorted(by_field.items(), key=lambda item: -item[1])),
        "business_key": BUSINESS_KEY,
    }
