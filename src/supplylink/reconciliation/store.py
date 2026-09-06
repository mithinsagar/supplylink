"""Persisting reconciliation results.

A reconciliation that is computed, printed and forgotten cannot answer the
question that matters most — "when did this start?". Results are written per
check per run so the history is queryable: a checksum that has been failing for
six runs is a different problem from one that failed once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from supplylink.clock import utcnow
from supplylink.warehouse import schema

if TYPE_CHECKING:
    import uuid

    from sqlalchemy import Connection

    from supplylink.reconciliation.metrics import ReconciliationReport


def save_report(
    connection: Connection,
    run_id: uuid.UUID,
    report: ReconciliationReport,
) -> int:
    """Write every check. Returns the number of rows written."""
    if not report.checks:
        return 0

    computed_at = utcnow()
    rows = [
        {
            "run_id": run_id,
            "check_name": check.check_name,
            "check_kind": check.kind.value,
            "dimension": check.dimension,
            "source_value": check.source_value,
            "target_value": check.target_value,
            "delta": check.delta,
            "delta_pct": check.delta_pct,
            "status": check.status.value,
            "threshold": check.threshold,
            "details": check.details,
            "computed_at": computed_at,
        }
        for check in report.checks
    ]
    connection.execute(schema.reconciliation_results.insert(), rows)
    return len(rows)
