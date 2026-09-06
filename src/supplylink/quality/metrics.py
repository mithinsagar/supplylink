"""Building the metrics payload handed to the model.

This module is the boundary between "what the pipeline measured" and "what the
model is allowed to say". Three rules govern it:

**Every figure is computed here, never by the model.** Including derived ones —
rates, shares, run-over-run deltas. If the model had to divide two counts to
produce a percentage it would sometimes divide them wrong, and a wrong number in
a confident sentence is worse than no sentence. Pre-computing every figure also
makes the numeric guard sound: anything numeric in the output that is not in this
payload was invented.

**No source data leaves the process.** The payload carries counts, rates, rule
names and field names. It does not carry product records, prices, supplier
names, or quarantined payloads. A sample *message* is included — the rule's own
text, written by us — but never a sample *value*.

**The payload is stored with the report.** ``dq.quality_reports.input_metrics``
holds exactly what was sent, so any number in the narrative can be traced back to
the metric it came from months later.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, func, select

from supplylink.warehouse import schema

if TYPE_CHECKING:
    import uuid
    from decimal import Decimal

    from sqlalchemy import Connection

    from supplylink.contracts.model import Contract
    from supplylink.reconciliation.metrics import ReconciliationReport

#: How many failure categories to describe. Enough to characterise a bad feed,
#: few enough that the model summarises rather than transcribes.
TOP_FAILURE_LIMIT = 6


def build_metrics(
    connection: Connection,
    run_id: uuid.UUID,
    contract: Contract,
    *,
    reconciliation: ReconciliationReport,
    previous_run: Any | None = None,
) -> dict[str, Any]:
    """Assemble the complete, self-contained metrics payload."""
    run = connection.execute(
        select(schema.pipeline_runs).where(schema.pipeline_runs.c.run_id == run_id)
    ).one()

    received = int(run.records_received)
    quarantined = int(run.records_quarantined)

    payload: dict[str, Any] = {
        "run": {
            "run_id": str(run_id),
            "source_id": contract.source_id,
            "source_name": contract.source.display_name,
            "source_type": contract.source.type,
            "contract_version": contract.version,
            "started_at": run.started_at.isoformat(),
            "trigger": run.trigger,
        },
        "volumes": {
            "records_received": received,
            "records_valid": int(run.records_valid),
            "records_quarantined": quarantined,
            "quarantine_rate_pct": _pct(quarantined, received),
            "rows_inserted": int(run.rows_inserted),
            "rows_updated": int(run.rows_updated),
            "rows_unchanged": int(run.rows_unchanged),
            "rows_superseded": int(run.rows_superseded),
        },
        "top_failures": _top_failures(connection, run_id),
        "warnings": _warning_summary(connection, run_id),
        "reconciliation": _reconciliation_payload(reconciliation),
    }

    payload["comparison"] = _comparison(previous_run, received, quarantined)
    return payload


def _top_failures(connection: Connection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Failure categories for this run, ranked, with pre-computed shares."""
    total = int(
        connection.execute(
            select(func.count())
            .select_from(schema.validation_failures)
            .where(
                schema.validation_failures.c.run_id == run_id,
                schema.validation_failures.c.severity == "error",
            )
        ).scalar_one()
    )
    if total == 0:
        return []

    rows = connection.execute(
        select(
            schema.validation_failures.c.rule_name,
            schema.validation_failures.c.field_name,
            func.count().label("failure_count"),
            func.count(func.distinct(schema.validation_failures.c.raw_record_id)).label(
                "affected_records"
            ),
            func.min(schema.validation_failures.c.message).label("sample_message"),
        )
        .where(
            schema.validation_failures.c.run_id == run_id,
            schema.validation_failures.c.severity == "error",
        )
        .group_by(
            schema.validation_failures.c.rule_name,
            schema.validation_failures.c.field_name,
        )
        .order_by(desc("failure_count"))
        .limit(TOP_FAILURE_LIMIT)
    ).all()

    return [
        {
            "rank": rank,
            "rule": row.rule_name,
            "field": row.field_name,
            "failure_count": int(row.failure_count),
            "affected_records": int(row.affected_records),
            "share_of_failures_pct": _pct(int(row.failure_count), total),
            # The rule's own message text, written by us. Never a data value.
            "explanation": row.sample_message,
        }
        for rank, row in enumerate(rows, start=1)
    ]


def _warning_summary(connection: Connection, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Non-blocking findings: recorded, but the records still loaded."""
    rows = connection.execute(
        select(
            schema.validation_failures.c.rule_name,
            # Deliberately not labelled "count": SQLAlchemy's Row exposes a
            # tuple-style `.count`, so that label would shadow the column and
            # `row.count` would silently yield a method instead of the value.
            func.count().label("occurrences"),
        )
        .where(
            schema.validation_failures.c.run_id == run_id,
            schema.validation_failures.c.severity == "warning",
        )
        .group_by(schema.validation_failures.c.rule_name)
        .order_by(desc("occurrences"))
        .limit(TOP_FAILURE_LIMIT)
    ).all()
    return [{"rule": row.rule_name, "count": int(row.occurrences)} for row in rows]


def _reconciliation_payload(report: ReconciliationReport) -> dict[str, Any]:
    return {
        "overall_status": report.status.value,
        "checks_run": len(report.checks),
        "checks_passing": len(report.checks) - len(report.failures()),
        "checks": [
            {
                "name": check.label,
                "kind": check.kind.value,
                "status": check.status.value,
                "source_value": check.source_value,
                "target_value": check.target_value,
                "delta_pct": _decimal_to_float(check.delta_pct),
            }
            for check in report.checks
        ],
    }


def _comparison(previous: Any | None, received: int, quarantined: int) -> dict[str, Any]:
    """Run-over-run context.

    A number on its own is weak: "829 quarantined" could be routine or a crisis.
    "829, up from 141" is a signal. When there is no previous run this says so
    explicitly, so the model does not imply a comparison that never happened.
    """
    if previous is None:
        return {
            "available": False,
            "note": "no previous successful run for this source; nothing to compare against",
        }

    previous_received = int(previous.records_received)
    previous_quarantined = int(previous.records_quarantined)
    current_rate = _pct(quarantined, received)
    previous_rate = _pct(previous_quarantined, previous_received)

    return {
        "available": True,
        "previous_run_id": str(previous.run_id),
        "previous_started_at": previous.started_at.isoformat(),
        "previous_records_received": previous_received,
        "previous_records_quarantined": previous_quarantined,
        "previous_quarantine_rate_pct": previous_rate,
        "records_received_change": received - previous_received,
        "records_received_change_pct": _pct(received - previous_received, previous_received),
        "records_quarantined_change": quarantined - previous_quarantined,
        "quarantine_rate_change_pct": round(current_rate - previous_rate, 2),
    }


def _pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator * 100 / denominator, 2)


def _decimal_to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
