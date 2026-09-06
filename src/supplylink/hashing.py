"""Deterministic canonicalisation and hashing.

Every hash in SupplyLink — record hashes that drive SCD-2, payload hashes,
contract content hashes, reconciliation checksums — flows through the functions
here, so that "the same data" always means the same digest.

Three properties are non-negotiable:

**Stable across processes.** Python's builtin :func:`hash` is randomised per
interpreter by PYTHONHASHSEED and is useless for anything persisted. SHA-256 is
used throughout.

**Stable across representations.** ``799``, ``799.0`` and ``Decimal("799.00")``
are the same price and must produce the same digest, so numerics are quantised
to a fixed scale before formatting. NULL is encoded as a sentinel that cannot
collide with the empty string — otherwise "no value" and "empty value" would
hash alike, which is precisely the distinction reconciliation exists to catch.

**Stable across languages.** The same canonical form is implemented in SQL in
``sql/target_checksum.sql``. Reconciliation compares a checksum computed in
Python over the source against one computed by Postgres over the target; two
independent implementations agreeing is real evidence, whereas one
implementation compared against itself proves nothing.

The field separator is U+001F (UNIT SEPARATOR) and the record separator is
U+001E (RECORD SEPARATOR). Both are control characters that cannot occur in the
CSV and JSON payloads this platform ingests, so no escaping is required.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

FIELD_SEPARATOR = "\x1f"
RECORD_SEPARATOR = "\x1e"

#: Encoding for SQL NULL. Chosen to match Postgres COPY's default NULL marker and
#: to be distinguishable from an empty string.
NULL_SENTINEL = r"\N"

#: Scale used for every monetary/decimal value before hashing. Matches
#: NUMERIC(14,4) in the warehouse so Python and Postgres format identically.
DECIMAL_SCALE = Decimal("0.0001")

#: Postgres rounds NUMERIC half away from zero. Python's Decimal default is
#: ROUND_HALF_EVEN, which disagrees on exact halves: 0.00005 quantises to 0.0000
#: in Python and to 0.0001 in Postgres. Since the source-side checksum is
#: computed here and the target-side one is computed by Postgres, that single
#: disagreement would report a phantom content mismatch. Matching the database's
#: rounding is not a detail — it is the reason the two checksums can be compared
#: at all. Prices are non-negative, so HALF_UP and half-away-from-zero coincide.
DECIMAL_ROUNDING = ROUND_HALF_UP


def canonical_value(value: Any) -> str:  # noqa: PLR0911 - a flat type dispatch reads better than nesting
    """Render a single value in its canonical string form.

    Canonicalisation is *type-faithful*: the integer ``799`` and the decimal
    ``799.0000`` are different canonical values, because in this warehouse they
    are different things (a quantity and a price). Uniformity comes from the
    mapping layer, which coerces every field to its contract-declared canonical
    type before anything is hashed — see
    :func:`supplylink.transformation.mapper.map_record`.

    What canonicalisation *does* erase is accidental representation:
    ``"799.00"``, ``799.0`` and ``Decimal("799")`` all arrive as a decimal field
    and all render as ``799.0000``.
    """
    if value is None:
        return NULL_SENTINEL
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Floats reach here only from JSON sources. Routing them through Decimal
        # avoids repr differences such as 799.0 vs 799.00000000000001.
        return _format_decimal(Decimal(str(value)))
    if isinstance(value, datetime):
        moment = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _format_decimal(value: Decimal) -> str:
    """Quantise to the warehouse scale and render without exponent notation."""
    try:
        return format(value.quantize(DECIMAL_SCALE, rounding=DECIMAL_ROUNDING), "f")
    except InvalidOperation:  # value too large to quantise; fall back to plain form
        return format(value.normalize(), "f")


def canonical_row(values: Sequence[Any]) -> str:
    """Join values in the given order into one canonical string.

    Order is significant and is the caller's responsibility — it comes from the
    contract's declared checksum column list, so it is stable across runs.
    """
    return FIELD_SEPARATOR.join(canonical_value(value) for value in values)


def hash_row(values: Sequence[Any]) -> str:
    """SHA-256 of a canonicalised row, hex encoded."""
    return sha256_text(canonical_row(values))


def hash_mapping(mapping: Mapping[str, Any], columns: Sequence[str]) -> str:
    """SHA-256 over selected keys of a mapping, in the given column order.

    Missing keys are treated as NULL rather than raising, so a record that simply
    does not carry an optional attribute hashes consistently with one that
    carries it as null.
    """
    return hash_row([mapping.get(column) for column in columns])


def checksum_set(row_digests: Iterable[str]) -> str:
    """Combine per-row digests into one order-independent set checksum.

    Digests are sorted before combining, so the checksum depends on the *content*
    of the set and not on the order rows happened to arrive in. Two systems that
    hold the same rows agree even if neither preserves insertion order.

    The identical computation in SQL is ``sql/target_checksum.sql``.

    At warehouse scale you would replace sorting with an associative aggregate
    (XOR of digests, or Postgres-side ``bit_xor``) to avoid materialising and
    sorting every digest; see ``docs/reconciliation.md``.
    """
    ordered = sorted(row_digests)
    if not ordered:
        # An empty set still needs a well-defined, non-empty checksum so that
        # "no rows" compares equal to "no rows" and unequal to anything else.
        return sha256_text("")
    return sha256_text("\n".join(ordered))


def sha256_text(text: str) -> str:
    """SHA-256 of a UTF-8 string, hex encoded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(payload: Any) -> str:
    """SHA-256 over a JSON-serialisable structure.

    Keys are sorted and separators are fixed so that two structurally identical
    payloads hash alike regardless of key order or the writer's whitespace.
    """
    serialised = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=canonical_value,
    )
    return sha256_text(serialised)


def batch_fingerprint(payload_hashes: Sequence[str]) -> str:
    """Fingerprint an entire batch from its per-record payload hashes.

    Unlike :func:`checksum_set` this preserves order, because for idempotency the
    question is "is this the same file?" — a file with the same rows in a
    different order is a different delivery and is allowed to be re-processed.
    """
    return sha256_text(RECORD_SEPARATOR.join(payload_hashes))
