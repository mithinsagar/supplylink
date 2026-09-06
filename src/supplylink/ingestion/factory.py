"""Adapter selection.

The pipeline asks for "an adapter for this contract" and gets one. Adding a
source type is a registry entry here plus one adapter class — no branching in
the pipeline, which stays entirely unaware of where data comes from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from supplylink.errors import IngestionError
from supplylink.ingestion.csv_adapter import CsvSourceAdapter
from supplylink.ingestion.rest_adapter import RestSourceAdapter

if TYPE_CHECKING:
    from supplylink.contracts.model import Contract
    from supplylink.ingestion.base import SourceAdapter

_ADAPTERS: dict[str, type[SourceAdapter]] = {
    "csv": CsvSourceAdapter,
    "rest": RestSourceAdapter,
}


def build_adapter(contract: Contract) -> SourceAdapter:
    """The adapter for a contract's declared source type."""
    try:
        adapter_class = _ADAPTERS[contract.source.type]
    except KeyError:
        known = ", ".join(sorted(_ADAPTERS))
        raise IngestionError(
            f"no adapter for source type {contract.source.type!r}. Known types: {known}"
        ) from None
    return adapter_class(contract)


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))
