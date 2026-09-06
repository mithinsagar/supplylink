"""The pipeline's clock.

Time is an input to this system, not an ambient fact: SCD-2 validity windows,
run timings and inventory observation times are all stamped from it. Reading
``datetime.now()`` directly at a dozen call sites would make history untestable —
you cannot assert that a version closed at the right instant if every call site
invents its own.

Everything that needs the current time goes through :func:`utcnow`, and tests
pin it with :func:`frozen_clock`. All timestamps are timezone-aware UTC; naive
datetimes are never produced or accepted.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def _system_clock() -> datetime:
    return datetime.now(UTC)


_now_provider: Callable[[], datetime] = _system_clock


def utcnow() -> datetime:
    """The current instant, timezone-aware UTC."""
    return _now_provider()


@contextmanager
def frozen_clock(moment: datetime) -> Iterator[datetime]:
    """Pin :func:`utcnow` to a fixed instant for the duration of the block.

    Used by tests that need to assert on SCD-2 validity boundaries, where the
    exact timestamps are the thing under test.
    """
    if moment.tzinfo is None:
        raise ValueError("frozen_clock requires a timezone-aware datetime")

    def _frozen() -> datetime:
        return moment

    global _now_provider  # noqa: PLW0603 - a deliberate, test-only seam
    previous = _now_provider
    _now_provider = _frozen
    try:
        yield moment
    finally:
        _now_provider = previous
