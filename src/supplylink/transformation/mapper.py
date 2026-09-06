"""Mapping: source dialect to canonical model.

This is where a record stops being "an ACME row" and becomes "a product". Every
field follows the same declared pipeline::

    source value -> transforms (in order) -> lookup -> default -> type coercion

Each step is optional and each is declared in the contract, so the order is
uniform across sources and a reader of one contract can read them all.

Two decisions worth calling out:

**Coercion is the type check.** There is no separate ``type`` rule that runs
later. Coercing to the canonical type either succeeds — and the value is now
guaranteed to be the right Python type for hashing and for the database — or it
fails and produces a ``type`` failure. One mechanism, so the two can never
disagree about what a valid decimal is.

**The default applies after the lookup, not before.** An unrecognised category
resolves to null and is then filled by the field's default if one exists. Without
a default it stays null and the ``required`` rule catches it. That ordering is
what stops an unknown code from entering the warehouse as itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from supplylink.canonical import CANONICAL_FIELDS_BY_NAME, CanonicalType
from supplylink.errors import MappingError
from supplylink.hashing import sha256_json
from supplylink.transformation.functions import (
    get_transform,
    to_boolean,
    to_date,
    to_decimal,
    to_integer,
)
from supplylink.validation.results import MappedRecord, ValidationFailure, render_actual

if TYPE_CHECKING:
    from collections.abc import Mapping

    from supplylink.contracts.model import Contract, FieldMapping

_COERCERS = {
    CanonicalType.STRING: lambda value: value if isinstance(value, str) else str(value),
    CanonicalType.INTEGER: to_integer,
    CanonicalType.DECIMAL: to_decimal,
    CanonicalType.BOOLEAN: to_boolean,
    CanonicalType.DATE: to_date,
}


def map_record(
    raw: Mapping[str, Any],
    contract: Contract,
    *,
    ordinal: int,
) -> MappedRecord:
    """Map one source record into canonical form.

    Never raises for bad data. A field that cannot be mapped is recorded as a
    failure and left null, and mapping continues — so one unparseable price does
    not hide an unknown supplier in the same row.
    """
    record = MappedRecord(
        ordinal=ordinal,
        raw_payload=dict(raw),
        payload_hash=sha256_json(raw),
        source_record_id=_extract_source_record_id(raw, contract),
    )

    for field_name, mapping in contract.mapping.items():
        value, failure = _map_field(raw, field_name, mapping, contract)
        record.values[field_name] = value
        if failure is not None:
            record.add(failure)

    return record


def _extract_source_record_id(raw: Mapping[str, Any], contract: Contract) -> str | None:
    """The source's own identifier for this record, if the contract names one.

    Kept as it appeared in the source, unnormalised: its job is to point back at
    the original row during an investigation, so it should match what someone
    sees when they open the file.
    """
    field_name = contract.source_record_id_field
    if field_name is None:
        return None
    value = raw.get(field_name)
    return None if value is None else str(value).strip() or None


def _map_field(
    raw: Mapping[str, Any],
    field_name: str,
    mapping: FieldMapping,
    contract: Contract,
) -> tuple[Any, ValidationFailure | None]:
    """Resolve one canonical field. Returns ``(value, failure_or_none)``."""
    original = mapping.constant if mapping.source is None else raw.get(mapping.source)
    value: Any = original

    try:
        for transform_name in mapping.transforms:
            value = get_transform(transform_name)(value)
    except MappingError as exc:
        return None, ValidationFailure(
            field_name=field_name,
            rule_name="transform",
            rule_definition={"transforms": list(mapping.transforms)},
            expected=f"a value the {mapping.transforms} chain can normalise",
            actual_value=render_actual(original),
            message=f"{field_name}: {exc.detail}",
        )

    if mapping.lookup is not None and value is not None:
        table = contract.lookups[mapping.lookup]
        key = str(value)
        if key in table:
            value = table[key]
        elif not mapping.lookup_passthrough:
            # Resolved to null rather than passed through, so the `required` rule
            # catches it. Letting an unrecognised code through would put a value
            # in the warehouse that no downstream consumer can interpret.
            value = None

    if value is None and mapping.default is not None:
        value = mapping.default

    if value is None:
        return None, None

    canonical_type = CANONICAL_FIELDS_BY_NAME[field_name].type
    try:
        return _COERCERS[canonical_type](value), None
    except MappingError as exc:
        return None, ValidationFailure(
            field_name=field_name,
            rule_name="type",
            rule_definition={"type": canonical_type.value},
            expected=f"a value coercible to {canonical_type.value}",
            actual_value=render_actual(original),
            message=f"{field_name} must be {canonical_type.value}: {exc.detail}",
        )


def canonical_values(record: MappedRecord, columns: list[str]) -> list[Any]:
    """The record's values for the given canonical columns, in order.

    Used for checksum and record-hash computation, where column order is part of
    the digest and comes from the contract.
    """
    return [record.values.get(column) for column in columns]
