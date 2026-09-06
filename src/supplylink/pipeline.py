"""The pipeline orchestrator.

One run, start to finish. This module owns the stage sequence, the transaction
boundaries and the failure policy; the work itself lives in the ingestion,
validation, warehouse, reconciliation and quality modules.

Transaction boundaries are a design decision, not an accident of where
``commit()`` happened to land (see ``docs/pipeline-lifecycle.md``):

===================================  ==========================================
Stage                                Boundary
===================================  ==========================================
run creation                         own transaction, committed immediately —
                                     a crash must not erase its own evidence
ingestion → staging                  own transaction — staging is the replay
                                     source and must survive a later failure
validation + quarantine + load       **one** transaction — the canonical model
                                     either advances completely or not at all,
                                     and quarantine from a run that never
                                     completed would misrepresent it
reconciliation                       own transaction, after the load commits —
                                     reconciling uncommitted state proves
                                     nothing about what a reader will see
run completion                       own transaction
quality report                       own transaction, best effort — narration
                                     of committed work, and never able to fail
                                     the run
===================================  ==========================================

Failure policy: a bad *record* is quarantined and the run continues; a bad *run*
— unreadable source, malformed contract, database unavailable — stops and is
recorded as FAILED. Loading a partial batch and calling it a success is how
silent data corruption starts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from supplylink.config import get_settings
from supplylink.contracts.loader import ContractRegistry, register_contract
from supplylink.db import read_connection, transaction
from supplylink.errors import DuplicateBatchError, PipelineError, SupplyLinkError
from supplylink.hashing import batch_fingerprint
from supplylink.ingestion import build_adapter
from supplylink.observability import get_logger, log_context
from supplylink.quality.report import generate_quality_report
from supplylink.reconciliation import null_rates, reconcile, save_report
from supplylink.reconciliation.metrics import CheckStatus, ReconciliationReport
from supplylink.transformation.mapper import map_record
from supplylink.validation.engine import summarise_failures, validate_batch
from supplylink.warehouse import quarantine, runs, scd, schema, staging
from supplylink.warehouse.runs import RunCounters

if TYPE_CHECKING:
    import uuid
    from pathlib import Path

    from supplylink.contracts.model import Contract
    from supplylink.llm import LLMProvider
    from supplylink.validation.results import MappedRecord

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineResult:
    """Everything a caller needs to render or assert on a completed run."""

    run_id: uuid.UUID
    source_id: str
    contract_version: str
    status: str
    counters: RunCounters
    reconciliation: ReconciliationReport | None = None
    quality_report: dict[str, Any] | None = None
    duration_seconds: float = 0.0
    stage_metrics: dict[str, Any] = field(default_factory=dict)
    skipped_reason: str | None = None
    error: BaseException | None = None

    @property
    def reconciliation_status(self) -> str | None:
        return None if self.reconciliation is None else self.reconciliation.status.value

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"


def run_pipeline(
    source_id: str,
    *,
    contracts_dir: Path | None = None,
    trigger: runs.Trigger = "cli",
    force: bool = False,
    skip_quality_report: bool = False,
    llm_provider: LLMProvider | None = None,
) -> PipelineResult:
    """Execute one full pipeline run for a source.

    Raises :class:`~supplylink.errors.PipelineError` on run-level failure, after
    recording the failure against the run so it stays auditable.
    """
    started = time.monotonic()
    settings = get_settings()
    registry = ContractRegistry.discover(contracts_dir or settings.contracts_dir)
    contract = registry.get(source_id)

    with transaction() as connection:
        contract_version_id = register_contract(connection, contract)

    run_id = runs.start_run(
        source_id=contract.source_id,
        contract_version_id=contract_version_id,
        trigger=trigger,
        is_replay=force,
    )

    with log_context(run_id=str(run_id), source_id=contract.source_id):
        return _execute(
            run_id,
            contract,
            force=force,
            started=started,
            skip_quality_report=skip_quality_report,
            llm_provider=llm_provider,
        )


def _execute(
    run_id: uuid.UUID,
    contract: Contract,
    *,
    force: bool,
    started: float,
    skip_quality_report: bool,
    llm_provider: LLMProvider | None,
) -> PipelineResult:
    result = PipelineResult(
        run_id=run_id,
        source_id=contract.source_id,
        contract_version=contract.version,
        status="RUNNING",
        counters=RunCounters(),
    )

    try:
        records = _stage_ingestion(run_id, contract, force=force, result=result)
    except DuplicateBatchError as exc:
        runs.mark_skipped(run_id, str(exc))
        result.status = "SKIPPED"
        result.skipped_reason = str(exc)
        result.duration_seconds = time.monotonic() - started
        return result
    except SupplyLinkError as exc:
        return _fail(result, run_id, "ingestion", exc, started)

    try:
        outcome = _stage_load(run_id, contract, records, result)
    except SupplyLinkError as exc:
        return _fail(result, run_id, "canonical_load", exc, started)
    except Exception as exc:
        return _fail(result, run_id, "canonical_load", PipelineError(str(exc)), started)

    try:
        report = _stage_reconciliation(run_id, contract, records, outcome)
        result.reconciliation = report
    except Exception as exc:
        return _fail(result, run_id, "reconciliation", PipelineError(str(exc)), started)

    runs.finish_run(
        run_id,
        status="SUCCEEDED",
        counters=result.counters,
        reconciliation_status=report.status.value,
    )
    result.status = "SUCCEEDED"

    if not skip_quality_report:
        result.quality_report = _stage_quality(run_id, contract, report, llm_provider)
    else:
        runs.begin_stage(run_id, "quality_report")
        runs.finish_stage(
            run_id, "quality_report", status="SKIPPED", metrics={"reason": "disabled by caller"}
        )

    result.duration_seconds = time.monotonic() - started
    return result


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def _stage_ingestion(
    run_id: uuid.UUID,
    contract: Contract,
    *,
    force: bool,
    result: PipelineResult,
) -> list[MappedRecord]:
    """Read the source, map it, check idempotency, and land it in staging."""
    runs.begin_stage(run_id, "ingestion")
    with log_context(stage="ingestion"):
        extraction = build_adapter(contract).extract()

        records = [
            map_record(record.payload, contract, ordinal=record.ordinal)
            for record in extraction.records
        ]

        # The fingerprint is taken over payload hashes in source order, so it
        # identifies "this delivery" rather than "this set of rows".
        fingerprint = batch_fingerprint([record.payload_hash for record in records])
        runs.record_batch_fingerprint(run_id, fingerprint, force=force)

        with transaction() as connection:
            staging.write_raw_records(connection, run_id, contract.source_id, records)

        result.counters.records_received = len(records)
        metrics = {
            "records": len(records),
            "origin": extraction.origin,
            "fingerprint": fingerprint[:16],
            **extraction.metrics,
        }
        result.stage_metrics["ingestion"] = metrics
        runs.finish_stage(run_id, "ingestion", metrics=metrics)

    return records


def _stage_load(
    run_id: uuid.UUID,
    contract: Contract,
    records: list[MappedRecord],
    result: PipelineResult,
) -> scd.LoadOutcome:
    """Validate, quarantine and load — all inside one transaction."""
    runs.begin_stage(run_id, "validation")
    runs.begin_stage(run_id, "canonical_load")

    with transaction() as connection, log_context(stage="validation"):
        validate_batch(records, contract, connection)
        summary = summarise_failures(records)
        quarantine.write_quarantine(connection, run_id, contract.source_id, records)

        with log_context(stage="canonical_load"):
            outcome = scd.load_canonical(connection, run_id, contract, records)

    result.counters.records_valid = summary["valid"]
    result.counters.records_quarantined = summary["quarantined"]
    result.counters.rows_inserted = outcome.inserted
    result.counters.rows_updated = outcome.updated
    result.counters.rows_unchanged = outcome.unchanged
    result.counters.rows_superseded = outcome.superseded

    validation_metrics = {
        **summary,
        # Stored so the next run has a completeness baseline to compare against.
        "null_rates": null_rates(contract, records),
    }
    result.stage_metrics["validation"] = validation_metrics
    result.stage_metrics["canonical_load"] = outcome.as_dict()

    runs.finish_stage(run_id, "validation", metrics=validation_metrics)
    runs.finish_stage(run_id, "canonical_load", metrics=outcome.as_dict())
    return outcome


def _stage_reconciliation(
    run_id: uuid.UUID,
    contract: Contract,
    records: list[MappedRecord],
    outcome: scd.LoadOutcome,
) -> ReconciliationReport:
    """Verify the committed load against the source."""
    runs.begin_stage(run_id, "reconciliation")
    with log_context(stage="reconciliation"):
        previous = _previous_null_rates(run_id, contract.source_id)

        with transaction() as connection:
            report = reconcile(
                connection,
                run_id,
                contract,
                records,
                outcome=outcome,
                previous_null_rates=previous,
            )
            save_report(connection, run_id, report)

        runs.finish_stage(
            run_id,
            "reconciliation",
            metrics={
                "status": report.status.value,
                "checks": len(report.checks),
                "failing": [check.label for check in report.failures()],
            },
        )
    return report


def _stage_quality(
    run_id: uuid.UUID,
    contract: Contract,
    report: ReconciliationReport,
    provider: LLMProvider | None,
) -> dict[str, Any] | None:
    """Generate the narrative report. Never fails the run."""
    runs.begin_stage(run_id, "quality_report")
    with log_context(stage="quality_report"):
        try:
            with read_connection() as connection:
                previous_run = runs.previous_successful_run(connection, contract.source_id, run_id)
            with transaction() as connection:
                record = generate_quality_report(
                    connection,
                    run_id,
                    contract,
                    reconciliation=report,
                    previous_run=previous_run,
                    provider=provider,
                )
        except Exception as exc:
            # The run has already succeeded and committed. A reporting failure is
            # recorded and nothing more.
            log.warning("quality report failed", extra={"error": str(exc)})
            runs.finish_stage(run_id, "quality_report", status="FAILED", error_message=str(exc))
            return None

        runs.finish_stage(
            run_id,
            "quality_report",
            metrics={"status": record["status"], "provider": record["provider"]},
        )
        return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _previous_null_rates(run_id: uuid.UUID, source_id: str) -> dict[str, float]:
    """The previous successful run's per-column null rates, if any."""
    with read_connection() as connection:
        previous = runs.previous_successful_run(connection, source_id, run_id)
        if previous is None:
            return {}
        metrics = connection.execute(
            select(schema.run_stages.c.metrics).where(
                schema.run_stages.c.run_id == previous.run_id,
                schema.run_stages.c.stage == "validation",
            )
        ).scalar_one_or_none()

    if not isinstance(metrics, dict):
        return {}
    rates = metrics.get("null_rates")
    return rates if isinstance(rates, dict) else {}


