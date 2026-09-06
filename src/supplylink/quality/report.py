"""Generating and persisting the quality report.

The report is the last stage of a run and the only one that may fail without
failing the run. Everything before it is data engineering; this is narration of
work already done and already committed.

The path is: build metrics → try to generate → verify the numbers → persist. Any
failure at any point produces the deterministic report instead. Three statuses
are recorded, and the distinction between them is the audit trail:

``GENERATED``
    A model wrote it and every number in it checked out.
``FALLBACK``
    The deterministic report was used. ``error_message`` says why — no key, the
    provider was down, or the numeric guard rejected the draft.
``FAILED``
    Even persisting the report went wrong. Recorded, still does not fail the run.

A rejected draft is kept in ``numeric_guard`` rather than discarded, so a
recurring hallucination is diagnosable rather than invisible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert

from supplylink.clock import utcnow
from supplylink.config import get_settings
from supplylink.llm import LLMUnavailableError, build_provider
from supplylink.observability import get_logger
from supplylink.quality import fallback
from supplylink.quality.metrics import build_metrics
from supplylink.quality.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from supplylink.quality.verifier import verify
from supplylink.warehouse import schema

if TYPE_CHECKING:
    import uuid

    from sqlalchemy import Connection

    from supplylink.contracts.model import Contract
    from supplylink.llm import LLMProvider
    from supplylink.reconciliation.metrics import ReconciliationReport

log = get_logger(__name__)

#: How much of a rejected draft to keep. Enough to see the invented figure in
#: context, bounded so a runaway generation cannot bloat the table.
_REJECTED_DRAFT_CHARS = 4_000


def generate_quality_report(
    connection: Connection,
    run_id: uuid.UUID,
    contract: Contract,
    *,
    reconciliation: ReconciliationReport,
    previous_run: Any | None = None,
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    """Produce, verify and store the run's quality report.

    Never raises. A report is always written, even if it is the deterministic one.
    """
    settings = get_settings()
    metrics = build_metrics(
        connection,
        run_id,
        contract,
        reconciliation=reconciliation,
        previous_run=previous_run,
    )

    owned_provider = provider is None
    active = provider or build_provider()

    try:
        record = _attempt_generation(active, metrics)
    finally:
        if owned_provider:
            active.close()

    record["input_metrics"] = metrics
    record["temperature"] = settings.llm_temperature
    record["prompt_version"] = PROMPT_VERSION

    _persist(connection, run_id, record)

    log.info(
        "quality report ready",
        extra={
            "run_id": str(run_id),
            "stage": "quality_report",
            "status": record["status"],
            "provider": record["provider"],
            "model": record["model"],
        },
    )
    return record


def _attempt_generation(provider: LLMProvider, metrics: dict[str, Any]) -> dict[str, Any]:
    """Try to generate; fall back on any failure, including a guard rejection."""
    try:
        response = provider.complete(SYSTEM_PROMPT, build_user_prompt(metrics))
    except LLMUnavailableError as exc:
        reason = str(exc)
        log.info("using deterministic quality report", extra={"reason": reason})
        return _fallback_record(metrics, provider, reason)
    except Exception as exc:  # an LLM must never take down a data pipeline
        reason = f"unexpected provider error: {type(exc).__name__}: {exc}"
        log.warning("LLM generation failed", extra={"reason": reason})
        return _fallback_record(metrics, provider, reason)

    guard = verify(response.text, metrics)
    if not guard.verified:
        reason = (
            f"numeric guard rejected the generated report: "
            f"{', '.join(guard.unverified)} do not correspond to any computed metric"
        )
        log.warning(
            "generated report failed the numeric guard",
            extra={
                "stage": "quality_report",
                "unverified": list(guard.unverified),
                "numbers_checked": guard.numbers_checked,
            },
        )
        record = _fallback_record(metrics, provider, reason)
        record["numeric_guard"] = {
            **guard.as_dict(),
            "rejected_draft": response.text[:_REJECTED_DRAFT_CHARS],
        }
        record["model"] = response.model
        return record

    return {
        "status": "GENERATED",
        "provider": response.provider,
        "model": response.model,
        "summary_markdown": response.text,
        "numeric_guard": guard.as_dict(),
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "latency_ms": response.latency_ms,
        "error_message": None,
    }


def _fallback_record(metrics: dict[str, Any], provider: LLMProvider, reason: str) -> dict[str, Any]:
    return {
        "status": "FALLBACK",
        "provider": provider.name,
        "model": provider.model,
        "summary_markdown": fallback.render(metrics, reason),
        "numeric_guard": {
            "verified": True,
            "numbers_checked": 0,
            "unverified": [],
            "policy": "deterministic report: every figure is copied from the metrics payload",
        },
        "prompt_tokens": None,
        "completion_tokens": None,
        "latency_ms": None,
        "error_message": reason,
    }


def _persist(connection: Connection, run_id: uuid.UUID, record: dict[str, Any]) -> None:
    """Write the report, replacing any earlier one for this run."""
    statement = insert(schema.quality_reports).values(
        run_id=run_id,
        status=record["status"],
        provider=record["provider"],
        model=record["model"],
        prompt_version=record["prompt_version"],
        temperature=record["temperature"],
        input_metrics=record["input_metrics"],
        summary_markdown=record["summary_markdown"],
        numeric_guard=record["numeric_guard"],
        prompt_tokens=record["prompt_tokens"],
        completion_tokens=record["completion_tokens"],
        latency_ms=record["latency_ms"],
        error_message=record["error_message"],
        generated_at=utcnow(),
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[schema.quality_reports.c.run_id],
            set_={
                "status": statement.excluded.status,
                "provider": statement.excluded.provider,
                "model": statement.excluded.model,
                "prompt_version": statement.excluded.prompt_version,
                "temperature": statement.excluded.temperature,
                "input_metrics": statement.excluded.input_metrics,
                "summary_markdown": statement.excluded.summary_markdown,
                "numeric_guard": statement.excluded.numeric_guard,
                "prompt_tokens": statement.excluded.prompt_tokens,
                "completion_tokens": statement.excluded.completion_tokens,
                "latency_ms": statement.excluded.latency_ms,
                "error_message": statement.excluded.error_message,
                "generated_at": statement.excluded.generated_at,
            },
        )
    )
