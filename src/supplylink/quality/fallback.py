"""The deterministic quality report.

Used whenever generation is unavailable — no key configured, provider down, or
the numeric guard rejected the draft. It is written from the same metrics
payload, so it is never less *accurate* than a generated report; it is only less
readable.

That matters more than it sounds. Because this path is the default in any
environment without an API key, it runs constantly in tests and in CI, which
means it is exercised rather than being emergency code that has never executed.
A fallback that only runs during an outage is a fallback that fails during an
outage.
"""

from __future__ import annotations

from typing import Any

#: Values longer than this are abbreviated in tables. A SHA-256 checksum is 64
#: characters and destroys the layout at full width.
_MAX_CELL_CHARS = 20
_ABBREVIATED_CHARS = 12


def render(metrics: dict[str, Any], reason: str) -> str:
    """A complete markdown report built only from computed metrics."""
    run = metrics.get("run", {})
    volumes = metrics.get("volumes", {})
    reconciliation = metrics.get("reconciliation", {})
    comparison = metrics.get("comparison", {})

    lines = [
        f"# Data quality — {run.get('source_name', 'unknown source')}",
        "",
        f"*Generated deterministically from pipeline metrics. {reason}*",
        "",
        "## Summary",
        "",
        f"Run `{run.get('run_id', '?')}` processed **{volumes.get('records_received', 0):,}** "
        f"records from `{run.get('source_id', '?')}` under contract version "
        f"{run.get('contract_version', '?')}. "
        f"**{volumes.get('records_valid', 0):,}** passed validation and "
        f"**{volumes.get('records_quarantined', 0):,}** were quarantined "
        f"({volumes.get('quarantine_rate_pct', 0)}%).",
        "",
        "| Outcome | Records |",
        "| --- | ---: |",
        f"| Received | {volumes.get('records_received', 0):,} |",
        f"| Accepted | {volumes.get('records_valid', 0):,} |",
        f"| Quarantined | {volumes.get('records_quarantined', 0):,} |",
        f"| Inserted | {volumes.get('rows_inserted', 0):,} |",
        f"| Updated | {volumes.get('rows_updated', 0):,} |",
        f"| Unchanged | {volumes.get('rows_unchanged', 0):,} |",
        f"| Superseded | {volumes.get('rows_superseded', 0):,} |",
        "",
    ]

    failures = metrics.get("top_failures") or []
    lines += ["## What failed", ""]
    if not failures:
        lines += ["No records were rejected.", ""]
    else:
        lines += [
            "| Rule | Field | Failures | Records | Share |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        lines += [
            f"| `{failure['rule']}` | `{failure.get('field') or '—'}` | "
            f"{failure['failure_count']:,} | {failure['affected_records']:,} | "
            f"{failure['share_of_failures_pct']}% |"
            for failure in failures
        ]
        lines.append("")

    warnings = metrics.get("warnings") or []
    if warnings:
        lines += ["### Warnings (recorded, records still loaded)", ""]
        lines += [f"- `{warning['rule']}` — {warning['count']:,}" for warning in warnings]
        lines.append("")

    lines += [
        "## Reconciliation",
        "",
        f"Overall status: **{reconciliation.get('overall_status', 'UNKNOWN')}** "
        f"({reconciliation.get('checks_passing', 0)} of "
        f"{reconciliation.get('checks_run', 0)} checks passing).",
        "",
        "| Check | Kind | Status | Source | Target |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in reconciliation.get("checks", []):
        lines.append(
            f"| {check['name']} | {check['kind']} | {check['status']} | "
            f"{_short(check.get('source_value'))} | {_short(check.get('target_value'))} |"
        )
    lines.append("")

    lines += ["## Compared with the previous run", ""]
    if not comparison.get("available"):
        lines += [
            comparison.get("note", "No previous run is available for comparison."),
            "",
        ]
    else:
        lines += [
            f"- Records received: {comparison['records_received_change']:+,} "
            f"({comparison['records_received_change_pct']:+}%)",
            f"- Records quarantined: {comparison['records_quarantined_change']:+,}",
            f"- Quarantine rate: {comparison['quarantine_rate_change_pct']:+}"
            f" percentage points (now {volumes.get('quarantine_rate_pct', 0)}%, "
            f"was {comparison['previous_quarantine_rate_pct']}%)",
            "",
        ]

    return "\n".join(lines).rstrip() + "\n"


def _short(value: Any) -> str:
    """Abbreviate long values — a checksum column is unreadable at full width."""
    text = "—" if value is None else str(value)
    if len(text) <= _MAX_CELL_CHARS:
        return text
    return f"`{text[:_ABBREVIATED_CHARS]}…`"
