#!/usr/bin/env python3
"""The SupplyLink end-to-end demonstration.

Runs the whole platform against the synthetic dataset and narrates what each
step proves. Everything printed here is computed live — nothing is hard-coded,
and the figures in the README come from running exactly this script.

CI runs it on every push (``--no-pause``), so a README that has drifted from the
code fails the build rather than quietly misleading a reader.

    python scripts/demo.py                 # interactive, pauses between acts
    python scripts/demo.py --no-pause      # straight through, for CI

It will:

1. rebuild the schema and load reference data;
2. ingest three sources that describe the same products in three dialects;
3. show validation quarantining bad records while good ones still load;
4. show a lower-precedence feed being superseded rather than overwriting;
5. deliver a second ERP extract and show SCD Type 2 opening versions only for
   attributes that actually changed;
6. answer a point-in-time question and trace a value back to its source bytes;
7. show reconciliation, and prove content checks catch what row counts miss;
8. print the data-quality report.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.table import Table  # noqa: E402
from sqlalchemy import text  # noqa: E402

from supplylink.cli import render  # noqa: E402
from supplylink.config import get_settings  # noqa: E402
from supplylink.contracts.loader import ContractRegistry  # noqa: E402
from supplylink.db import dispose_engine, load_sql, read_connection, transaction  # noqa: E402
from supplylink.errors import SupplyLinkError  # noqa: E402
from supplylink.observability import configure_logging  # noqa: E402
from supplylink.pipeline import overall_status, run_pipeline  # noqa: E402
from supplylink.reconciliation.checksums import target_checksum  # noqa: E402
from supplylink.warehouse.reference import seed_suppliers  # noqa: E402

console = Console(width=100 if not sys.stdout.isatty() else None, highlight=False)

VENDOR_SIM_PORT = 8081
ERP_LANDING = REPO_ROOT / "data" / "samples" / "erp"
ERP_EXTRACTS = REPO_ROOT / "data" / "samples" / "erp_extracts"
FIRST_EXTRACT = "erp_master_2026-03-01.csv"
SECOND_EXTRACT = "erp_master_2026-03-14.csv"

#: Number of versions a SKU needs before a before/after comparison is meaningful.
_VERSIONS_FOR_COMPARISON = 2


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


def act(number: int, title: str, subtitle: str) -> None:
    console.print()
    console.print(Rule(f"[bold cyan]{number}. {title}[/bold cyan]", style="cyan"))
    console.print(f"[dim]{subtitle}[/dim]\n")


def note(message: str) -> None:
    console.print(f"[dim]  → {message}[/dim]")


def pause(enabled: bool) -> None:
    if enabled and sys.stdin.isatty():
        console.input("\n[dim]  press enter to continue…[/dim]")


# ---------------------------------------------------------------------------
# Vendor simulator
# ---------------------------------------------------------------------------


def port_is_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@contextlib.contextmanager
def vendor_simulator():
    """Start the vendor API simulator if nothing is already serving it."""
    if port_is_open(VENDOR_SIM_PORT):
        note(f"vendor API already serving on :{VENDOR_SIM_PORT}")
        yield
        return

    note(f"starting the vendor API simulator on :{VENDOR_SIM_PORT}")
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tools.vendor_sim.app:app",
            "--port",
            str(VENDOR_SIM_PORT),
            "--log-level",
            "error",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            if port_is_open(VENDOR_SIM_PORT):
                break
            time.sleep(0.25)
        else:  # pragma: no cover - only on a very slow machine
            raise RuntimeError("the vendor simulator did not start")
        yield
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


# ---------------------------------------------------------------------------
# Acts
# ---------------------------------------------------------------------------


def prepare(pausing: bool) -> None:
    act(
        1,
        "Prepare the warehouse",
        "Rebuild the schema from migrations and load supplier reference data.",
    )

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    dispose_engine()
    note("schema rebuilt from migrations (downgrade to base, then upgrade to head)")

    with transaction() as connection:
        count = seed_suppliers(connection)
    note(f"{count} suppliers loaded — referential integrity checks against these")
    pause(pausing)


def ingest_three_sources(pausing: bool) -> dict[str, object]:
    act(
        2,
        "Ingest three sources",
        "The same products, described three different ways. Adding a source is a "
        "YAML contract, not a new pipeline.",
    )

    stage_extract(FIRST_EXTRACT)
    results: dict[str, object] = {}

    for source in ("erp_core", "acme_supplier", "globalparts_api"):
        result = run_pipeline(source, trigger="demo")
        results[source] = result
        counters = result.counters
        console.print(
            f"  [bold]{source:16}[/bold] "
            f"received [cyan]{counters.records_received:>4}[/cyan]   "
            f"valid [green]{counters.records_valid:>4}[/green]   "
            f"quarantined [red]{counters.records_quarantined:>3}[/red]   "
            f"inserted {counters.rows_inserted:>3}  "
            f"unchanged {counters.rows_unchanged:>3}  "
            f"superseded {counters.rows_superseded:>3}   "
            f"{render.status_text(overall_status(result))}"
        )

    console.print()
    note("ERP writes 'WM-001', ACME writes 'wm001', the vendor API writes 'wm_001'")
    note("all three normalise to WM001 — the same product, one canonical record")

    acme = results["acme_supplier"]
    note(
        f"ACME had {acme.counters.rows_superseded} records superseded: the ERP is more "
        f"authoritative (precedence 10 vs 20), so its values stand"
    )
    show_convergence()
    pause(pausing)
    return results


def show_convergence() -> None:
    with read_connection() as connection:
        rows = connection.execute(
            text(
                "SELECT r.source_id, r.payload, pv.canonical_sku, pv.category, pv.supplier_id "
                "FROM staging.raw_records r "
                "JOIN core.product_versions pv ON pv.canonical_sku = "
                "     upper(regexp_replace(coalesce(r.payload->>'SKU', r.payload->>'product_code', "
                "     r.payload->>'itemId'), '[^A-Za-z0-9]', '', 'g')) "
                "WHERE pv.is_current AND pv.canonical_sku = 'WM001' "
                "ORDER BY r.source_id"
            )
        ).all()

    if not rows:
        return

    table = Table(title="\n  Three dialects, one canonical record", title_justify="left")
    table.add_column("Source")
    table.add_column("As delivered")
    table.add_column("Canonical SKU")
    table.add_column("Canonical category")

    for row in rows:
        payload = row.payload
        delivered = payload.get("SKU") or payload.get("product_code") or payload.get("itemId")
        raw_category = payload.get("Category") or payload.get("category") or payload.get("dept")
        table.add_row(
            render.cell(row.source_id),
            render.cell(f"{delivered}  ({raw_category})"),
            render.cell(row.canonical_sku),
            render.cell(row.category),
        )
    console.print(table)


def show_quarantine(results: dict[str, object], pausing: bool) -> None:
    act(
        3,
        "Bad records are quarantined, not dropped",
        "One bad row does not reject a batch, and nothing disappears silently.",
    )

    run_id = results["acme_supplier"].run_id
    with read_connection() as connection:
        failures = connection.execute(load_sql("run_failures"), {"run_id": run_id}).all()
        sample = connection.execute(
            load_sql("quarantine_detail"), {"run_id": run_id, "limit": 1}
        ).all()

    render.render_failures(failures)

    if sample:
        record = sample[0]
        console.print(
            f"\n  [bold]A rejected record, kept whole[/bold] "
            f"[dim](source line {record.source_line})[/dim]"
        )
        console.print(f"  [dim]{record.raw_payload}[/dim]")
        for failure in record.failures:
            console.print(
                f"  [red]✗[/red] {failure['field'] or '<record>'}: {failure['message']} "
                f"[dim](expected {failure['expected']}, got {failure['actual']})[/dim]"
            )
    note("every rejection keeps the payload, the rule, the expectation and the actual value")
    pause(pausing)


def demonstrate_idempotency(pausing: bool) -> None:
    act(4, "The same file twice", "Re-delivering an identical payload must not duplicate anything.")

    result = run_pipeline("erp_core", trigger="demo")
    console.print(f"  status: {render.status_text(result.status)}")
    note(result.skipped_reason or "no duplicate detected")

    forced = run_pipeline("erp_core", trigger="demo", force=True)
    console.print(
        f"  forced replay: {forced.counters.rows_unchanged} unchanged, "
        f"{forced.counters.rows_inserted} inserted, {forced.counters.rows_updated} updated"
    )
    note("two independent guards: a batch fingerprint, and a per-record content hash")
    note("even forced past the first, the second means identical content opens no version")
    pause(pausing)


def demonstrate_scd(pausing: bool) -> None:
    act(
        5,
        "A second ERP extract arrives",
        "Stock moved on almost every SKU. Only genuine attribute changes should "
        "open a new version.",
    )

    stage_extract(SECOND_EXTRACT)
    result = run_pipeline("erp_core", trigger="demo")

    counters = result.counters
    console.print(
        f"  inserted [green]{counters.rows_inserted}[/green]   "
        f"updated [yellow]{counters.rows_updated}[/yellow]   "
        f"unchanged [cyan]{counters.rows_unchanged}[/cyan]"
    )
    note(
        f"{counters.rows_unchanged} records had different stock levels and produced no new "
        f"version — quantity is a measure, stored as a fact"
    )
    note(f"{counters.rows_updated} records had a tracked attribute change and were versioned")

    with read_connection() as connection:
        changed = (
            connection.execute(
                text(
                    "SELECT canonical_sku FROM core.product_versions "
                    "GROUP BY canonical_sku HAVING count(*) > 1 ORDER BY canonical_sku"
                )
            )
            .scalars()
            .all()
        )

    console.print(f"\n  Versioned SKUs: [bold]{', '.join(changed)}[/bold]\n")
    for sku in changed[:3]:
        with read_connection() as connection:
            history = connection.execute(load_sql("product_history"), {"sku": sku}).all()
        render.render_history(history)
    pause(pausing)
    return changed


def point_in_time(changed: list[str], pausing: bool) -> None:
    act(
        6,
        "Point-in-time and lineage",
        "What was this priced at, then? And where did that number come from?",
    )

    sku = changed[0] if changed else "WM001"
    with read_connection() as connection:
        history = connection.execute(load_sql("product_history"), {"sku": sku}).all()

    if len(history) >= _VERSIONS_FOR_COMPARISON:
        boundary = history[0].effective_to
        moments = [
            (boundary - timedelta(microseconds=1), "just before the change"),
            (boundary, "at the instant of the change"),
            (datetime.now(UTC), "now"),
        ]
        table = Table(title=f"  {sku} through time", title_justify="left")
        table.add_column("As of")
        table.add_column("")
        table.add_column("Version", justify="right")
        table.add_column("Price", justify="right")

        for moment, label in moments:
            with read_connection() as connection:
                row = connection.execute(
                    load_sql("point_in_time_product"), {"sku": sku, "as_of": moment}
                ).one_or_none()
            table.add_row(
                moment.strftime("%Y-%m-%d %H:%M:%S.%f"),
                label,
                str(row.version_number) if row else "—",
                f"{row.unit_price:,.2f} {row.currency}" if row else "did not exist",
            )
        console.print(table)
        note(
            "exactly one version is in effect at any instant — enforced by a GiST "
            "exclusion constraint, not by convention"
        )

    with read_connection() as connection:
        lineage = connection.execute(load_sql("lineage_trace"), {"sku": sku}).all()

    if lineage:
        step = lineage[-1]
        console.print(f"\n  [bold]Lineage for {sku} v{step.version_number}[/bold]")
        for label, value in (
            ("warehouse value", f"{step.unit_price} {step.currency}"),
            (
                "source",
                f"{step.source_id}, record {step.source_record_id!r}, line {step.source_line}",
            ),
            ("source payload", str(step.source_payload)),
            ("run", f"{step.run_id} ({step.run_status})"),
            ("contract", f"v{step.contract_version} [{step.contract_hash[:12]}]"),
            (
                "reconciled",
                f"{step.reconciliation_status}, content checksum {step.content_checksum_status}",
            ),
        ):
            console.print(f"    [dim]{label:18}[/dim] {value}")
        note("every hop is a foreign key — none of this depends on log retention")
    pause(pausing)


def reconciliation(pausing: bool) -> None:
    act(
        7,
        "Reconciliation",
        "Volume asks whether rows went missing. Content asks whether the values "
        "survived. Only the second can detect silent corruption.",
    )

    with read_connection() as connection:
        run_id = connection.execute(
            text(
                "SELECT run_id FROM meta.pipeline_runs WHERE status = 'SUCCEEDED' "
                "ORDER BY started_at DESC LIMIT 1"
            )
        ).scalar_one()
        checks = connection.execute(
            text(
                "SELECT check_name, dimension, check_kind, status, source_value, target_value "
                "FROM dq.reconciliation_results WHERE run_id = :r "
                "ORDER BY check_kind DESC, check_name"
            ),
            {"r": run_id},
        ).all()

    table = Table(title="  Latest run", title_justify="left")
    # The dimension-qualified label ("null_rate_pct[product_name]") is the useful
    # part; let it fold rather than be silently truncated to look like a duplicate.
    table.add_column("Check", overflow="fold")
    for column in ("Kind", "Status", "Source", "Target"):
        table.add_column(column)
    for check in checks:
        label = (
            check.check_name if check.dimension == "*" else f"{check.check_name}[{check.dimension}]"
        )
        table.add_row(
            render.cell(label),
            check.check_kind,
            render.status_text(check.status),
            _abbrev(check.source_value),
            _abbrev(check.target_value),
        )
    console.print(table)

    note(
        "the content checksum is computed twice — in Python over the source, and by "
        "Postgres over the warehouse"
    )
    note(
        "two independent implementations agreeing is evidence; comparing a value with itself is not"
    )

    prove_corruption_detection()
    pause(pausing)


def _volume_verdict(before: int, after: int) -> str:
    if before == after:
        return "identical — volume reconciliation sees nothing"
    return "changed"


def _content_verdict(before: str, after: str) -> str:
    if before != after:
        return "MISMATCH — content reconciliation catches it"
    return "unchanged"


def prove_corruption_detection() -> None:
    """Corrupt one price directly in the warehouse and show what each check sees."""
    contract = ContractRegistry.discover(get_settings().contracts_dir).get("erp_core")
    columns = contract.reconciliation.checksum_columns

    with read_connection() as connection:
        skus = (
            connection.execute(
                text("SELECT canonical_sku FROM core.products ORDER BY canonical_sku LIMIT 20")
            )
            .scalars()
            .all()
        )
        before_count, before_digest = target_checksum(connection, skus, columns)

    with transaction() as connection:
        connection.execute(
            text(
                "UPDATE core.product_versions SET unit_price = unit_price + 1 "
                "WHERE canonical_sku = :s AND is_current"
            ),
            {"s": skus[0]},
        )

    with read_connection() as connection:
        after_count, after_digest = target_checksum(connection, skus, columns)

    console.print(
        Panel(
            f"[bold]A single price is altered directly in the warehouse.[/bold]\n\n"
            f"  row count      before {before_count}   after {after_count}   "
            f"[{'green' if before_count == after_count else 'red'}]"
            f"{_volume_verdict(before_count, after_count)}"
            f"[/]\n"
            f"  content digest before [dim]{before_digest[:24]}…[/dim]\n"
            f"                 after  [dim]{after_digest[:24]}…[/dim]   "
            f"[{'red' if before_digest != after_digest else 'green'}]"
            f"{_content_verdict(before_digest, after_digest)}"
            f"[/]",
            title="Why content reconciliation exists",
            border_style="yellow",
            expand=False,
        )
    )

    # Put it back, so the demo leaves a consistent warehouse behind.
    with transaction() as connection:
        connection.execute(
            text(
                "UPDATE core.product_versions SET unit_price = unit_price - 1 "
                "WHERE canonical_sku = :s AND is_current"
            ),
            {"s": skus[0]},
        )


def quality_report() -> None:
    act(
        8,
        "The data-quality report",
        "Every figure is computed by the pipeline. The model only interprets them — "
        "and any number it invents is rejected.",
    )

    with read_connection() as connection:
        report = connection.execute(
            text("SELECT * FROM dq.quality_reports ORDER BY generated_at DESC LIMIT 1")
        ).one_or_none()

    if report is None:
        console.print("[yellow]No quality report was generated.[/yellow]")
        return

    console.print(
        f"  [dim]status[/dim] {render.status_text(report.status)}   "
        f"[dim]provider[/dim] {report.provider}/{report.model}   "
        f"[dim]prompt[/dim] {report.prompt_version}"
    )
    if report.error_message:
        console.print(f"  [dim]{report.error_message}[/dim]")
    guard = report.numeric_guard
    console.print(
        f"  [dim]numeric guard[/dim] verified={guard.get('verified')} "
        f"checked={guard.get('numbers_checked', 0)} "
        f"unverified={guard.get('unverified', [])}\n"
    )
    console.print(Markdown(report.summary_markdown))


# ---------------------------------------------------------------------------
# Sample-data staging
# ---------------------------------------------------------------------------


def stage_extract(name: str) -> None:
    """Put one archived extract into the landing zone, replacing what is there.

    A landing zone holds *the current extract*: each night's file supersedes the
    last, and the pipeline is a full-refresh consumer that reads whatever it
    finds. Staging is how the demo plays two nights in sequence.
    """
    source = ERP_EXTRACTS / name
    if not source.is_file():
        raise SystemExit(f"{source} is missing. Run: python scripts/generate_sample_data.py")
    ERP_LANDING.mkdir(parents=True, exist_ok=True)
    for stale in ERP_LANDING.glob("*.csv"):
        stale.unlink()
    shutil.copy(source, ERP_LANDING / name)


def _abbrev(value: object, width: int = 18) -> str:
    text_value = "—" if value is None else str(value)
    trimmed = text_value if len(text_value) <= width else f"{text_value[: width - 1]}…"
    return render.cell(trimmed)


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-pause", action="store_true", help="Run straight through.")
    parser.add_argument("--quiet", action="store_true", help="Suppress pipeline logs.")
    arguments = parser.parse_args()

    configure_logging("WARNING" if arguments.quiet else "ERROR", "console")
    logging.getLogger("alembic").setLevel(logging.ERROR)
    pausing = not arguments.no_pause

    console.print(
        Panel(
            "[bold]SupplyLink[/bold]\n"
            "[dim]Multi-source data integration and reconciliation[/dim]\n\n"
            "Three sources describing the same products in three dialects, "
            "reconciled into one canonical, versioned warehouse.",
            border_style="blue",
            expand=False,
        )
    )

    try:
        with vendor_simulator():
            prepare(pausing)
            results = ingest_three_sources(pausing)
            show_quarantine(results, pausing)
            demonstrate_idempotency(pausing)
            changed = demonstrate_scd(pausing)
            point_in_time(changed, pausing)
            reconciliation(pausing)
            quality_report()
    except SupplyLinkError as exc:
        console.print(f"\n[red]{type(exc).__name__}:[/red] {exc}")
        return 1
    finally:
        stage_extract(FIRST_EXTRACT)
        dispose_engine()

    console.print()
    console.print(Rule("[green]Demo complete[/green]", style="green"))
    console.print(
        "\n  Explore further:\n"
        "    [cyan]supplylink runs list[/cyan]\n"
        "    [cyan]supplylink sku history MN001[/cyan]\n"
        "    [cyan]supplylink sku lineage MN001[/cyan]\n"
        "    [cyan]supplylink runs quality[/cyan]\n"
        "    [cyan]uvicorn supplylink.api:app[/cyan]  then open http://localhost:8000/docs\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
