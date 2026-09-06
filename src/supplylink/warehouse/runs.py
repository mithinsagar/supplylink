"""Pipeline run lifecycle and idempotency.

Every execution gets a UUID before it does any work, and that id is threaded
through staging rows, quarantine rows, dimension versions, reconciliation results
and the quality report. It is what makes lineage a query rather than an
archaeology project.

**Run rows are committed as they change, outside the pipeline's data
transaction.** That is deliberate. If the load transaction rolls back, the record
that a run happened, when it started and why it failed must survive — a crash
that erases its own evidence is the worst possible outcome for an audit trail.

**Idempotency is enforced by the database, not by a check.** Rather than "SELECT,
decide, INSERT" — which two concurrent runs of the same file can both pass — the
run is created first and the fingerprint recorded once the batch is read. The
partial unique index on ``(source_id, batch_fingerprint) WHERE status =
'SUCCEEDED' AND NOT is_replay`` then makes the *second* run's transition to
SUCCEEDED fail. The pre-check that follows is an optimisation that produces a
friendly message early; the index is the guarantee. See ADR-006.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from supplylink.clock import utcnow
from supplylink.db import transaction
from supplylink.errors import DuplicateBatchError, PipelineError
from supplylink.observability import get_logger
from supplylink.warehouse import schema

if TYPE_CHECKING:
    from sqlalchemy import Connection

log = get_logger(__name__)

RunStatus = Literal["RUNNING", "SUCCEEDED", "FAILED", "SKIPPED"]
Stage = Literal[
    "ingestion", "mapping", "validation", "canonical_load", "reconciliation", "quality_report"
]
Trigger = Literal["cli", "api", "test", "demo"]

STAGES: tuple[Stage, ...] = (
    "ingestion",
    "mapping",
    "validation",
    "canonical_load",
    "reconciliation",
    "quality_report",
)


@dataclass(slots=True)
class RunCounters:
    """The accounting for one run.

    Two identities must hold when a run succeeds, and the database enforces both:

    * ``records_received == records_valid + records_quarantined``
    * ``records_valid == inserted + updated + unchanged + superseded``

    They are constraints rather than assertions on purpose. A loader bug that
    loses records becomes a failed transaction instead of a quietly short
    warehouse.
    """

    records_received: int = 0
    records_valid: int = 0
    records_quarantined: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_superseded: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def new_run_id() -> uuid.UUID:
    return uuid.uuid4()


def start_run(
    *,
    source_id: str,
    contract_version_id: int,
    trigger: Trigger = "cli",
    is_replay: bool = False,
) -> uuid.UUID:
    """Create a RUNNING run and commit it immediately."""
    run_id = new_run_id()
    with transaction() as connection:
        connection.execute(
            schema.pipeline_runs.insert().values(
                run_id=run_id,
                source_id=source_id,
                contract_version_id=contract_version_id,
                trigger=trigger,
                status="RUNNING",
                is_replay=is_replay,
                started_at=utcnow(),
            )
        )
    log.info(
        "run started",
        extra={"run_id": str(run_id), "source_id": source_id, "trigger": trigger},
    )
    return run_id


def record_batch_fingerprint(
    run_id: uuid.UUID,
    fingerprint: str,
    *,
    force: bool,
) -> None:
    """Attach the batch fingerprint and check for a prior successful load.

    Raises :class:`~supplylink.errors.DuplicateBatchError` when this exact
    payload already loaded successfully and ``force`` was not given.
    """
    with transaction() as connection:
        connection.execute(
            update(schema.pipeline_runs)
            .where(schema.pipeline_runs.c.run_id == run_id)
            .values(batch_fingerprint=fingerprint)
        )

        if force:
            return

        previous = connection.execute(
            select(schema.pipeline_runs.c.run_id)
            .where(
                schema.pipeline_runs.c.source_id
                == select(schema.pipeline_runs.c.source_id)
                .where(schema.pipeline_runs.c.run_id == run_id)
                .scalar_subquery(),
                schema.pipeline_runs.c.batch_fingerprint == fingerprint,
                schema.pipeline_runs.c.status == "SUCCEEDED",
                schema.pipeline_runs.c.run_id != run_id,
            )
            .order_by(schema.pipeline_runs.c.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    if previous is not None:
        raise DuplicateBatchError(fingerprint, str(previous))


def begin_stage(run_id: uuid.UUID, stage: Stage) -> None:
    """Mark a stage as started. Safe to call again after a retry."""
    with transaction() as connection:
        statement = insert(schema.run_stages).values(
            run_id=run_id, stage=stage, status="RUNNING", started_at=utcnow(), metrics={}
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[schema.run_stages.c.run_id, schema.run_stages.c.stage],
                set_={"status": "RUNNING", "started_at": utcnow(), "finished_at": None},
            )
        )


def finish_stage(
    run_id: uuid.UUID,
    stage: Stage,
    *,
    status: RunStatus = "SUCCEEDED",
    metrics: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    """Close a stage with its metrics."""
    with transaction() as connection:
        connection.execute(
            update(schema.run_stages)
            .where(
                schema.run_stages.c.run_id == run_id,
                schema.run_stages.c.stage == stage,
            )
            .values(
                status=status,
                finished_at=utcnow(),
                metrics=_jsonable(metrics or {}),
                error_message=_clip(error_message),
            )
        )
    log.info(
        "stage complete",
        extra={"run_id": str(run_id), "stage": stage, "status": status, **(metrics or {})},
    )


def finish_run(
    run_id: uuid.UUID,
    *,
    status: RunStatus,
    counters: RunCounters | None = None,
    reconciliation_status: str | None = None,
    error: BaseException | None = None,
) -> None:
    """Close a run.

    A :class:`~sqlalchemy.exc.IntegrityError` on the transition to SUCCEEDED
    means the idempotency index caught a concurrent duplicate load. That is
    translated into a clear error rather than a constraint-violation traceback,
    and the run is left FAILED.
    """
    values: dict[str, Any] = {
        "status": status,
        "finished_at": utcnow(),
        "reconciliation_status": reconciliation_status,
        "error_type": type(error).__name__ if error else None,
        "error_message": _clip(str(error)) if error else None,
    }
    if counters is not None:
        values.update(counters.as_dict())

    try:
        with transaction() as connection:
            connection.execute(
                update(schema.pipeline_runs)
                .where(schema.pipeline_runs.c.run_id == run_id)
                .values(**values)
            )
    except IntegrityError as exc:
        if status == "SUCCEEDED" and "pipeline_runs_batch_idem_uk" in str(exc.orig):
            _force_failed(
                run_id,
                "another run loaded this exact payload concurrently; "
                "this run was rejected by the idempotency constraint",
            )
            raise DuplicateBatchError("concurrent", str(run_id)) from exc
        raise

    log.info(
        "run finished",
        extra={
            "run_id": str(run_id),
            "status": status,
            "reconciliation": reconciliation_status,
            **(counters.as_dict() if counters else {}),
        },
    )


def _force_failed(run_id: uuid.UUID, message: str) -> None:
    """Last-resort status write, used when the intended write was rejected."""
    with transaction() as connection:
        connection.execute(
            update(schema.pipeline_runs)
            .where(schema.pipeline_runs.c.run_id == run_id)
            .values(
                status="FAILED",
                finished_at=utcnow(),
                error_type="DuplicateBatchError",
                error_message=message,
            )
        )


def mark_skipped(run_id: uuid.UUID, reason: str) -> None:
    """Close a run that did no work because its payload was already loaded."""
    with transaction() as connection:
        connection.execute(
            update(schema.pipeline_runs)
            .where(schema.pipeline_runs.c.run_id == run_id)
            .values(status="SKIPPED", finished_at=utcnow(), error_message=_clip(reason))
        )
    log.info("run skipped", extra={"run_id": str(run_id), "reason": reason})


def get_run(connection: Connection, run_id: uuid.UUID) -> Any:
    row = connection.execute(
        select(schema.pipeline_runs).where(schema.pipeline_runs.c.run_id == run_id)
    ).one_or_none()
    if row is None:
        raise PipelineError(f"no such run: {run_id}")
    return row


def previous_successful_run(
    connection: Connection,
    source_id: str,
    before_run_id: uuid.UUID,
) -> Any | None:
    """The most recent successful run for a source, before this one.

    Used for run-over-run comparison. Reconciliation is far more informative
    against the previous load than against nothing: "829 quarantined" is a
    number, "829 quarantined, up from 141" is a signal.
    """
    started_at = (
        select(schema.pipeline_runs.c.started_at)
        .where(schema.pipeline_runs.c.run_id == before_run_id)
        .scalar_subquery()
    )
    return connection.execute(
        select(schema.pipeline_runs)
        .where(
            schema.pipeline_runs.c.source_id == source_id,
            schema.pipeline_runs.c.status == "SUCCEEDED",
            schema.pipeline_runs.c.started_at < started_at,
        )
        .order_by(schema.pipeline_runs.c.started_at.desc())
        .limit(1)
    ).one_or_none()


#: Error and stage messages are stored, so they must be bounded. A driver
#: traceback or a vendor HTML error page can be enormous.
MAX_MESSAGE_CHARS = 4_000


def _clip(message: str | None) -> str | None:
    if message is None:
        return None
    return message if len(message) <= MAX_MESSAGE_CHARS else f"{message[:MAX_MESSAGE_CHARS]}…"


def _jsonable(value: Any) -> Any:
    """Make metrics JSON-safe. Decimals and UUIDs routinely appear in them."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    return str(value)
