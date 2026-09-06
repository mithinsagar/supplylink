"""CSV source adapter.

Handles the failure modes a real supplier drop actually exhibits: the file that
is not there, the file in the wrong encoding, the export whose columns changed,
and rows with the wrong number of fields.

Two of those are treated as *record* problems and the rest as *run* problems, and
the split is deliberate:

* A **ragged row** — too few or too many fields — is one bad record. It is
  yielded with a marker so validation can quarantine it with the line number
  attached.
* A **missing declared header** is a run problem. The file is not the file the
  contract describes; if it loaded, a whole canonical column would silently
  become null and every row would fail content reconciliation. Failing loudly
  here is far cheaper than explaining it afterwards.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from supplylink.config import get_settings
from supplylink.errors import IngestionError
from supplylink.ingestion.base import ExtractionResult, SourceAdapter, SourceRecord
from supplylink.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from supplylink.contracts.model import Contract, CsvSourceConfig

log = get_logger(__name__)

#: Key added to a row whose field count did not match the header. Validation sees
#: it as a normal record and quarantines it; nothing else has to know.
RAGGED_MARKER = "__ragged__"


class CsvSourceAdapter(SourceAdapter):
    """Reads one or more CSV files matched by the contract's glob."""

    def __init__(self, contract: Contract) -> None:
        super().__init__(contract)
        if contract.source.csv is None:  # pragma: no cover - contract model guarantees this
            raise IngestionError(f"contract {contract.source_id} has no csv configuration")
        self.config: CsvSourceConfig = contract.source.csv
        self._data_dir = Path(get_settings().data_dir)

    # -- discovery --------------------------------------------------------

    def discover_files(self) -> list[Path]:
        """Files matching the contract glob, in a stable order.

        Sorted by name so that a multi-file drop is processed identically on
        every run and on every platform — filesystem iteration order is not a
        guarantee anywhere, and a pipeline whose output depends on it is not
        reproducible.
        """
        pattern = self.config.path_glob
        root = Path(pattern)
        if root.is_absolute():
            base, glob = root.parent, root.name
        else:
            base, glob = self._data_dir, pattern

        try:
            matches = sorted(path for path in base.glob(glob) if path.is_file())
        except (OSError, ValueError) as exc:
            raise IngestionError(f"cannot search {base} for {glob!r}: {exc}") from exc

        if not matches:
            raise IngestionError(
                f"no files matched {glob!r} under {base} for source "
                f"{self.source_id!r}. Nothing to ingest."
            )
        return matches

    # -- extraction -------------------------------------------------------

    def extract(self) -> ExtractionResult:
        files = self.discover_files()
        records = list(self._iter_files(files))
        return ExtractionResult(
            records=records,
            origin=", ".join(str(path) for path in files),
            metrics={
                "files": len(files),
                "file_names": [path.name for path in files],
                "ragged_rows": sum(1 for record in records if RAGGED_MARKER in record.payload),
            },
        )

    def iter_records(self) -> Iterator[SourceRecord]:
        yield from self._iter_files(self.discover_files())

    def _iter_files(self, files: list[Path]) -> Iterator[SourceRecord]:
        """Stream every file as one continuous, globally-ordinalled batch."""
        ordinal = 0
        for path in files:
            for payload in self._read_file(path):
                yield SourceRecord(ordinal=ordinal, payload=payload)
                ordinal += 1

    def _read_file(self, path: Path) -> Iterator[dict[str, Any]]:
        self._raise_if_field_limit_too_low()
        try:
            handle = path.open("r", encoding=self.config.encoding, newline="")
        except OSError as exc:
            raise IngestionError(f"cannot open {path}: {exc}") from exc

        with handle:
            reader = csv.reader(
                handle, delimiter=self.config.delimiter, quotechar=self.config.quotechar
            )
            try:
                headers = self._read_headers(reader, path)
                yield from self._read_rows(reader, headers, path)
            except UnicodeDecodeError as exc:
                # A declared encoding that does not match reality corrupts every
                # string field. Guessing an alternative would put mojibake in the
                # warehouse; naming the file and the byte offset lets someone fix it.
                raise IngestionError(
                    f"{path} is not valid {self.config.encoding} at byte {exc.start}: "
                    f"{exc.reason}. Correct the file or change source.csv.encoding."
                ) from exc
            except csv.Error as exc:
                raise IngestionError(f"{path} is not parseable as CSV: {exc}") from exc

    def _read_headers(self, reader: Any, path: Path) -> list[str]:
        try:
            raw_headers = next(reader)
        except StopIteration:
            raise IngestionError(f"{path} is empty — it has no header row") from None

        headers = [header.strip().lstrip("﻿") for header in raw_headers]

        missing = [name for name in self.config.required_headers if name not in headers]
        if missing:
            raise IngestionError(
                f"{path} is missing required headers {missing}. Found: {headers}. "
                f"The export shape has changed; loading it would null out those columns."
            )

        duplicates = sorted({name for name in headers if headers.count(name) > 1})
        if duplicates:
            raise IngestionError(
                f"{path} has duplicate headers {duplicates}; which column a mapping "
                f"refers to would be ambiguous"
            )
        return headers

    def _read_rows(self, reader: Any, headers: list[str], path: Path) -> Iterator[dict[str, Any]]:
        width = len(headers)
        for row in reader:
            if self.config.skip_blank_lines and not any(cell.strip() for cell in row):
                continue

            if len(row) != width:
                # Yield it anyway, marked. Dropping it here would make the record
                # vanish with no trace, which is the failure mode quarantine exists
                # to prevent.
                yield {
                    RAGGED_MARKER: (
                        f"row has {len(row)} fields, header declares {width} "
                        f"(line {reader.line_num} of {path.name})"
                    ),
                    **dict(zip(headers, row, strict=False)),
                }
                continue

            yield dict(zip(headers, row, strict=True))

    def _raise_if_field_limit_too_low(self) -> None:
        """Raise csv's global field-size cap if the contract needs a bigger one.

        The default is 128 KiB and a wide free-text column can exceed it, which
        surfaces as an opaque ``_csv.Error``.
        """
        wanted = self.config.max_field_size_bytes
        if csv.field_size_limit() >= wanted:
            return
        try:
            csv.field_size_limit(wanted)
        except OverflowError:  # pragma: no cover - platform dependent
            csv.field_size_limit(sys.maxsize)
