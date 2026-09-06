"""Reconciliation vocabulary and result types.

Deliberately dependency-free — it imports nothing from the rest of SupplyLink —
so that contracts can validate their threshold keys against it without dragging
the database layer into contract parsing.

The central distinction this module encodes is **volume** versus **content**
reconciliation:

* *Volume* asks "did we lose rows?" It is cheap and it is what most pipelines
  stop at. It cannot detect a transformation that mangled every price, because
  the row count is identical either way.
* *Content* asks "did the values survive intact?" It compares checksums computed
  independently over the source and the target, so a silent corruption shows up
  as a digest mismatch even when every count agrees.

A pipeline that only reconciles volume is not reconciled. See
``docs/reconciliation.md`` and ADR-004.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from decimal import Decimal


class CheckKind(StrEnum):
    VOLUME = "volume"
    CONTENT = "content"


class CheckStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a check verdict, not a credential
    WARN = "WARN"
    FAIL = "FAIL"

    @property
    def rank(self) -> int:
        return {"PASS": 0, "WARN": 1, "FAIL": 2}[self.value]


def worst(statuses: list[CheckStatus]) -> CheckStatus:
    """The most severe status in a set. An empty set reconciles clean."""
    return max(statuses, key=lambda status: status.rank, default=CheckStatus.PASS)


# ---------------------------------------------------------------------------
# Check names — also the keys a contract may set thresholds for
# ---------------------------------------------------------------------------

ROW_COUNT: Final = "row_count"
QUARANTINE_RATE: Final = "quarantine_rate_pct"
DUPLICATE_RATE: Final = "duplicate_rate_pct"
NULL_RATE: Final = "null_rate_pct"
DISTINCT_KEY_COUNT: Final = "distinct_key_count"
CONTENT_CHECKSUM: Final = "content_checksum"
ORPHAN_RATE: Final = "orphan_rate_pct"

#: Threshold keys a contract may configure. Anything else is a contract error,
#: so a typo in a threshold name cannot silently disable a check.
THRESHOLD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "row_count_delta_pct",
        QUARANTINE_RATE,
        DUPLICATE_RATE,
        "null_rate_delta_pct",
        ORPHAN_RATE,
    }
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One reconciliation check on one dimension."""

    check_name: str
    kind: CheckKind
    status: CheckStatus
    dimension: str = "*"
    source_value: str | None = None
    target_value: str | None = None
    delta: Decimal | None = None
    delta_pct: Decimal | None = None
    threshold: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """``check_name`` qualified by dimension, for display."""
        return self.check_name if self.dimension == "*" else f"{self.check_name}[{self.dimension}]"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Every check for one run, plus the overall verdict."""

    run_id: str
    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> CheckStatus:
        """The overall status: the worst individual check.

        Deliberately pessimistic. A run where nine checks pass and one fails is a
        failed reconciliation, and averaging that away is how bad data ships.
        """
        return worst([check.status for check in self.checks])

    def by_status(self, status: CheckStatus) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.status is status)

    def failures(self) -> tuple[CheckResult, ...]:
        """Checks that did not pass, worst first."""
        return tuple(
            sorted(
                (check for check in self.checks if check.status is not CheckStatus.PASS),
                key=lambda check: -check.status.rank,
            )
        )
