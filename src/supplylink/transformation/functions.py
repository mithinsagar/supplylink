"""The transform registry.

Contracts name transforms as strings (``transforms: [trim, upper, sku_normalise]``)
and the mapper resolves them here. That indirection is what keeps supplier
onboarding declarative: a new feed reuses existing transforms, and a genuinely
new normalisation is one registered function plus a unit test, available to
every contract from then on.

Every transform obeys the same contract:

* it is **pure** — no I/O, no clock, no database, no global state;
* it is **null-safe** — ``None`` in, ``None`` out, so missing values are the
  ``required`` rule's business rather than every transform's;
* it **raises** :class:`~supplylink.errors.MappingError` on input it cannot
  honestly convert, which quarantines that one record.

Purity is what makes these testable in isolation and safe to reorder.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from supplylink.errors import MappingError

Transform = Callable[[Any], Any]

_REGISTRY: dict[str, Transform] = {}

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_SUPPLIER_DIGITS = re.compile(r"(\d+)")

#: Strings that different systems use to mean "no value". Normalised to None so
#: that a nullability rule sees them for what they are instead of letting the
#: literal text "NULL" reach the warehouse.
_NULL_TOKENS: Final[frozenset[str]] = frozenset(
    {"", "null", "none", "nil", "n/a", "na", "#n/a", "-", "--", "\\n", "nan"}
)


def register(name: str) -> Callable[[Transform], Transform]:
    """Register a transform under the name contracts refer to it by."""

    def decorator(function: Transform) -> Transform:
        if name in _REGISTRY:
            raise ValueError(f"transform {name!r} is already registered")
        _REGISTRY[name] = function
        return function

    return decorator


def get_transform(name: str) -> Transform:
    """Resolve a transform by name, or raise :class:`KeyError` if unknown."""
    return _REGISTRY[name]


def available_transforms() -> tuple[str, ...]:
    """Every registered transform name, sorted. Used to validate contracts and
    to power ``supplylink contracts transforms``."""
    return tuple(sorted(_REGISTRY))


def _null_safe(function: Callable[[Any], Any]) -> Transform:
    def wrapper(value: Any) -> Any:
        return None if value is None else function(value)

    return wrapper


# ---------------------------------------------------------------------------
# String hygiene
# ---------------------------------------------------------------------------


@register("trim")
@_null_safe
def trim(value: Any) -> str:
    """Strip leading and trailing whitespace."""
    return str(value).strip()


@register("collapse_whitespace")
@_null_safe
def collapse_whitespace(value: Any) -> str:
    """Collapse internal whitespace runs to single spaces.

    Fixed-width ERP exports routinely pad names with runs of spaces; without this
    ``"Wireless  Mouse"`` and ``"Wireless Mouse"`` would look like a genuine
    product rename and open a spurious SCD version.
    """
    return _WHITESPACE.sub(" ", str(value)).strip()


@register("upper")
@_null_safe
def upper(value: Any) -> str:
    return str(value).upper()


@register("lower")
@_null_safe
def lower(value: Any) -> str:
    return str(value).lower()


@register("title_case")
@_null_safe
def title_case(value: Any) -> str:
    """Title-case words while leaving existing intra-word capitals alone.

    ``str.title()`` would turn ``"USB-C Hub"`` into ``"Usb-C Hub"``; this keeps
    acronyms intact.
    """
    return " ".join(word[:1].upper() + word[1:] if word else word for word in str(value).split(" "))


@register("nfkc")
@_null_safe
def nfkc(value: Any) -> str:
    """Unicode NFKC normalisation.

    Different systems encode the same character differently — a full-width digit,
    a non-breaking space. Without normalisation these hash as distinct values and
    reconciliation reports a content mismatch that does not exist.
    """
    return unicodedata.normalize("NFKC", str(value))


@register("nullify_placeholders")
def nullify_placeholders(value: Any) -> Any:
    """Convert a source's "no value" placeholder into a real ``None``."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in _NULL_TOKENS:
        return None
    return value


