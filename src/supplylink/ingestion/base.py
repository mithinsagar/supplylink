"""The source adapter interface.

An adapter's entire job is to turn "wherever the data lives" into an ordered
stream of dictionaries. It performs **no** business logic: no renaming, no
normalisation, no validation, no filtering. Those belong to the mapping and
validation layers, which are shared by every source — pushing them into adapters
is how a platform ends up with per-supplier pipelines again.

Adapters yield :class:`SourceRecord` in a deterministic order and stream rather
than materialise, so a large CSV is bounded by one row of memory rather than the
whole file.

The distinction an adapter *is* responsible for is failure classification:

* the file or endpoint is unusable (missing headers, unreadable encoding, the
  API is down after retries) — that is a :class:`~supplylink.errors.IngestionError`
  and it fails the run;
* one record is unusable — that is not the adapter's call. It yields whatever it
  read and lets validation quarantine it with a proper explanation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from supplylink.contracts.model import Contract


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One record as the source presented it."""

    ordinal: int
    """Position in the batch: CSV line number, or index in the API response.

    Deterministic and stable, so a failure message can point at a real line and
    a replay processes records in the same order.
    """

    payload: dict[str, Any]
    """The record's fields, unmodified."""


@dataclass
class ExtractionResult:
    """What an adapter produced, plus what it had to do to get it."""

    records: list[SourceRecord]
    origin: str
    """Where the data came from — a file path or a request URL — for lineage."""

    metrics: dict[str, Any] = field(default_factory=dict)
    """Adapter-specific detail: files read, pages fetched, retries spent."""

    @property
    def count(self) -> int:
        return len(self.records)


class SourceAdapter(ABC):
    """Reads one source into a common internal representation."""

    def __init__(self, contract: Contract) -> None:
        self.contract = contract

    @property
    def source_id(self) -> str:
        return self.contract.source_id

    @abstractmethod
    def extract(self) -> ExtractionResult:
        """Read the source in full.

        Raises :class:`~supplylink.errors.IngestionError` when the source itself
        is unusable.
        """

    @abstractmethod
    def iter_records(self) -> Iterator[SourceRecord]:
        """Stream records without materialising them all.

        :meth:`extract` is the convenient form; this is the one that matters for
        a source too large to hold in memory.
        """
