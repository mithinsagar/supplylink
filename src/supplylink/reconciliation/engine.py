"""The reconciliation engine.

Reconciliation runs **after the load transaction has committed**, against
committed warehouse state. Computing it inside the load transaction would
reconcile the pipeline against its own uncommitted intentions, which proves
nothing about what a reader will actually see.

The checks are split into two kinds, and the split is the point (see ADR-004):

*Volume* — did we lose rows? Cheap, and where most pipelines stop. It cannot
detect a mapping bug that mangled every price, because the row count is identical
either way.

*Content* — did the values survive? The source-side checksum is computed in
Python; the target-side one is computed by Postgres. Agreement between two
independent implementations is evidence. A pipeline that only reconciles volume
is not reconciled.

Null rates are compared **run over run**, not source against target. Every
canonical attribute is NOT NULL in the warehouse, so a target null rate is
always zero and comparing against it would be theatre. What is genuinely worth
alerting on is a feed whose completeness degrades: "category was 0.4% null last
week and is 31% null today" is a real incident that no row count will surface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from supplylink.canonical import BUSINESS_KEY
from supplylink.observability import get_logger
from supplylink.reconciliation.checksums import source_checksum, target_checksum
from supplylink.reconciliation.metrics import (
    CONTENT_CHECKSUM,
    DISTINCT_KEY_COUNT,
    DUPLICATE_RATE,
    NULL_RATE,
    ORPHAN_RATE,
    QUARANTINE_RATE,
    ROW_COUNT,
    CheckKind,
    CheckResult,
    CheckStatus,
    ReconciliationReport,
)
from supplylink.warehouse import schema

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy import Connection

    from supplylink.contracts.model import Contract, Threshold
    from supplylink.validation.results import MappedRecord
    from supplylink.warehouse.scd import LoadOutcome

log = get_logger(__name__)

_HUNDRED = Decimal(100)


def reconcile(
    connection: Connection,
    run_id: uuid.UUID,
    contract: Contract,
    records: Sequence[MappedRecord],
    *,
    outcome: LoadOutcome,
    previous_null_rates: dict[str, float] | None = None,
) -> ReconciliationReport:
    """Run every configured check and return the report."""
    thresholds = contract.reconciliation.thresholds
    valid = [record for record in records if record.is_valid]
    applied = sorted(outcome.applied_skus)

    checks: list[CheckResult] = [
        _check_row_count(connection, applied, thresholds.get("row_count_delta_pct")),
        _check_quarantine_rate(records, thresholds.get("quarantine_rate_pct")),
        _check_duplicate_rate(records, thresholds.get("duplicate_rate_pct")),
        _check_distinct_keys(records),
        _check_orphan_rate(records, thresholds.get("orphan_rate_pct")),
        _check_content_checksum(connection, contract, valid, applied, outcome),
    ]
    checks.extend(
        _check_null_rates(
            contract, valid, previous_null_rates or {}, thresholds.get("null_rate_delta_pct")
        )
    )

    report = ReconciliationReport(run_id=str(run_id), checks=tuple(checks))
    log.info(
        "reconciliation complete",
        extra={
            "run_id": str(run_id),
            "stage": "reconciliation",
            "status": report.status.value,
            "checks": len(checks),
            "failing": len(report.failures()),
        },
    )
    return report


# ---------------------------------------------------------------------------
# Volume checks
# ---------------------------------------------------------------------------


def _check_row_count(
    connection: Connection,
    applied_skus: list[str],
    threshold: Threshold | None,
) -> CheckResult:
    """Did every SKU this run applied end up with a current version?

    Compares what the loader claims it applied against what the warehouse
    actually holds. A discrepancy means rows were lost between the loader's
    accounting and the committed state — the exact failure this check exists for.
    """
    expected = len(applied_skus)
    actual = 0
    if applied_skus:
        actual = int(
            connection.execute(
                select(func.count())
                .select_from(schema.product_versions)
                .where(
                    schema.product_versions.c.canonical_sku.in_(applied_skus),
                    schema.product_versions.c.is_current.is_(True),
                )
            ).scalar_one()
        )

    delta = Decimal(actual - expected)
    delta_pct = _percentage(abs(delta), expected)

    return CheckResult(
        check_name=ROW_COUNT,
        kind=CheckKind.VOLUME,
        status=_grade(delta_pct, threshold, zero_is_pass=True),
        source_value=str(expected),
        target_value=str(actual),
        delta=delta,
        delta_pct=delta_pct,
        threshold=_threshold_dict(threshold),
        details={"description": "SKUs the loader applied vs current versions in the warehouse"},
    )


def _check_quarantine_rate(
    records: Sequence[MappedRecord], threshold: Threshold | None
) -> CheckResult:
    received = len(records)
    quarantined = sum(1 for record in records if not record.is_valid)
    rate = _percentage(Decimal(quarantined), received)

    return CheckResult(
        check_name=QUARANTINE_RATE,
        kind=CheckKind.VOLUME,
        status=_grade(rate, threshold),
        source_value=str(received),
        target_value=str(received - quarantined),
        delta=Decimal(-quarantined),
        delta_pct=rate,
        threshold=_threshold_dict(threshold),
        details={"quarantined": quarantined, "accepted": received - quarantined},
    )


def _check_duplicate_rate(
    records: Sequence[MappedRecord], threshold: Threshold | None
) -> CheckResult:
    duplicates = sum(
        1 for record in records for failure in record.failures if failure.rule_name == "unique"
    )
    rate = _percentage(Decimal(duplicates), len(records))

    return CheckResult(
        check_name=DUPLICATE_RATE,
        kind=CheckKind.VOLUME,
        status=_grade(rate, threshold),
        source_value=str(len(records)),
        target_value=str(duplicates),
        delta=Decimal(duplicates),
        delta_pct=rate,
        threshold=_threshold_dict(threshold),
        details={"duplicate_records": duplicates},
    )


def _check_distinct_keys(records: Sequence[MappedRecord]) -> CheckResult:
    """Distinct business keys vs rows received.

    Informational rather than thresholded: a feed with legitimate repeats is not
    wrong, but the ratio is the fastest way to see that a "full extract" is
    actually a change log, or that a join upstream fanned out.
    """
    keys = [
        str(record.values[BUSINESS_KEY])
        for record in records
        if record.values.get(BUSINESS_KEY) is not None
    ]
    distinct = len(set(keys))
    return CheckResult(
        check_name=DISTINCT_KEY_COUNT,
        kind=CheckKind.VOLUME,
        status=CheckStatus.PASS,
        source_value=str(len(records)),
        target_value=str(distinct),
        delta=Decimal(distinct - len(records)),
        delta_pct=_percentage(Decimal(len(records) - distinct), len(records)),
        details={"business_key": BUSINESS_KEY, "distinct_keys": distinct},
    )


def _check_orphan_rate(records: Sequence[MappedRecord], threshold: Threshold | None) -> CheckResult:
    """Records pointing at reference data that does not exist."""
    orphans = {
        record.ordinal
        for record in records
        for failure in record.failures
        if failure.rule_name == "references"
    }
    rate = _percentage(Decimal(len(orphans)), len(records))
    return CheckResult(
        check_name=ORPHAN_RATE,
        kind=CheckKind.VOLUME,
        status=_grade(rate, threshold),
        source_value=str(len(records)),
        target_value=str(len(orphans)),
        delta=Decimal(len(orphans)),
        delta_pct=rate,
        threshold=_threshold_dict(threshold),
        details={"orphaned_records": len(orphans)},
    )


# ---------------------------------------------------------------------------
# Content checks
# ---------------------------------------------------------------------------


def _check_content_checksum(
    connection: Connection,
    contract: Contract,
    valid: Sequence[MappedRecord],
    applied_skus: list[str],
    outcome: LoadOutcome,
) -> CheckResult:
    """Compare a Python digest of the source against a Postgres digest of the target.

    Scoped to the SKUs this run actually applied. Superseded records are excluded
    on purpose: a lower-precedence feed's values were *deliberately* not written,
    so including them would report a mismatch for behaviour that is working
    exactly as designed.
    """
    columns = contract.reconciliation.checksum_columns
    applied = set(applied_skus)
    scoped = [record for record in valid if str(record.values.get(BUSINESS_KEY)) in applied]

    source_digest = source_checksum(scoped, columns)
    target_rows, target_digest = target_checksum(connection, applied_skus, columns)
    matched = source_digest == target_digest

    return CheckResult(
        check_name=CONTENT_CHECKSUM,
        kind=CheckKind.CONTENT,
        status=CheckStatus.PASS if matched else CheckStatus.FAIL,
        source_value=source_digest,
        target_value=target_digest,
        details={
            "columns": list(columns),
            "source_rows": len(scoped),
            "target_rows": target_rows,
            "superseded_excluded": outcome.superseded,
            "algorithm": "sha256 over canonicalised rows, digests sorted then hashed",
            "interpretation": (
                "Equal digests mean every checksummed value survived mapping and "
                "storage intact. A mismatch with equal row counts is silent "
                "corruption — the failure mode volume reconciliation cannot see."
            ),
        },
    )


def _check_null_rates(
    contract: Contract,
    valid: Sequence[MappedRecord],
    previous: dict[str, float],
    threshold: Threshold | None,
) -> list[CheckResult]:
    """Per-column null rate, compared with the previous successful run."""
    columns = contract.reconciliation.null_rate_columns
    if not columns or not valid:
        return []

    results = []
    for column in columns:
        nulls = sum(1 for record in valid if record.values.get(column) is None)
        rate = _percentage(Decimal(nulls), len(valid))
        prior = previous.get(column)

        if prior is None:
            # No baseline yet. Reporting PASS is honest — there is nothing to
            # compare against — and the details say so rather than implying a
            # comparison happened.
            status = CheckStatus.PASS
            delta = None
            baseline_rate = None
        else:
            # Quantised to the same scale as the current rate, so the two are
            # visually comparable rather than "0.0000% vs 0.0%".
            baseline_rate = Decimal(str(prior)).quantize(Decimal("0.0001"))
            delta = rate - baseline_rate
            status = _grade(abs(delta), threshold)

        results.append(
            CheckResult(
                check_name=NULL_RATE,
                kind=CheckKind.CONTENT,
                dimension=column,
                status=status,
                source_value=f"{rate}%",
                target_value=None if baseline_rate is None else f"{baseline_rate}%",
                delta=delta,
                delta_pct=delta,
                threshold=_threshold_dict(threshold),
                details={
                    "null_records": nulls,
                    "of_records": len(valid),
                    "baseline": "previous successful run"
                    if prior is not None
                    else "none — first run for this source",
                },
            )
        )
    return results


def null_rates(contract: Contract, records: Sequence[MappedRecord]) -> dict[str, float]:
    """Current null rates, stored on the run so the next run has a baseline."""
    valid = [record for record in records if record.is_valid]
    if not valid:
        return {}
    return {
        column: float(
            _percentage(
                Decimal(sum(1 for record in valid if record.values.get(column) is None)),
                len(valid),
            )
        )
        for column in contract.reconciliation.null_rate_columns
    }


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def _grade(
    value: Decimal,
    threshold: Threshold | None,
    *,
    zero_is_pass: bool = False,
) -> CheckStatus:
    """Grade a metric against its thresholds.

    Without a configured threshold the check is informational: it is reported but
    cannot fail a run. Inventing a default would make a contract's silence mean
    something the contract author never chose.
    """
    if threshold is None:
        return CheckStatus.PASS
    if zero_is_pass and value == 0:
        return CheckStatus.PASS
    if value > threshold.fail:
        return CheckStatus.FAIL
    if value > threshold.warn:
        return CheckStatus.WARN
    return CheckStatus.PASS


def _threshold_dict(threshold: Threshold | None) -> dict[str, Any]:
    if threshold is None:
        return {"configured": False}
    return {"warn": str(threshold.warn), "fail": str(threshold.fail), "configured": True}


def _percentage(numerator: Decimal, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal(0)
    return (numerator * _HUNDRED / Decimal(denominator)).quantize(Decimal("0.0001"))
