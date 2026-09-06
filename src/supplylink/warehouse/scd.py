"""Slowly Changing Dimension, Type 2.

Overwriting a product row answers "what is the price?" and destroys the ability
to answer "what was the price on 14 March?" — which is the question a supply
chain actually asks when reconciling an invoice or explaining a margin. Type 2
keeps both by closing the old version and opening a new one. See ADR-003.

**A change is a hash comparison.** ``record_hash`` is SHA-256 over the
contract's ``tracked_attributes``, canonicalised. Equal hash, no new version — so
re-delivering last week's file produces zero churn no matter how many times it
arrives. That, not the batch fingerprint, is the real idempotency guarantee: it
holds even for a file that only partially overlaps a previous one.

**One load timestamp per batch.** Every version closed by a run closes at the
same instant every version it opens begins, so validity periods abut exactly with
no gap and no overlap. A point-in-time query therefore returns exactly one row
for any instant after a SKU first appeared — and the database enforces this with
a GiST exclusion constraint rather than trusting this module to be correct.

**Measures are not versioned.** ``quantity_available`` moves on every extract.
Tracking it would open a new dimension version nightly for every SKU and bury
the price and naming changes that matter. It is written to
``core.inventory_snapshots`` as a fact instead.

**Precedence decides conflicts.** When two feeds describe the same SKU, the more
authoritative one wins; the less authoritative one is counted as *superseded*
rather than silently applied. Without this, ERP and supplier feeds would
overwrite each other on alternating runs and the version history would be noise.
See ADR-007.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from supplylink.canonical import BUSINESS_KEY
from supplylink.clock import utcnow
from supplylink.config import get_settings
from supplylink.errors import WarehouseError
from supplylink.hashing import hash_row
from supplylink.observability import get_logger
from supplylink.warehouse import schema

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy import Connection

    from supplylink.contracts.model import Contract
    from supplylink.validation.results import MappedRecord

log = get_logger(__name__)

#: Smallest gap that keeps a validity period non-empty when two loads land in the
#: same clock tick. Postgres timestamps have microsecond resolution.
_TICK = timedelta(microseconds=1)


@dataclass(slots=True)
class LoadOutcome:
    """What the canonical load did."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    superseded: int = 0
    load_timestamp: datetime | None = None

    applied_skus: set[str] = field(default_factory=set)
    """SKUs whose warehouse state this run asserts: inserted, updated, or
    confirmed unchanged. Content reconciliation is scoped to these."""

    superseded_skus: set[str] = field(default_factory=set)
    """SKUs a more authoritative source already mastered. Deliberately excluded
    from the content checksum — the warehouse value is *supposed* to differ from
    what this feed sent."""

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged + self.superseded

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "superseded": self.superseded,
        }


def compute_record_hash(record: MappedRecord, tracked: Sequence[str]) -> str:
    """SHA-256 over the tracked attributes, in the contract's declared order.

    Order is part of the digest, which is why ``scd.tracked_attributes`` must not
    be reordered without a contract version bump.
    """
    return hash_row([record.values.get(attribute) for attribute in tracked])