def _fail(
    result: PipelineResult,
    run_id: uuid.UUID,
    stage: runs.Stage,
    error: BaseException,
    started: float,
) -> PipelineResult:
    """Record a run-level failure against both the stage and the run, then raise."""
    log.error(
        "pipeline failed",
        extra={"stage": stage, "error_type": type(error).__name__, "error": str(error)},
    )
    runs.finish_stage(run_id, stage, status="FAILED", error_message=str(error))
    runs.finish_run(run_id, status="FAILED", counters=result.counters, error=error)

    result.status = "FAILED"
    result.error = error
    result.duration_seconds = time.monotonic() - started
    raise error


def overall_status(result: PipelineResult) -> str:
    """A single word for the CLI and the API.

    A run that loaded cleanly but reconciled WARN is not simply "SUCCEEDED" —
    reporting it that way is how a degrading feed goes unnoticed for a month.
    """
    if result.status != "SUCCEEDED":
        return result.status
    if result.reconciliation is None:
        return "SUCCEEDED"
    if result.reconciliation.status is CheckStatus.FAIL:
        return "SUCCEEDED_WITH_FAILURES"
    if result.reconciliation.status is CheckStatus.WARN:
        return "SUCCEEDED_WITH_WARNINGS"
    return "SUCCEEDED"
