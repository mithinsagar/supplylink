"""The SupplyLink command line.

Organised by noun rather than verb — ``supplylink runs show`` instead of
``supplylink show-run`` — so the surface stays discoverable as it grows.

Two conventions hold across every command:

* **Exit codes carry meaning.** 0 clean, 1 a run-level failure, 2 the load
  succeeded but reconciliation did not. A cron job or CI step can branch on that
  without parsing output. A pipeline that returns 0 whatever happens is not
  operable.
* **Everything supports ``--json``.** Rich tables are for people; the JSON is
  for the next script in the chain.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.markdown import Markdown
from rich.table import Table
from sqlalchemy import select

from supplylink import __version__
from supplylink.cli import render
from supplylink.config import get_settings
from supplylink.contracts.loader import ContractRegistry, register_contract
from supplylink.db import dispose_engine, healthcheck, load_sql, read_connection, transaction
from supplylink.errors import SupplyLinkError
from supplylink.ingestion import build_adapter
from supplylink.observability import configure_logging
from supplylink.pipeline import overall_status, run_pipeline
from supplylink.transformation.functions import available_transforms, get_transform
from supplylink.transformation.mapper import map_record
from supplylink.validation.engine import summarise_failures, validate_batch
from supplylink.warehouse import schema
from supplylink.warehouse.reference import seed_suppliers, supplier_count

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_RECONCILIATION = 2

app = typer.Typer(
    name="supplylink",
    help="Multi-source data integration and reconciliation for supply-chain master data.",
    no_args_is_help=True,
    add_completion=False,
)
contracts_app = typer.Typer(help="Inspect and validate data contracts.", no_args_is_help=True)
runs_app = typer.Typer(help="Inspect pipeline runs.", no_args_is_help=True)
sku_app = typer.Typer(help="Query the canonical product model.", no_args_is_help=True)
db_app = typer.Typer(help="Database lifecycle.", no_args_is_help=True)

app.add_typer(contracts_app, name="contracts")
app.add_typer(runs_app, name="runs")
app.add_typer(sku_app, name="sku")
app.add_typer(db_app, name="db")

JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")]


@app.callback()
def main(
    log_level: Annotated[
        str, typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR.")
    ] = "INFO",
    log_format: Annotated[str, typer.Option("--log-format", help="console or json.")] = "console",
) -> None:
    """Configure logging before any command runs."""
    configure_logging(log_level.upper(), log_format)


@app.command()
def version() -> None:
    """Print the SupplyLink version."""
    render.console.print(f"supplylink {__version__}")


@app.command()
def health(as_json: JsonOption = False) -> None:
    """Check database connectivity and contract loading."""
    settings = get_settings()
    db_ok, db_detail = healthcheck()

    try:
        registry = ContractRegistry.discover(settings.contracts_dir)
        contracts_ok, contract_detail = True, f"{len(registry)} contracts loaded"
    except SupplyLinkError as exc:
        contracts_ok, contract_detail = False, str(exc)

    payload = {
        "status": "ok" if db_ok and contracts_ok else "degraded",
        "database": {"ok": db_ok, "detail": db_detail, "url": settings.masked_database_url()},
        "contracts": {"ok": contracts_ok, "detail": contract_detail},
        "llm": {"configured": settings.llm_configured, "provider": settings.llm_provider},
        "version": __version__,
    }

    if as_json:
        render.emit_json(payload)
    else:
        db_verdict = render.status_text("PASS" if db_ok else "FAIL")
        contract_verdict = render.status_text("PASS" if contracts_ok else "FAIL")
        llm_note = (
            f"configured ({settings.llm_provider})"
            if settings.llm_configured
            else "not configured — reports use the deterministic fallback"
        )
        render.console.print(f"Database   {db_verdict}  {db_detail}")
        render.console.print(f"Contracts  {contract_verdict}  {contract_detail}")
        render.console.print(f"LLM        {llm_note}")

    raise typer.Exit(EXIT_OK if db_ok and contracts_ok else EXIT_FAILED)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@app.command()
def run(
    source: Annotated[str, typer.Option("--source", "-s", help="Source id from a contract.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Reload a payload that was already loaded successfully."),
    ] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Skip the quality report entirely.")
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Run the full pipeline for one source."""
    try:
        result = run_pipeline(source, trigger="cli", force=force, skip_quality_report=no_llm)
    except SupplyLinkError as exc:
        render.error_console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
        raise typer.Exit(EXIT_FAILED) from exc

    status = overall_status(result)

    if as_json:
        render.emit_json(
            {
                "run_id": str(result.run_id),
                "source_id": result.source_id,
                "contract_version": result.contract_version,
                "status": status,
                "duration_seconds": round(result.duration_seconds, 3),
                "counters": result.counters.as_dict(),
                "reconciliation": {
                    "status": result.reconciliation_status,
                    "checks": [
                        {
                            "name": check.label,
                            "kind": check.kind.value,
                            "status": check.status.value,
                            "source_value": check.source_value,
                            "target_value": check.target_value,
                        }
                        for check in (result.reconciliation.checks if result.reconciliation else ())
                    ],
                },
                "quality_report_status": (result.quality_report or {}).get("status"),
                "skipped_reason": result.skipped_reason,
            }
        )
    else:
        render.render_run_result(result, status)

    raise typer.Exit(EXIT_RECONCILIATION if status == "SUCCEEDED_WITH_FAILURES" else EXIT_OK)