def load_canonical(
    connection: Connection,
    run_id: uuid.UUID,
    contract: Contract,
    records: Sequence[MappedRecord],
) -> LoadOutcome:
    """Apply a validated batch to the canonical dimension and the inventory fact.

    Runs inside the caller's transaction: the dimension either advances
    completely or not at all.
    """
    valid = [record for record in records if record.is_valid]
    if not valid:
        return LoadOutcome(load_timestamp=utcnow())

    tracked = contract.scd.tracked_attributes
    precedence = contract.source.precedence
    source_id = contract.source_id

    # Last write wins within a batch. Batch-scoped uniqueness has already
    # quarantined in-batch duplicates, so this only guards against a contract
    # that does not declare the key unique.
    by_sku: dict[str, MappedRecord] = {}
    for record in valid:
        by_sku[str(record.values[BUSINESS_KEY])] = record

    current = _fetch_current_versions(connection, list(by_sku))
    supplier_keys = _fetch_supplier_keys(
        connection,
        {str(record.values["supplier_id"]) for record in by_sku.values()},
    )
    load_timestamp = _resolve_load_timestamp(current.values())

    outcome = LoadOutcome(load_timestamp=load_timestamp)
    to_close: list[int] = []
    to_insert: list[dict[str, Any]] = []
    new_skus: list[str] = []

    for sku, record in by_sku.items():
        record_hash = compute_record_hash(record, tracked)
        existing = current.get(sku)

        if existing is None:
            new_skus.append(sku)
            to_insert.append(
                _version_row(
                    record,
                    contract,
                    run_id,
                    record_hash=record_hash,
                    version_number=1,
                    load_timestamp=load_timestamp,
                    supplier_keys=supplier_keys,
                )
            )
            outcome.inserted += 1
            outcome.applied_skus.add(sku)
            continue

        if existing.record_hash == record_hash:
            outcome.unchanged += 1
            outcome.applied_skus.add(sku)
            continue

        if precedence > existing.source_precedence:
            # A less authoritative feed disagrees with a more authoritative one.
            # Recorded as superseded rather than applied, so the disagreement is
            # visible in the run counters instead of causing version flip-flop.
            outcome.superseded += 1
            outcome.superseded_skus.add(sku)
            continue

        to_close.append(int(existing.product_version_id))
        to_insert.append(
            _version_row(
                record,
                contract,
                run_id,
                record_hash=record_hash,
                version_number=int(existing.version_number) + 1,
                load_timestamp=load_timestamp,
                supplier_keys=supplier_keys,
            )
        )
        outcome.updated += 1
        outcome.applied_skus.add(sku)

    _register_products(connection, run_id, new_skus, list(by_sku), load_timestamp)

    # Close before inserting: the partial unique index allows only one open
    # version per SKU, so the new row cannot land while the old one is still open.
    if to_close:
        connection.execute(
            update(schema.product_versions)
            .where(schema.product_versions.c.product_version_id.in_(to_close))
            .values(effective_to=load_timestamp)
        )

    if to_insert:
        _chunked_insert(connection, schema.product_versions, to_insert)

    _write_inventory(connection, run_id, source_id, by_sku, load_timestamp)

    log.info(
        "canonical load complete",
        extra={
            "run_id": str(run_id),
            "source_id": source_id,
            "stage": "canonical_load",
            **outcome.as_dict(),
        },
    )
    return outcome


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _fetch_current_versions(connection: Connection, skus: list[str]) -> dict[str, Any]:
    """The open version for each SKU in the batch, in one query.

    One statement rather than one per record: on a 15,000-row batch that is the
    difference between a single indexed scan and 15,000 round trips.
    """
    if not skus:
        return {}
    rows = connection.execute(
        select(
            schema.product_versions.c.product_version_id,
            schema.product_versions.c.canonical_sku,
            schema.product_versions.c.version_number,
            schema.product_versions.c.record_hash,
            schema.product_versions.c.effective_from,
            schema.product_versions.c.source_precedence,
            schema.product_versions.c.source_id,
        ).where(
            schema.product_versions.c.canonical_sku.in_(skus),
            schema.product_versions.c.is_current.is_(True),
        )
    ).all()
    return {row.canonical_sku: row for row in rows}


def _fetch_supplier_keys(connection: Connection, supplier_ids: set[str]) -> dict[str, int]:
    """Resolve natural supplier ids to surrogate keys, in one query.

    Referential integrity has already run, so every id here exists; a missing one
    would mean validation and loading disagree, which is worth failing loudly for
    rather than inserting a null.
    """
    if not supplier_ids:
        return {}
    rows = connection.execute(
        select(schema.suppliers.c.supplier_id, schema.suppliers.c.supplier_key).where(
            schema.suppliers.c.supplier_id.in_(sorted(supplier_ids))
        )
    ).all()
    resolved = {row.supplier_id: int(row.supplier_key) for row in rows}

    missing = sorted(supplier_ids - resolved.keys())
    if missing:
        raise WarehouseError(
            f"validation accepted supplier references that do not exist: {missing}. "
            f"This means the referential-integrity check and the loader disagree."
        )
    return resolved


