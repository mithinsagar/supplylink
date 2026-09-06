"""Quarantine: rejected records and the reasons they were rejected.

Invalid data is never dropped. It is written to two tables at two different
grains, and the split is the whole point:

* ``dq.quarantined_records`` — **one row per rejected record**, holding the
  payload whole. This is what you open when someone asks "what did ACME actually
  send for WM001?"
* ``dq.validation_failures`` — **one row per rule violation**. A record failing
  three rules produces three rows.

A single table cannot serve both. At record grain the failures collapse into a
JSON array, and "which rule fails most often?" becomes JSON archaeology instead
of ``GROUP BY rule_name``. At rule grain the payload would be duplicated once per
failure. Two grains, one foreign key, and both questions stay cheap. See ADR-008.

Warnings are recorded in ``validation_failures`` with a null ``quarantine_id``:
the signal is kept, the record still loads. A database CHECK constraint enforces
that pairing so the two can never disagree.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

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


def write_quarantine(
    connection: Connection,
    run_id: uuid.UUID,
    source_id: str,
    records: Sequence[MappedRecord],
) -> tuple[int, int]:
    """Persist rejections and warnings. Returns ``(records_quarantined, failures)``.

    Called inside the load transaction. If the canonical load rolls back, these
    roll back with it — quarantine from a run that never completed would
    misrepresent what the warehouse actually saw.
    """
    quarantined = [record for record in records if not record.is_valid]
    warned = [record for record in records if record.is_valid and record.warnings]

    if not quarantined and not warned:
        return 0, 0

    failure_rows: list[dict[str, Any]] = []
    max_payload = get_settings().max_quarantine_payload_bytes
    created_at = utcnow()

    if quarantined:
        quarantine_rows = []
        for record in quarantined:
            payload, truncated = _bound_payload(record.raw_payload, max_payload)
            quarantine_rows.append(
                {
                    "run_id": run_id,
                    "source_id": source_id,
                    "raw_record_id": record.raw_record_id,
                    "source_record_id": record.source_record_id,
                    "ordinal": record.ordinal,
                    "raw_payload": payload,
                    "payload_truncated": truncated,
                    "failure_count": len(record.errors),
                    "created_at": created_at,
                }
            )

        quarantine_ids = (
            connection.execute(
                schema.quarantined_records.insert().returning(
                    schema.quarantined_records.c.quarantine_id,
                    sort_by_parameter_order=True,
                ),
                quarantine_rows,
            )
            .scalars()
            .all()
        )

        for record, quarantine_id in zip(quarantined, quarantine_ids, strict=True):
            for failure in record.errors:
                failure_rows.append(
                    {
                        "quarantine_id": int(quarantine_id),
                        "run_id": run_id,
                        "source_id": source_id,
                        "raw_record_id": record.raw_record_id,
                        "source_record_id": record.source_record_id,
                        "created_at": created_at,
                        **failure.as_row(),
                    }
                )

    # Warnings from records that passed, plus warnings that happened to sit on a
    # record rejected for some other reason.
    for record in list(warned) + list(quarantined):
        for failure in record.warnings:
            failure_rows.append(
                {
                    "quarantine_id": None,
                    "run_id": run_id,
                    "source_id": source_id,
                    "raw_record_id": record.raw_record_id,
                    "source_record_id": record.source_record_id,
                    "created_at": created_at,
                    **failure.as_row(),
                }
            )

    if failure_rows:
        chunk_size = get_settings().batch_size
        for start in range(0, len(failure_rows), chunk_size):
            connection.execute(
                schema.validation_failures.insert(), failure_rows[start : start + chunk_size]
            )

    log.info(
        "quarantine written",
        extra={
            "run_id": str(run_id),
            "stage": "validation",
            "records_quarantined": len(quarantined),
            "failures": len(failure_rows),
        },
    )
    return len(quarantined), len(failure_rows)


def _bound_payload(payload: Any, limit: int) -> tuple[Any, bool]:
    """Keep the payload whole unless it is pathologically large.

    A malformed file can carry a multi-megabyte cell. Storing it once per
    rejected record would let one bad file dominate the quarantine table, so
    oversized payloads are replaced by a marker that says what was dropped and
    how big it was.
    """
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= limit:
        return payload, False
    return (
        {
            "__truncated__": (
                f"payload was {len(encoded)} bytes, above the {limit}-byte quarantine limit"
            ),
            "__keys__": sorted(payload)[:50] if isinstance(payload, dict) else None,
        },
        True,
    )
