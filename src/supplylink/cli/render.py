"""Terminal rendering.

Kept separate from the commands so that command functions decide *what* to do
and this module decides *how it looks*. That split is what lets the same
pipeline result be printed as a table, emitted as JSON for a script, or returned
by the API without any of them knowing about the others.

Rich is used for tables and colour, but every command also supports ``--json``.
A CLI whose output can only be read by a human is not an operational tool.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from supplylink.pipeline import PipelineResult
    from supplylink.reconciliation.metrics import ReconciliationReport


def _output_width() -> int | None:
    """Width to render tables at.

    ``None`` lets Rich detect the terminal. When output is not a terminal Rich
    falls back to 80 columns and silently truncates every table cell, which
    mangles ``supplylink runs list > runs.txt`` and makes piped output useless.
    A generous fixed width for the non-interactive case keeps redirected output
    readable; ``COLUMNS`` still overrides everything.
    """
    if os.environ.get("COLUMNS"):
        return None
    return None if sys.stdout.isatty() else 160


console = Console(stderr=False, highlight=False, width=_output_width())
error_console = Console(stderr=True, highlight=False, width=_output_width())

STATUS_STYLE = {
    "PASS": "green",
    "SUCCEEDED": "green",
    "WARN": "yellow",
    "SUCCEEDED_WITH_WARNINGS": "yellow",
    "SKIPPED": "cyan",
    "FAIL": "red",
    "FAILED": "red",
    "SUCCEEDED_WITH_FAILURES": "red",
    "GENERATED": "green",
    "FALLBACK": "yellow",
    "RUNNING": "blue",
}


def status_text(status: str) -> Text:
    return Text(status, style=f"bold {STATUS_STYLE.get(status, 'white')}")


def cell(value: Any) -> str:
    """Render a data-derived value safely inside a Rich table.

    Rich parses square brackets as style markup, so a value like
    ``null_rate_pct[product_name]`` — or a product name like ``USB Hub [refurb]``
    — would have that text silently swallowed. Every value that comes from the
    database or from source data goes through here; only literals we control are
    written as markup.
    """
    return escape("—" if value is None else str(value))


def emit_json(payload: Any) -> None:
    """Machine-readable output on stdout."""
    console.print_json(json.dumps(payload, default=str))


def render_run_result(result: PipelineResult, overall: str) -> None:
    """The headline view of a completed run."""
    counters = result.counters

    header = Table.grid(padding=(0, 2))
    header.add_column(style="dim", justify="right")
    header.add_column()
    header.add_row("Run ID", str(result.run_id))
    header.add_row("Source", result.source_id)
    header.add_row("Contract", f"v{result.contract_version}")
    header.add_row("Duration", f"{result.duration_seconds:.2f}s")
    header.add_row("Status", status_text(overall))
    console.print(Panel(header, title="SupplyLink pipeline", border_style="blue", expand=False))

    if result.skipped_reason:
        console.print(
            Panel(
                Text(result.skipped_reason, style="cyan"),
                title="Skipped — idempotency",
                border_style="cyan",
                expand=False,
            )
        )
        return

    volumes = Table(title="Records", title_justify="left", header_style="bold")
    volumes.add_column("Stage")
    volumes.add_column("Count", justify="right")
    volumes.add_row("Received", f"{counters.records_received:,}")
    volumes.add_row("Validated", f"[green]{counters.records_valid:,}[/green]")
    volumes.add_row(
        "Quarantined",
        f"[red]{counters.records_quarantined:,}[/red]" if counters.records_quarantined else "0",
    )
    volumes.add_section()
    volumes.add_row("Inserted", f"{counters.rows_inserted:,}")
    volumes.add_row("Updated", f"{counters.rows_updated:,}")
    volumes.add_row("Unchanged", f"{counters.rows_unchanged:,}")
    volumes.add_row("Superseded", f"{counters.rows_superseded:,}")
    console.print(volumes)

    if result.reconciliation is not None:
        render_reconciliation(result.reconciliation)

    if result.quality_report is not None:
        report = result.quality_report
        detail = f"{report['provider']} / {report['model']}"
        console.print(f"\nQuality report  {status_text(report['status'])}  [dim]{detail}[/dim]")
        if report.get("error_message"):
            console.print(f"[dim]  {report['error_message']}[/dim]")


def render_reconciliation(report: ReconciliationReport) -> None:
    """The reconciliation table.

    Volume and content checks are labelled distinctly because the difference is
    the point: a passing content checksum is evidence that values survived, and
    a passing row count is not.
    """
    table = Table(title="Reconciliation", title_justify="left", header_style="bold")
    # Check and dimension are separate columns rather than one combined label:
    # "null_rate_pct[product_name]" is long enough to wrap mid-word on a narrow
    # terminal, which reads as four identical rows.
    table.add_column("Check")
    table.add_column("Dimension")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Source", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Δ%", justify="right")

    for check in report.checks:
        table.add_row(
            cell(check.check_name),
            cell(check.dimension),
            check.kind.value,
            status_text(check.status.value),
            _abbreviate(check.source_value),
            _abbreviate(check.target_value),
            "" if check.delta_pct is None else f"{check.delta_pct:+}",
        )

    console.print(table)
    console.print(f"Overall  {status_text(report.status.value)}")


def render_failures(rows: Sequence[Any]) -> None:
    """Top failure categories for a run."""
    if not rows:
        console.print("[green]No validation failures recorded for this run.[/green]")
        return

    table = Table(title="Validation failures", title_justify="left", header_style="bold")
    table.add_column("Rule")
    table.add_column("Field")
    table.add_column("Severity")
    table.add_column("Failures", justify="right")
    table.add_column("Records", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("Example value")

    for row in rows:
        samples = row.sample_actual_values or []
        table.add_row(
            cell(row.rule_name),
            cell(row.field_name),
            "[red]error[/red]" if row.severity == "error" else "[yellow]warning[/yellow]",
            f"{row.failure_count:,}",
            f"{row.affected_records:,}",
            f"{row.share_of_failures_pct}%",
            cell(samples[0]) if samples else "—",
        )
    console.print(table)


def render_runs(rows: Sequence[Any]) -> None:
    table = Table(title="Pipeline runs", title_justify="left", header_style="bold")
    table.add_column("Run")
    table.add_column("Source")
    table.add_column("Started")
    table.add_column("Status")
    table.add_column("Recon")
    table.add_column("Recv", justify="right")
    table.add_column("Quar", justify="right")
    table.add_column("Ins", justify="right")
    table.add_column("Upd", justify="right")
    table.add_column("Unch", justify="right")

    for row in rows:
        table.add_row(
            str(row.run_id)[:8],
            cell(row.source_id),
            row.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            status_text(row.status),
            status_text(row.reconciliation_status or "—"),
            f"{row.records_received:,}",
            f"{row.records_quarantined:,}",
            f"{row.rows_inserted:,}",
            f"{row.rows_updated:,}",
            f"{row.rows_unchanged:,}",
        )
    console.print(table)


def render_history(rows: Sequence[Any]) -> None:
    """SCD-2 version history for a SKU."""
    if not rows:
        console.print("[yellow]No history for that SKU.[/yellow]")
        return

    table = Table(title=f"Version history — {rows[0].canonical_sku}", title_justify="left")
    table.add_column("v", justify="right")
    table.add_column("Name")
    table.add_column("Supplier")
    table.add_column("Price", justify="right")
    table.add_column("Status")
    table.add_column("Effective from")
    table.add_column("Effective to")
    table.add_column("Changed")

    for row in rows:
        table.add_row(
            str(row.version_number),
            cell(row.product_name),
            cell(row.supplier_id),
            f"{row.unit_price:,.2f} {cell(row.currency)}",
            cell(row.status),
            row.effective_from.strftime("%Y-%m-%d %H:%M:%S"),
            (
                row.effective_to.strftime("%Y-%m-%d %H:%M:%S")
                if row.effective_to
                else "[green]current[/green]"
            ),
            cell(", ".join(row.changed_attributes))
            if row.changed_attributes
            else "[dim]initial[/dim]",
        )
    console.print(table)


def _abbreviate(value: Any, width: int = 18) -> str:
    text = "—" if value is None else str(value)
    return escape(text if len(text) <= width else f"{text[: width - 1]}…")
