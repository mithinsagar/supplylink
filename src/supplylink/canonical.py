"""The canonical product model.

This is the vocabulary every source is translated into, and the reason a new
supplier is a YAML file rather than a new pipeline. Sources describe products in
their own dialect — ``product_code``, ``itemId``, ``SKU`` — and each contract's
mapping section is the dictionary from that dialect into the field names below.

Nothing downstream of mapping knows or cares which system a record came from.
Validation, SCD-2, reconciliation and the API all speak canonical.

Adding a canonical field means: add it here, add a column in a migration, and
update the contracts that can supply it. Contracts that cannot simply omit it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class CanonicalType(StrEnum):
    """The declared type of a canonical field.

    Mapping coerces every value to its field's type before validation or
    hashing, which is what lets ``"799.00"`` from a CSV and ``799.0`` from JSON
    become the same warehouse value with the same digest.
    """

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"


class FieldRole(StrEnum):
    """Why a field exists, which decides how history is kept for it."""

    #: Part of the business identity of the product.
    KEY = "key"
    #: A descriptive attribute. Changes are worth versioning (SCD Type 2).
    ATTRIBUTE = "attribute"
    #: A measure observed at a point in time. Versioning it would create a new
    #: dimension row on every run and bury the changes that matter.
    MEASURE = "measure"


@dataclass(frozen=True, slots=True)
class CanonicalField:
    name: str
    type: CanonicalType
    role: FieldRole
    description: str
    required: bool = True


CANONICAL_FIELDS: Final[tuple[CanonicalField, ...]] = (
    CanonicalField(
        "canonical_sku",
        CanonicalType.STRING,
        FieldRole.KEY,
        "Normalised stock-keeping unit. ERP 'WM-001', supplier 'wm001' and vendor "
        "'wm_001' all normalise to 'WM001'.",
    ),
    CanonicalField(
        "product_name",
        CanonicalType.STRING,
        FieldRole.ATTRIBUTE,
        "Human-readable product name.",
    ),
    CanonicalField(
        "category",
        CanonicalType.STRING,
        FieldRole.ATTRIBUTE,
        "Normalised category. 'Electronics', 'electronics' and 'ELEC' all resolve "
        "to 'ELECTRONICS' via a contract lookup.",
    ),
    CanonicalField(
        "supplier_id",
        CanonicalType.STRING,
        FieldRole.ATTRIBUTE,
        "Canonical supplier identifier in SUP-NNN form. Referentially checked "
        "against core.suppliers.",
    ),
    CanonicalField(
        "unit_price",
        CanonicalType.DECIMAL,
        FieldRole.ATTRIBUTE,
        "Price per unit in the record's currency. An attribute rather than a "
        "measure: 'what did this cost on 14 March' is a question the business asks.",
    ),
    CanonicalField(
        "currency",
        CanonicalType.STRING,
        FieldRole.ATTRIBUTE,
        "ISO 4217 alphabetic currency code.",
    ),
    CanonicalField(
        "quantity_available",
        CanonicalType.INTEGER,
        FieldRole.MEASURE,
        "On-hand units at observation time. Stored as a fact, not versioned.",
    ),
    CanonicalField(
        "status",
        CanonicalType.STRING,
        FieldRole.ATTRIBUTE,
        "Lifecycle state: ACTIVE or DISCONTINUED.",
        required=False,
    ),
)

CANONICAL_FIELDS_BY_NAME: Final[dict[str, CanonicalField]] = {
    field.name: field for field in CANONICAL_FIELDS
}

CANONICAL_FIELD_NAMES: Final[frozenset[str]] = frozenset(CANONICAL_FIELDS_BY_NAME)

#: Fields eligible for SCD-2 tracking — identity plus descriptive attributes.
VERSIONABLE_FIELDS: Final[frozenset[str]] = frozenset(
    field.name for field in CANONICAL_FIELDS if field.role is not FieldRole.MEASURE
)

#: The business key of the canonical model.
BUSINESS_KEY: Final[str] = "canonical_sku"

#: Default SCD-2 tracked set when a contract does not narrow it.
DEFAULT_TRACKED_ATTRIBUTES: Final[tuple[str, ...]] = (
    "product_name",
    "category",
    "supplier_id",
    "unit_price",
    "currency",
    "status",
)


def field_type(name: str) -> CanonicalType:
    """The declared type of a canonical field."""
    return CANONICAL_FIELDS_BY_NAME[name].type


def describe_model() -> list[dict[str, str | bool]]:
    """The canonical model as plain data, for the API and documentation."""
    return [
        {
            "name": field.name,
            "type": field.type.value,
            "role": field.role.value,
            "required": field.required,
            "description": field.description,
        }
        for field in CANONICAL_FIELDS
    ]
