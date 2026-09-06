"""Content checksums, computed independently on both sides.

The source-side digest is computed in Python from the mapped records. The
target-side digest is computed **by Postgres** from what actually landed in the
warehouse. They agree only if the canonicalisation, the rounding, the NULL
encoding, the separator and the column order all match — which is exactly why
comparing them is evidence rather than ceremony.

Comparing a checksum against one computed by the same code over the same
in-memory objects proves only that the objects did not change. Two independent
implementations agreeing proves the data survived the round trip through
mapping, serialisation, the driver, the type system and the storage layer.

The SQL is generated rather than stored as a static file because the columns are
contract-driven. It is safe to generate because the column names come from a
closed set — :data:`~supplylink.canonical.CANONICAL_FIELD_NAMES`, checked at
contract load and checked again here — and every expression comes from the
literal table below. No caller-supplied text ever reaches the statement.

``docs/reconciliation.md`` shows a fully rendered example of the generated SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import bindparam, text

from supplylink.canonical import CANONICAL_FIELDS_BY_NAME, CanonicalType
from supplylink.errors import ReconciliationError
from supplylink.hashing import NULL_SENTINEL, checksum_set, hash_row

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection

    from supplylink.validation.results import MappedRecord

#: SQL rendering for each canonical type, mirroring
#: :func:`supplylink.hashing.canonical_value` exactly.
#:
#: * NULL becomes the same sentinel, so "absent" and "empty" stay distinguishable.
#: * Decimals are rendered at scale 4 with ``FM`` to suppress padding, matching
#:   Python's quantise-to-four-places.
#: * ``array_to_string`` drops NULL elements, so every element is coalesced first —
#:   without that, a null column would silently shift every later field left and
#:   change the digest of a row that is perfectly fine.
_SQL_CANONICALISERS: Final[dict[CanonicalType, str]] = {
    CanonicalType.STRING: "coalesce({column}::text, '{null}')",
    CanonicalType.INTEGER: "coalesce({column}::text, '{null}')",
    CanonicalType.DECIMAL: "coalesce(to_char({column}, 'FM9999999999990.0000'), '{null}')",
    CanonicalType.BOOLEAN: "coalesce(CASE WHEN {column} THEN 'true' ELSE 'false' END, '{null}')",
    CanonicalType.DATE: "coalesce(to_char({column}, 'YYYY-MM-DD'), '{null}')",
}

#: U+001F UNIT SEPARATOR / U+001E RECORD SEPARATOR, as chr() so the SQL text
#: stays free of literal control characters.
_FIELD_SEPARATOR_SQL = "chr(31)"
_ROW_SEPARATOR_SQL = "chr(30)"


def source_checksum(records: Sequence[MappedRecord], columns: Sequence[str]) -> str:
    """Order-independent checksum over the mapped source records."""
    return checksum_set(
        hash_row([record.values.get(column) for column in columns]) for record in records
    )


def render_target_checksum_sql(columns: Sequence[str]) -> str:
    """Build the Postgres statement that digests the same columns.

    Column names are validated against the canonical model, so only known
    identifiers are ever interpolated. Values — the SKU list — are bound.
    """
    unknown = [column for column in columns if column not in CANONICAL_FIELDS_BY_NAME]
    if unknown:
        raise ReconciliationError(f"cannot checksum unknown canonical columns: {unknown}")
    if not columns:
        raise ReconciliationError("checksum requires at least one column")

    expressions = ",\n            ".join(
        _SQL_CANONICALISERS[CANONICAL_FIELDS_BY_NAME[column].type].format(
            column=f"pv.{column}", null=NULL_SENTINEL
        )
        for column in columns
    )

    # S608: the only interpolated text is `expressions`, assembled above purely
    # from the literal _SQL_CANONICALISERS templates and column names that were
    # just checked against the closed canonical set. The SKU list is bound.
    return f"""
WITH scoped AS (
    SELECT pv.*
    FROM core.product_versions pv
    WHERE pv.is_current
      AND pv.canonical_sku = ANY(:skus)
),
digests AS (
    SELECT encode(
        sha256(convert_to(array_to_string(ARRAY[
            {expressions}
        ], {_FIELD_SEPARATOR_SQL}), 'UTF8')),
        'hex'
    ) AS row_digest
    FROM scoped pv
)
SELECT
    count(*) AS row_count,
    coalesce(
        encode(
            sha256(convert_to(string_agg(row_digest, chr(10) ORDER BY row_digest), 'UTF8')),
            'hex'
        ),
        encode(sha256(convert_to('', 'UTF8')), 'hex')
    ) AS checksum
FROM digests
""".strip()  # noqa: S608


def target_checksum(
    connection: Connection,
    skus: Sequence[str],
    columns: Sequence[str],
) -> tuple[int, str]:
    """``(row_count, checksum)`` for the current versions of ``skus``."""
    statement = text(render_target_checksum_sql(columns)).bindparams(
        bindparam("skus", value=list(skus), expanding=False)
    )
    row = connection.execute(statement).one()
    return int(row.row_count), str(row.checksum)


__all__ = [
    "render_target_checksum_sql",
    "source_checksum",
    "target_checksum",
]
