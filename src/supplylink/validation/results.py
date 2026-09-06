"""Validation outcomes.

A failure is a structured record, never a log line and never a print. It carries
enough to answer "why was this rejected?" months later without re-running
anything: the field, the rule, the rule's own definition, what was expected, what
actually arrived, and where in the batch it came from.

``rule_definition`` in particular is stored as data rather than prose. When a
contract tightens ``max`` from 10,000,000 to 1,000,000, historical failures still
show the threshold that was actually applied at the time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

Severity = Literal["error", "warning"]

#: Longest ``actual_value`` kept. A 2 MB cell in a malformed CSV should not be
#: copied into every failure row it triggers.
MAX_ACTUAL_VALUE_CHARS = 512


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """One rule violated by one record."""

    rule_name: str
    message: str
    expected: str
    field_name: str | None = None
    actual_value: str | None = None
    rule_definition: dict[str, Any] = field(default_factory=dict)
    severity: Severity = "error"

    @property
    def is_error(self) -> bool:
        """Errors quarantine the record; warnings are recorded and let it pass."""
        return self.severity == "error"

    def as_row(self) -> dict[str, Any]:
        """The failure as a database row fragment."""
        return {
            "field_name": self.field_name,
            "rule_name": self.rule_name,
            "rule_definition": self.rule_definition,
            "expected": self.expected,
            "actual_value": _truncate(self.actual_value),
            "message": self.message,
            "severity": self.severity,
        }


def _truncate(value: str | None) -> str | None:
    if value is None or len(value) <= MAX_ACTUAL_VALUE_CHARS:
        return value
    return f"{value[:MAX_ACTUAL_VALUE_CHARS]}… ({len(value)} chars)"


def render_actual(value: Any) -> str | None:
    """Render a value for the ``actual_value`` column.

    ``None`` and the empty string are rendered distinctly — a required-field
    failure reads very differently depending on which one it was.
    """
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return "''"
    return str(value)


@dataclass(slots=True)
class MappedRecord:
    """One source record after mapping, with any failures collected so far.

    Failures accumulate rather than short-circuiting. A record with a bad price
    *and* an unknown supplier should report both, because fixing one and
    rediscovering the other on the next run wastes a whole cycle.
    """

    ordinal: int
    raw_payload: Mapping[str, Any]
    payload_hash: str
    source_record_id: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    failures: list[ValidationFailure] = field(default_factory=list)
    raw_record_id: int | None = None

    @property
    def errors(self) -> list[ValidationFailure]:
        return [failure for failure in self.failures if failure.is_error]

    @property
    def warnings(self) -> list[ValidationFailure]:
        return [failure for failure in self.failures if not failure.is_error]

    @property
    def is_valid(self) -> bool:
        """Valid means no *errors*. Warnings do not block a record."""
        return not any(failure.is_error for failure in self.failures)

    def add(self, failure: ValidationFailure) -> None:
        self.failures.append(failure)