@app.command()
def validate(
    source: Annotated[str, typer.Option("--source", "-s")],
    as_json: JsonOption = False,
) -> None:
    """Validate a source without writing anything to the warehouse.

    A dry run: the source is read, mapped and validated, and the failures are
    reported. Nothing is staged, loaded or recorded. Useful for checking a
    contract change against real data before it touches the database.
    """
    settings = get_settings()
    try:
        contract = ContractRegistry.discover(settings.contracts_dir).get(source)
        extraction = build_adapter(contract).extract()
        records = [
            map_record(record.payload, contract, ordinal=record.ordinal)
            for record in extraction.records
        ]
        with read_connection() as connection:
            validate_batch(records, contract, connection)
    except SupplyLinkError as exc:
        render.error_console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
        raise typer.Exit(EXIT_FAILED) from exc

    summary = summarise_failures(records)
    summary["source_id"] = contract.source_id
    summary["contract_version"] = contract.version
    summary["dry_run"] = True

    if as_json:
        render.emit_json(summary)
        raise typer.Exit(EXIT_OK)

    render.console.print(
        f"[bold]{contract.source_id}[/bold] v{contract.version} — dry run, nothing written\n"
        f"  received     {summary['records']:,}\n"
        f"  valid        [green]{summary['valid']:,}[/green]\n"
        f"  would reject [red]{summary['quarantined']:,}[/red]\n"
    )
    if summary["by_rule"]:
        table_rows = sorted(summary["by_rule"].items(), key=lambda item: -item[1])
        for rule, count in table_rows:
            render.console.print(f"  {count:>6,}  {rule}")
    raise typer.Exit(EXIT_OK)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


@contracts_app.command("list")
def contracts_list(as_json: JsonOption = False) -> None:
    """List every discovered contract."""
    registry = _load_registry()
    payload: list[dict[str, Any]] = [
        {
            "source_id": contract.source_id,
            "display_name": contract.source.display_name,
            "type": contract.source.type,
            "precedence": contract.source.precedence,
            "version": contract.version,
            "content_hash": contract.content_hash,
            "effective_from": contract.contract.effective_from.isoformat(),
            "path": contract.path,
        }
        for contract in registry.all()
    ]

    if as_json:
        render.emit_json(payload)
        return

    table = Table(title="Data contracts", title_justify="left", header_style="bold")
    for column in ("Source", "Name", "Type", "Prec", "Version", "Hash", "Path"):
        table.add_column(column)
    for entry in payload:
        path = Path(str(entry["path"]))
        display_path = (
            str(path.relative_to(Path.cwd())) if path.is_relative_to(Path.cwd()) else str(path)
        )
        table.add_row(
            str(entry["source_id"]),
            str(entry["display_name"]),
            str(entry["type"]),
            str(entry["precedence"]),
            str(entry["version"]),
            str(entry["content_hash"])[:10],
            display_path,
        )
    render.console.print(table)


