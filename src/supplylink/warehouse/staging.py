"""Writing the immutable landing zone.

Every source record is persisted exactly as received before anything interprets
it. That has three payoffs:

* **Lineage.** Each canonical version, quarantine row and validation failure
  carries a ``raw_record_id``, so any warehouse value can be traced back to the
  bytes it came from.
* **Replay.** A run can be re-processed under a corrected contract without
  re-reading the original file, which may be gone.
* **Honesty about transforms.** When a mapping is wrong, the evidence of what
  the source actually sent is still there.

Rows are written with a multi-row INSERT ... RETURNING so ids come back in
parameter order — one round trip per batch chunk instead of one per record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from supplylink.clock import utcnow
from supplylink.config import get_settings
from supplylink.observability import get_logger
from supplylink.warehouse import schema

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy import Connection

    from supplylink.validation.results import MappedRecord

log = get_logger(__name__)


def write_raw_records(
    connection: Connection,
    run_id: uuid.UUID,
    source_id: str,
    records: Sequence[MappedRecord],
) -> None:
    """Persist the batch to staging, setting ``raw_record_id`` on each record.

    Chunked so a very large batch does not build one enormous statement. The
    chunk size is configuration rather than a constant because the right value
    depends on row width and on the database, and it is the first thing to tune.
    """
    if not records:
        return

    chunk_size = get_settings().batch_size
    ingested_at = utcnow()
    statement = schema.raw_records.insert().returning(
        schema.raw_records.c.raw_record_id,
        sort_by_parameter_order=True,
    )

    for start in range(0, len(records), chunk_size):
        chunk = records[start : start + chunk_size]
        rows = [
            {
                "run_id": run_id,
                "source_id": source_id,
                "ordinal": record.ordinal,
                "source_record_id": record.source_record_id,
                "payload": record.raw_payload,
                "payload_hash": record.payload_hash,
                "ingested_at": ingested_at,
            }
            for record in chunk
        ]
        returned = connection.execute(statement, rows).scalars().all()
        for record, raw_record_id in zip(chunk, returned, strict=True):
            record.raw_record_id = int(raw_record_id)

    log.info(
        "staging written",
        extra={"run_id": str(run_id), "stage": "ingestion", "rows": len(records)},
    )
