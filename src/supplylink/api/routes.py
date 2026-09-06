"""The SupplyLink HTTP API.

Small and operational rather than broad. These endpoints answer the questions
someone actually asks when a feed misbehaves at 3am — what ran, what it
rejected, whether it reconciled, and what a SKU looked like at some past
instant — and nothing else. There is no CRUD surface over the warehouse, because
the warehouse is written by the pipeline and read by everyone else.

**Errors are sanitised.** A driver exception can carry the full SQL statement,
parameter values and connection details. The handler at the bottom of this
module logs that in full and returns the exception class plus a short message.
Leaking a stack trace to a caller is a security problem, not a debugging feature.

**Triggering a run is synchronous** — see the note on ``POST /runs``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from supplylink import __version__
from supplylink.api import models
from supplylink.canonical import describe_model
from supplylink.config import get_settings
from supplylink.contracts.loader import ContractRegistry
from supplylink.db import dispose_engine, healthcheck, load_sql, read_connection
from supplylink.errors import (
    ContractError,
    DuplicateBatchError,
    IngestionError,
    SupplyLinkError,
)
from supplylink.observability import configure_logging, get_logger
from supplylink.pipeline import overall_status, run_pipeline
from supplylink.warehouse import schema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy import Connection

log = get_logger(__name__)

DESCRIPTION = """
Operational API for the SupplyLink data platform.

**What this is for.** Inspecting pipeline runs, investigating rejected records,
checking reconciliation, and querying the canonical product model — including
its history. It is a read surface over the warehouse plus one write endpoint
that triggers a run.

