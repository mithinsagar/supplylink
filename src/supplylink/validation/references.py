"""Referential-integrity targets a contract is permitted to reference.

The contract model already constrains ``references.table`` with a pattern, but a
pattern is a weak guarantee when the value ends up in SQL. This allow-list makes
the set of legal targets finite and explicit, so a contract cannot point a
foreign-key check at an arbitrary relation, and an operator can see at a glance
what referential integrity means in this platform.

Adding a target is a deliberate code change, which is the right amount of
friction for something that widens the SQL surface.
"""

from __future__ import annotations

from typing import Final

#: ``schema.table`` -> columns that may be referenced.
REFERENCEABLE_TARGETS: Final[dict[str, frozenset[str]]] = {
    "core.suppliers": frozenset({"supplier_id"}),
    "core.products": frozenset({"canonical_sku"}),
}


def is_referenceable(table: str, column: str) -> bool:
    return column in REFERENCEABLE_TARGETS.get(table, frozenset())


def describe_targets() -> str:
    """Human-readable target list, for contract error messages."""
    return ", ".join(
        f"{table}({', '.join(sorted(columns))})"
        for table, columns in sorted(REFERENCEABLE_TARGETS.items())
    )