# ---------------------------------------------------------------------------
# Domain normalisation
# ---------------------------------------------------------------------------


@register("sku_normalise")
@_null_safe
def sku_normalise(value: Any) -> str:
    """Reduce a SKU to its canonical form.

    ``WM-001``, ``wm001``, ``wm_001`` and ``  WM 001 `` all become ``WM001``.
    Punctuation is decoration that each system applies differently; the identity
    is the alphanumeric core.
    """
    normalised = _NON_ALNUM.sub("", str(value)).upper()
    if not normalised:
        raise MappingError("canonical_sku", "SKU contains no alphanumeric characters", value)
    return normalised


@register("supplier_id_normalise")
@_null_safe
def supplier_id_normalise(value: Any) -> str:
    """Reduce a supplier reference to canonical ``SUP-NNN`` form.

    ``SUP42``, ``sup-42``, ``SUP_042`` and ``42`` all become ``SUP-042``. The
    numeric part is zero-padded to three digits so ordering and equality behave.
    """
    text = str(value).strip().upper()
    match = _SUPPLIER_DIGITS.search(text)
    if not match:
        raise MappingError("supplier_id", "supplier reference contains no digits", value)
    return f"SUP-{int(match.group(1)):03d}"


@register("currency_normalise")
@_null_safe
def currency_normalise(value: Any) -> str:
    """Upper-case and strip a currency code. Validity is the contract's job."""
    return _NON_ALNUM.sub("", str(value)).upper()


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------


@register("to_decimal")
@_null_safe
def to_decimal(value: Any) -> Decimal:
    """Parse a decimal, tolerating thousands separators and a currency symbol.

    Deliberately strict about what it will not guess at: ``"abc"`` raises rather
    than becoming zero, because a silent zero price is far more damaging than a
    quarantined record.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise MappingError("unit_price", "boolean is not a decimal", value)
    if isinstance(value, int | float):
        return Decimal(str(value))
    # Python's \s is Unicode-aware, so this also strips the non-breaking spaces
    # that spreadsheet exports use as thousands separators.
    cleaned = re.sub(r"[,\s]", "", str(value)).lstrip("$€£₹")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise MappingError("value", f"cannot parse {value!r} as a decimal", value) from exc


@register("to_integer")
@_null_safe
def to_integer(value: Any) -> int:
    """Parse an integer.

    A fractional input is rejected rather than truncated: ``120.7`` units on hand
    means the source is wrong, and rounding it hides that.
    """
    if isinstance(value, bool):
        raise MappingError("value", "boolean is not an integer", value)
    if isinstance(value, int):
        return value
    decimal_value = to_decimal(value)
    if decimal_value != decimal_value.to_integral_value():
        raise MappingError("value", f"{value!r} is not a whole number", value)
    return int(decimal_value)


@register("to_boolean")
@_null_safe
def to_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "t", "yes", "y", "1"}:
        return True
    if text in {"false", "f", "no", "n", "0"}:
        return False
    raise MappingError("value", f"cannot parse {value!r} as a boolean", value)


@register("to_date")
@_null_safe
def to_date(value: Any) -> date:
    """Parse a date from the formats these feeds actually emit.

    Ambiguous day/month orderings are not guessed at — an unrecognised format
    quarantines the record instead of inventing a date.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise MappingError("value", f"cannot parse {value!r} as a date", value)


@register("abs")
@_null_safe
def absolute(value: Any) -> Any:
    """Absolute value.

    Available but not used by any shipped contract, and deliberately so: turning
    a negative stock figure into a positive one manufactures data. The negative
    is a real signal and belongs in quarantine. Kept because a feed that encodes
    sign in a separate column legitimately needs it.
    """
    return abs(value) if isinstance(value, int | float | Decimal) else value