@contracts_app.command("validate")
def contracts_validate() -> None:
    """Parse and cross-check every contract. Exits non-zero on the first problem."""
    settings = get_settings()
    try:
        registry = ContractRegistry.discover(settings.contracts_dir)
    except SupplyLinkError as exc:
        render.error_console.print(f"[red]Contract error:[/red] {exc}")
        raise typer.Exit(EXIT_FAILED) from exc

    for contract in registry.all():
        render.console.print(
            f"[green]✓[/green] {contract.source_id:20} v{contract.version:8} "
            f"{len(contract.mapping)} mapped, {len(contract.validation)} validated, "
            f"{len(contract.rules)} rules"
        )
    render.console.print(f"\n{len(registry)} contracts valid.")


@contracts_app.command("sync")
def contracts_sync() -> None:
    """Register every contract version in the warehouse.

    Fails if a published version's definition has changed without a version bump.
    """
    registry = _load_registry()
    try:
        with transaction() as connection:
            for contract in registry.all():
                contract_version_id = register_contract(connection, contract)
                render.console.print(
                    f"[green]✓[/green] {contract.source_id:20} v{contract.version} "
                    f"→ contract_version_id={contract_version_id}"
                )
    except SupplyLinkError as exc:
        render.error_console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
        raise typer.Exit(EXIT_FAILED) from exc


@contracts_app.command("transforms")
def contracts_transforms() -> None:
    """List the transforms a contract may reference."""
    for name in available_transforms():
        doc = (get_transform(name).__doc__ or "").strip().split("\n")[0]
        render.console.print(f"  [cyan]{name:24}[/cyan] {doc}")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@runs_app.command("list")
def runs_list(
    source: Annotated[str | None, typer.Option("--source", "-s")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    as_json: JsonOption = False,
) -> None:
    """List recent pipeline runs."""
    with read_connection() as connection:
        rows = connection.execute(
            load_sql("run_summary"),
            {"run_id": None, "source_id": source, "limit": limit},
        ).all()

    if as_json:
        render.emit_json([dict(row._mapping) for row in rows])
    else:
        render.render_runs(rows)


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Full run UUID.")],
    as_json: JsonOption = False,
) -> None:
    """Show one run in detail, including per-stage timings."""
    identifier = _parse_uuid(run_id)
    with read_connection() as connection:
        match = connection.execute(
            load_sql("run_summary"),
            {"run_id": identifier, "source_id": None, "limit": 1},
        ).one_or_none()
        if match is None:
            render.error_console.print(f"[red]No such run:[/red] {run_id}")
            raise typer.Exit(EXIT_FAILED)

        reconciliation = connection.execute(
            select(schema.reconciliation_results)
            .where(schema.reconciliation_results.c.run_id == identifier)
            .order_by(schema.reconciliation_results.c.reconciliation_id)
        ).all()

    payload = {
        "run": dict(match._mapping),
        "reconciliation": [dict(row._mapping) for row in reconciliation],
    }
    if as_json:
        render.emit_json(payload)
        return

    render.render_runs([match])
    if match.timings:
        render.console.print("\n[bold]Stage timings[/bold]")
        for stage, detail in match.timings.items():
            render.console.print(
                f"  {stage:18} {render.status_text(detail['status'])}  {detail['ms']:>7} ms"
            )
    if reconciliation:
        table = Table(title="\nReconciliation", title_justify="left", header_style="bold")
        for column in ("Check", "Dimension", "Kind", "Status", "Source", "Target"):
            table.add_column(column)
        for row in reconciliation:
            table.add_row(
                row.check_name,
                row.dimension,
                row.check_kind,
                render.status_text(row.status),
                (row.source_value or "—")[:20],
                (row.target_value or "—")[:20],
            )
        render.console.print(table)