def _resolve_load_timestamp(existing: Any) -> datetime:
    """One instant for the whole batch, guaranteed to be after every version it closes.

    Normally this is simply "now". The guard matters when two loads land inside
    the same clock tick — common under a frozen clock in tests, and not
    impossible in production. Without it a closed version would get
    ``effective_to == effective_from``, an empty validity period that the CHECK
    constraint rejects and that would make a point-in-time query return nothing.
    """
    now = utcnow()
    latest = max((row.effective_from for row in existing), default=None)
    if latest is not None and latest >= now:
        return latest + _TICK
    return now


def _version_row(
    record: MappedRecord,
    contract: Contract,
    run_id: uuid.UUID,
    *,
    record_hash: str,
    version_number: int,
    load_timestamp: datetime,
    supplier_keys: dict[str, int],
) -> dict[str, Any]:
    values = record.values
    supplier_id = str(values["supplier_id"])
    return {
        "canonical_sku": values[BUSINESS_KEY],
        "version_number": version_number,
        "product_name": values["product_name"],
        "category": values["category"],
        "supplier_id": supplier_id,
        "unit_price": values["unit_price"],
        "currency": values["currency"],
        "status": values.get("status") or "ACTIVE",
        "record_hash": record_hash,
        "effective_from": load_timestamp,
        "effective_to": None,
        "source_id": contract.source_id,
        "source_precedence": contract.source.precedence,
        "source_record_id": record.source_record_id or str(values[BUSINESS_KEY]),
        "raw_record_id": record.raw_record_id,
        "run_id": run_id,
        "created_at": load_timestamp,
        # Both halves of the key are carried. The composite foreign key
        # core.suppliers(supplier_key, supplier_id) guarantees they can never
        # disagree, so queries get a readable supplier code without the usual
        # denormalisation risk.
        "supplier_key": supplier_keys[supplier_id],
    }


def _register_products(
    connection: Connection,
    run_id: uuid.UUID,
    new_skus: list[str],
    all_skus: list[str],
    load_timestamp: datetime,
) -> None:
    """Keep the durable SKU registry current.

    ``core.products`` is the foreign-key target that a Type 2 table cannot be,
    because its business key repeats. Every SKU in the batch touches
    ``last_seen``; new ones are inserted.
    """
    if not all_skus:
        return
    statement = insert(schema.products).values(
        [
            {
                "canonical_sku": sku,
                "first_seen_at": load_timestamp,
                "last_seen_at": load_timestamp,
                "first_seen_run_id": run_id,
                "last_seen_run_id": run_id,
            }
            for sku in all_skus
        ]
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[schema.products.c.canonical_sku],
            set_={
                "last_seen_at": statement.excluded.last_seen_at,
                "last_seen_run_id": statement.excluded.last_seen_run_id,
            },
        )
    )
    if new_skus:
        log.debug(
            "new SKUs registered",
            extra={"run_id": str(run_id), "count": len(new_skus), "stage": "canonical_load"},
        )


def _write_inventory(
    connection: Connection,
    run_id: uuid.UUID,
    source_id: str,
    by_sku: dict[str, MappedRecord],
    observed_at: datetime,
) -> None:
    """One inventory observation per SKU per run."""
    rows = [
        {
            "canonical_sku": sku,
            "run_id": run_id,
            "source_id": source_id,
            "quantity_available": record.values["quantity_available"],
            "observed_at": observed_at,
            "raw_record_id": record.raw_record_id,
        }
        for sku, record in by_sku.items()
        if record.values.get("quantity_available") is not None
    ]
    if rows:
        _chunked_insert(connection, schema.inventory_snapshots, rows)


def _chunked_insert(connection: Connection, table: Any, rows: list[dict[str, Any]]) -> None:
    chunk_size = get_settings().batch_size
    for start in range(0, len(rows), chunk_size):
        connection.execute(table.insert(), rows[start : start + chunk_size])