**Reconciliation vocabulary.** Checks are either *volume* ("did rows go
missing?") or *content* ("did the values survive intact?"). Only content checks
can detect a transformation that silently corrupted data while leaving the row
count identical.

**Quality reports.** `GENERATED` means a language model wrote the narrative and
every number in it was verified against metrics the pipeline computed.
`FALLBACK` means a deterministic report was used — the reason is in
`error_message`. Reports are never allowed to fail a run.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Configure logging once, when the server starts."""
    configure_logging()
    log.info("api started", extra={"version": __version__})
    yield
    dispose_engine()


app = FastAPI(
    title="SupplyLink",
    lifespan=lifespan,
    version=__version__,
    description=DESCRIPTION,
    openapi_tags=[
        {"name": "system", "description": "Health and platform metadata."},
        {"name": "sources", "description": "Registered feeds and their contracts."},
        {"name": "runs", "description": "Pipeline runs, failures and reconciliation."},
        {"name": "products", "description": "The canonical model, current and historical."},
    ],
)


def get_connection() -> Iterator[Connection]:
    """A read-only connection per request."""
    with read_connection() as connection:
        yield connection


ConnectionDep = Annotated["Connection", Depends(get_connection)]
RunIdPath = Annotated[UUID, Path(description="Full run UUID.")]
SkuPath = Annotated[str, Path(description="Canonical SKU.", pattern=r"^[A-Za-z0-9]{3,32}$")]


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


@app.get("/health", response_model=models.HealthResponse, tags=["system"])
def health() -> models.HealthResponse:
    """Liveness and readiness.

    Reports degraded rather than failing outright when contracts cannot be
    loaded, so an orchestrator can distinguish "the process is up but
    misconfigured" from "the process is down".
    """
    settings = get_settings()
    db_ok, db_detail = healthcheck()

    try:
        registry = ContractRegistry.discover(settings.contracts_dir)
        contracts_ok, contracts_detail = True, f"{len(registry)} contracts loaded"
    except SupplyLinkError as exc:
        contracts_ok, contracts_detail = False, str(exc)

    return models.HealthResponse(
        status="ok" if db_ok and contracts_ok else "degraded",
        version=__version__,
        database=models.HealthComponent(ok=db_ok, detail=db_detail),
        contracts=models.HealthComponent(ok=contracts_ok, detail=contracts_detail),
        llm_configured=settings.llm_configured,
    )


@app.get("/model", response_model=list[models.CanonicalFieldResponse], tags=["system"])
def canonical_model() -> list[dict[str, Any]]:
    """The canonical product model every source is mapped into."""
    return describe_model()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@app.get("/sources", response_model=list[models.SourceResponse], tags=["sources"])
def list_sources(connection: ConnectionDep) -> list[models.SourceResponse]:
    """Registered sources, with their contract version and most recent run."""
    latest_run = (
        select(
            schema.pipeline_runs.c.source_id,
            schema.pipeline_runs.c.started_at,
            schema.pipeline_runs.c.status,
        )
        .distinct(schema.pipeline_runs.c.source_id)
        .order_by(
            schema.pipeline_runs.c.source_id,
            schema.pipeline_runs.c.started_at.desc(),
        )
        .subquery()
    )
    latest_contract = (
        select(
            schema.contract_versions.c.source_id,
            schema.contract_versions.c.version,
            schema.contract_versions.c.effective_from,
        )
        .distinct(schema.contract_versions.c.source_id)
        .order_by(
            schema.contract_versions.c.source_id,
            schema.contract_versions.c.registered_at.desc(),
        )
        .subquery()
    )

    rows = connection.execute(
        select(
            schema.sources,
            latest_contract.c.version.label("contract_version"),
            latest_contract.c.effective_from.label("contract_effective_from"),
            latest_run.c.started_at.label("last_run_at"),
            latest_run.c.status.label("last_run_status"),
        )
        .join(
            latest_contract,
            latest_contract.c.source_id == schema.sources.c.source_id,
            isouter=True,
        )
        .join(latest_run, latest_run.c.source_id == schema.sources.c.source_id, isouter=True)
        .order_by(schema.sources.c.precedence)
    ).all()

    return [models.SourceResponse.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@app.get("/runs", response_model=list[models.RunResponse], tags=["runs"])
def list_runs(
    connection: ConnectionDep,
    source_id: Annotated[str | None, Query(description="Filter by source.")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[models.RunResponse]:
    """Recent pipeline runs, newest first."""
    rows = connection.execute(
        load_sql("run_summary"), {"run_id": None, "source_id": source_id, "limit": limit}
    ).all()
    return [models.RunResponse.model_validate(row) for row in rows]


@app.get("/runs/{run_id}", response_model=models.RunDetailResponse, tags=["runs"])
def get_run(run_id: RunIdPath, connection: ConnectionDep) -> models.RunDetailResponse:
    """One run, with per-stage timings."""
    row = _one_run(connection, run_id)
    detail = models.RunDetailResponse.model_validate(row)
    detail.stage_timings = row.timings
    return detail


@app.get(
    "/runs/{run_id}/failures",
    response_model=list[models.FailureCategoryResponse],
    tags=["runs"],
)
def get_run_failures(
    run_id: RunIdPath, connection: ConnectionDep
) -> list[models.FailureCategoryResponse]:
    """Failure categories for a run, ranked by frequency."""
    _one_run(connection, run_id)
    rows = connection.execute(load_sql("run_failures"), {"run_id": run_id}).all()
    return [models.FailureCategoryResponse.model_validate(row) for row in rows]


@app.get(
    "/runs/{run_id}/quarantine",
    response_model=list[models.QuarantinedRecordResponse],
    tags=["runs"],
)
def get_run_quarantine(
    run_id: RunIdPath,
    connection: ConnectionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[models.QuarantinedRecordResponse]:
    """Rejected records, each with every rule it broke and its original payload."""
    _one_run(connection, run_id)
    rows = connection.execute(
        load_sql("quarantine_detail"), {"run_id": run_id, "limit": limit}
    ).all()
    return [models.QuarantinedRecordResponse.model_validate(row) for row in rows]


@app.get(
    "/runs/{run_id}/reconciliation",
    response_model=models.ReconciliationResponse,
    tags=["runs"],
)
def get_run_reconciliation(
    run_id: RunIdPath, connection: ConnectionDep
) -> models.ReconciliationResponse:
    """Every reconciliation check computed for a run."""
    run = _one_run(connection, run_id)
    rows = connection.execute(
        select(schema.reconciliation_results)
        .where(schema.reconciliation_results.c.run_id == run_id)
        .order_by(schema.reconciliation_results.c.reconciliation_id)
    ).all()
    return models.ReconciliationResponse(
        run_id=run_id,
        overall_status=run.reconciliation_status,
        checks=[models.ReconciliationCheckResponse.model_validate(row) for row in rows],
    )


@app.get("/runs/{run_id}/quality", response_model=models.QualityReportResponse, tags=["runs"])
def get_run_quality(run_id: RunIdPath, connection: ConnectionDep) -> models.QualityReportResponse:
    """The data-quality report for a run, with the metrics it was built from."""
    row = connection.execute(
        select(schema.quality_reports).where(schema.quality_reports.c.run_id == run_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no quality report for run {run_id}")
    return models.QualityReportResponse.model_validate(row)


@app.get("/quality/latest", response_model=models.QualityReportResponse, tags=["runs"])
def latest_quality(
    connection: ConnectionDep,
    source_id: Annotated[str | None, Query()] = None,
) -> models.QualityReportResponse:
    """The most recent quality report, optionally for one source."""
    query = select(schema.quality_reports)
    if source_id is not None:
        query = query.join(
            schema.pipeline_runs,
            schema.pipeline_runs.c.run_id == schema.quality_reports.c.run_id,
        ).where(schema.pipeline_runs.c.source_id == source_id)
    row = connection.execute(
        query.order_by(schema.quality_reports.c.generated_at.desc()).limit(1)
    ).one_or_none()

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no quality reports have been generated")
    return models.QualityReportResponse.model_validate(row)


@app.post(
    "/runs",
    response_model=models.RunTriggerResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["runs"],
)
def trigger_run(payload: models.RunRequest) -> models.RunTriggerResponse:
    """Run the pipeline for a source.

    **Synchronous by design at this scale** — a run over the demo dataset
    completes in well under a second, and returning the real outcome is more
    useful than returning a job id. At production volumes this endpoint would
    enqueue work and return `202 Accepted` with a run id to poll; the run
    lifecycle in `meta.pipeline_runs` already models that, so the change is to
    this handler alone and not to the data model.
    """
    try:
        result = run_pipeline(
            payload.source_id,
            trigger="api",
            force=payload.force,
            skip_quality_report=payload.skip_quality_report,
        )
    except ContractError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicateBatchError as exc:
        # Raised only by the concurrent-load race, where the idempotency index
        # rejects a second run's transition to SUCCEEDED. The ordinary duplicate
        # is handled below, because the pipeline treats it as a skip, not an error.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IngestionError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except SupplyLinkError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    if result.skipped_reason is not None:
        # The pipeline is right to treat an already-loaded payload as a skip
        # rather than a failure. Over HTTP, though, "you asked me to load
        # something that is already loaded" is precisely a 409, and answering
        # 201 would tell a caller a load happened when none did.
        raise HTTPException(status.HTTP_409_CONFLICT, result.skipped_reason)

    return models.RunTriggerResponse(
        run_id=result.run_id,
        source_id=result.source_id,
        status=overall_status(result),
        reconciliation_status=result.reconciliation_status,
        duration_seconds=round(result.duration_seconds, 3),
        quality_report_status=(result.quality_report or {}).get("status"),
        skipped_reason=result.skipped_reason,
        **result.counters.as_dict(),
    )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@app.get("/skus/{sku}", response_model=models.ProductResponse, tags=["products"])
def get_sku(sku: SkuPath, connection: ConnectionDep) -> models.ProductResponse:
    """Current canonical state of a SKU."""
    row = connection.execute(
        load_sql("point_in_time_product"),
        {"sku": sku.upper(), "as_of": datetime.now(UTC)},
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no current version for {sku.upper()}")
    return models.ProductResponse.model_validate(row)


@app.get(
    "/skus/{sku}/history", response_model=list[models.ProductVersionResponse], tags=["products"]
)
def get_sku_history(sku: SkuPath, connection: ConnectionDep) -> list[models.ProductVersionResponse]:
    """Full SCD Type 2 history, with the attributes that changed at each step."""
    rows = connection.execute(load_sql("product_history"), {"sku": sku.upper()}).all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no history for {sku.upper()}")
    return [models.ProductVersionResponse.model_validate(row) for row in rows]


@app.get("/skus/{sku}/as-of", response_model=models.ProductResponse, tags=["products"])
def get_sku_as_of(
    sku: SkuPath,
    connection: ConnectionDep,
    at: Annotated[datetime, Query(description="ISO 8601 instant, e.g. 2026-03-14T09:00:00Z")],
) -> models.ProductResponse:
    """Point-in-time lookup.

    Returns at most one version by construction: a GiST exclusion constraint
    forbids overlapping validity periods for a SKU.
    """
    moment = at if at.tzinfo else at.replace(tzinfo=UTC)
    row = connection.execute(
        load_sql("point_in_time_product"), {"sku": sku.upper(), "as_of": moment}
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{sku.upper()} had no version in effect at {moment.isoformat()}",
        )
    return models.ProductResponse.model_validate(row)


@app.get("/skus/{sku}/lineage", response_model=list[models.LineageStepResponse], tags=["products"])
def get_sku_lineage(sku: SkuPath, connection: ConnectionDep) -> list[models.LineageStepResponse]:
    """Trace every version back to the source record, run and contract that produced it."""
    rows = connection.execute(load_sql("lineage_trace"), {"sku": sku.upper()}).all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no lineage for {sku.upper()}")
    return [models.LineageStepResponse.model_validate(row) for row in rows]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@app.exception_handler(SQLAlchemyError)
def _database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Log the driver error in full; tell the caller almost nothing.

    A SQLAlchemy exception stringifies to the full statement, its bound
    parameters and often the connection URL. That belongs in the server log, not
    in an HTTP response.
    """
    log.error(
        "database error serving request",
        extra={"path": request.url.path, "error": str(exc)},
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": "DatabaseError", "detail": "the warehouse is unavailable"},
    )


@app.exception_handler(SupplyLinkError)
def _supplylink_error(request: Request, exc: SupplyLinkError) -> JSONResponse:
    log.warning(
        "request failed", extra={"path": request.url.path, "error_type": type(exc).__name__}
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


def _one_run(connection: Connection, run_id: UUID) -> Any:
    """Fetch one run's summary row, or 404."""
    row = connection.execute(
        load_sql("run_summary"), {"run_id": run_id, "source_id": None, "limit": 1}
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such run: {run_id}")
    return row