@runs_app.command("failures")
def runs_failures(
    run_id: Annotated[str, typer.Argument(help="Full run UUID.")],
    as_json: JsonOption = False,
) -> None:
    """Show the failure categories for a run."""
    identifier = _parse_uuid(run_id)
    with read_connection() as connection:
        rows = connection.execute(load_sql("run_failures"), {"run_id": identifier}).all()

    if as_json:
        render.emit_json([dict(row._mapping) for row in rows])
    else:
        render.render_failures(rows)


@runs_app.command("quarantine")
def runs_quarantine(
    run_id: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Show quarantined records with the rules each one broke."""
    identifier = _parse_uuid(run_id)
    with read_connection() as connection:
        rows = connection.execute(
            load_sql("quarantine_detail"), {"run_id": identifier, "limit": limit}
        ).all()

    if not rows:
        render.console.print("[green]No quarantined records for this run.[/green]")
        return

    for row in rows:
        render.console.print(
            f"\n[bold]record {row.source_record_id or '(no id)'}[/bold] "
            f"[dim]source line {row.source_line}, {row.failure_count} failure(s)[/dim]"
        )
        render.console.print(f"  [dim]payload:[/dim] {row.raw_payload}")
        for failure in row.failures:
            render.console.print(
                f"  [red]✗[/red] {failure['field'] or '<record>'}: {failure['message']}"
                f"  [dim](expected {failure['expected']}, got {failure['actual']})[/dim]"
            )


@runs_app.command("quality")
def runs_quality(
    run_id: Annotated[str | None, typer.Argument(help="Omit for the latest run.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Print the data-quality report for a run."""
    with read_connection() as connection:
        query = select(schema.quality_reports)
        if run_id is None:
            query = query.order_by(schema.quality_reports.c.generated_at.desc()).limit(1)
        else:
            query = query.where(schema.quality_reports.c.run_id == _parse_uuid(run_id))
        report = connection.execute(query).one_or_none()

    if report is None:
        render.error_console.print("[yellow]No quality report found.[/yellow]")
        raise typer.Exit(EXIT_FAILED)

    if as_json:
        render.emit_json(dict(report._mapping))
        return

    render.console.print(
        f"[dim]{report.status} · {report.provider}/{report.model} · "
        f"prompt {report.prompt_version} · {report.generated_at:%Y-%m-%d %H:%M:%S}[/dim]\n"
    )
    render.console.print(Markdown(report.summary_markdown))


# ---------------------------------------------------------------------------
# SKU
# ---------------------------------------------------------------------------


@sku_app.command("show")
def sku_show(sku: Annotated[str, typer.Argument()], as_json: JsonOption = False) -> None:
    """Show the current canonical state of a SKU."""
    with read_connection() as connection:
        row = connection.execute(
            load_sql("point_in_time_product"),
            {"sku": sku.upper(), "as_of": datetime.now(UTC)},
        ).one_or_none()

    if row is None:
        render.error_console.print(f"[yellow]No current version for {sku.upper()}.[/yellow]")
        raise typer.Exit(EXIT_FAILED)

    if as_json:
        render.emit_json(dict(row._mapping))
        return

    for key, value in row._mapping.items():
        render.console.print(f"  [dim]{key:18}[/dim] {value}")


@sku_app.command("history")
def sku_history(sku: Annotated[str, typer.Argument()], as_json: JsonOption = False) -> None:
    """Show the full SCD Type 2 version history of a SKU."""
    with read_connection() as connection:
        rows = connection.execute(load_sql("product_history"), {"sku": sku.upper()}).all()

    if as_json:
        render.emit_json([dict(row._mapping) for row in rows])
    else:
        render.render_history(rows)


@sku_app.command("as-of")
def sku_as_of(
    sku: Annotated[str, typer.Argument()],
    as_of: Annotated[str, typer.Argument(help="ISO 8601 timestamp, e.g. 2026-03-14T09:00:00Z")],
    as_json: JsonOption = False,
) -> None:
    """Point-in-time lookup: what did this SKU look like at a given instant?"""
    moment = _parse_timestamp(as_of)
    with read_connection() as connection:
        row = connection.execute(
            load_sql("point_in_time_product"), {"sku": sku.upper(), "as_of": moment}
        ).one_or_none()

    if row is None:
        render.console.print(
            f"[yellow]{sku.upper()} had no version in effect at {moment.isoformat()}.[/yellow]"
        )
        raise typer.Exit(EXIT_OK)

    if as_json:
        render.emit_json(dict(row._mapping))
        return

    render.console.print(f"[bold]{sku.upper()}[/bold] as of [cyan]{moment.isoformat()}[/cyan]")
    for key, value in row._mapping.items():
        render.console.print(f"  [dim]{key:18}[/dim] {value}")


@sku_app.command("lineage")
def sku_lineage(sku: Annotated[str, typer.Argument()]) -> None:
    """Trace every version of a SKU back to the source record that produced it."""
    with read_connection() as connection:
        rows = connection.execute(load_sql("lineage_trace"), {"sku": sku.upper()}).all()

    if not rows:
        render.error_console.print(f"[yellow]No lineage for {sku.upper()}.[/yellow]")
        raise typer.Exit(EXIT_FAILED)

    for row in rows:
        render.console.print(
            f"\n[bold]v{row.version_number}[/bold]  {row.product_name}  "
            f"{row.unit_price} {row.currency}"
        )
        render.console.print(f"  source        {row.source_id} record {row.source_record_id!r}")
        render.console.print(f"  source line   {row.source_line}")
        render.console.print(f"  payload       {row.source_payload}")
        render.console.print(f"  run           {row.run_id} ({row.run_status})")
        render.console.print(
            f"  contract      v{row.contract_version} [dim]{row.contract_hash[:12]}[/dim]"
        )
        render.console.print(
            f"  reconciled    {row.reconciliation_status} "
            f"(content checksum {row.content_checksum_status})"
        )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
) -> None:
    """Apply database migrations."""
    _alembic("upgrade", revision)
    render.console.print(f"[green]✓[/green] migrated to {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument()] = "-1") -> None:
    """Roll back database migrations."""
    _alembic("downgrade", revision)
    render.console.print(f"[green]✓[/green] rolled back to {revision}")


@db_app.command("seed")
def db_seed(
    path: Annotated[Path | None, typer.Option("--path", help="Supplier reference CSV.")] = None,
) -> None:
    """Load supplier reference data.

    Must run before any pipeline: referential-integrity validation checks against
    this table, so an empty one would quarantine every record in every feed.
    """
    try:
        with transaction() as connection:
            written = seed_suppliers(connection, path)
            total = supplier_count(connection)
    except SupplyLinkError as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_FAILED) from exc

    render.console.print(f"[green]✓[/green] {written} suppliers written ({total} in total)")


def _alembic(command: str, revision: str) -> None:
    """Drive Alembic in-process.

    Imported lazily: Alembic pulls in a substantial dependency graph that only
    the two ``db`` commands need, and paying for it on every ``supplylink runs
    list`` would make the CLI noticeably slower to start.
    """
    from alembic import command as alembic_command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    settings = get_settings()
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    getattr(alembic_command, command)(config, revision)
    dispose_engine()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_registry() -> ContractRegistry:
    try:
        return ContractRegistry.discover(get_settings().contracts_dir)
    except SupplyLinkError as exc:
        render.error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_FAILED) from exc


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        render.error_console.print(f"[red]Not a valid run id:[/red] {value}")
        raise typer.Exit(EXIT_FAILED) from exc


def _parse_timestamp(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        render.error_console.print(
            f"[red]Not a valid ISO 8601 timestamp:[/red] {value}\nTry 2026-03-14T09:00:00Z"
        )
        raise typer.Exit(EXIT_FAILED) from exc
    # A naive timestamp is ambiguous, and history is stored in UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _entrypoint() -> Any:  # pragma: no cover - console script shim
    try:
        return app()
    finally:
        dispose_engine()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_entrypoint())
