"""Ingestion: source adapters and the common record representation."""

from supplylink.ingestion.base import ExtractionResult, SourceAdapter, SourceRecord
from supplylink.ingestion.factory import build_adapter, registered_types

__all__ = [
    "ExtractionResult",
    "SourceAdapter",
    "SourceRecord",
    "build_adapter",
    "registered_types",
]
