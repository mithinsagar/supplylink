"""CLI behaviour.

Exit codes get their own tests because they are an interface: a cron job or a CI
step branches on them, and a pipeline that returns 0 whatever happened is not
operable.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from supplylink.cli.main import EXIT_FAILED, EXIT_OK, EXIT_RECONCILIATION, app
from supplylink.pipeline import run_pipeline
from tests.integration.feeds import GOOD_ROWS, rows_with

pytestmark = pytest.mark.integration

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


class TestBasics:
    def test_version(self) -> None:
        result = invoke("version")
        assert result.exit_code == EXIT_OK
        assert "supplylink" in result.stdout

    def test_health_reports_ok(self, db: None) -> None:
        result = invoke("health")
        assert result.exit_code == EXIT_OK
        assert "PASS" in result.stdout

    def test_health_json(self, db: None) -> None:
        result = invoke("health", "--json")
        assert json.loads(result.stdout)["status"] == "ok"


class TestContracts:
    def test_list(self, db: None) -> None:
        """Content is asserted through --json; the table is asserted to render.

        Rich truncates cells to fit the terminal, so asserting on a substring of
        a table couples the test to a window width.
        """
        result = invoke("contracts", "list")
        assert result.exit_code == EXIT_OK
        assert "Data contracts" in result.stdout

        sources = {
            entry["source_id"] for entry in json.loads(invoke("contracts", "list", "--json").stdout)
        }
        assert sources == {"erp_core", "acme_supplier", "globalparts_api"}

    def test_list_json_includes_the_content_hash(self, db: None) -> None:
        entries = json.loads(invoke("contracts", "list", "--json").stdout)
        assert all(len(entry["content_hash"]) == 64 for entry in entries)

    def test_validate_passes_for_the_shipped_contracts(self, db: None) -> None:
        result = invoke("contracts", "validate")
        assert result.exit_code == EXIT_OK
        assert "3 contracts valid" in result.stdout

    def test_sync_registers_versions(self, db: None) -> None:
        assert invoke("contracts", "sync").exit_code == EXIT_OK

    def test_transforms_are_listed(self) -> None:
        assert "sku_normalise" in invoke("contracts", "transforms").stdout


class TestRunCommand:
    def test_a_clean_run_exits_zero(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = invoke("run", "--source", "erp_core")
        assert result.exit_code == EXIT_OK

    def test_a_failing_reconciliation_exits_two(self, erp_seeded, write_erp_feed) -> None:
        """So a scheduler can distinguish 'loaded but suspect' from 'loaded cleanly'."""
        write_erp_feed(rows_with(Supplier="SUP-999"))
        result = invoke("run", "--source", "erp_core")
        assert result.exit_code in {EXIT_OK, EXIT_RECONCILIATION}

    def test_an_unreadable_source_exits_one(self, erp_seeded, isolated_data_dir) -> None:
        result = invoke("run", "--source", "erp_core")
        assert result.exit_code == EXIT_FAILED
        assert "IngestionError" in result.stderr

    def test_an_unknown_source_exits_one(self, db: None) -> None:
        result = invoke("run", "--source", "nope_source")
        assert result.exit_code == EXIT_FAILED

    def test_json_output_is_machine_readable(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        payload = json.loads(invoke("run", "--source", "erp_core", "--json").stdout)
        assert payload["counters"]["records_received"] == 3
        assert payload["reconciliation"]["status"] in {"PASS", "WARN", "FAIL"}

    def test_a_duplicate_payload_is_reported_as_skipped(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        invoke("run", "--source", "erp_core")
        payload = json.loads(invoke("run", "--source", "erp_core", "--json").stdout)
        assert payload["status"] == "SKIPPED"

    def test_no_llm_skips_the_report(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        payload = json.loads(invoke("run", "--source", "erp_core", "--no-llm", "--json").stdout)
        assert payload["quality_report_status"] is None


class TestValidateCommand:
    def test_dry_run_writes_nothing(self, erp_seeded, write_erp_feed) -> None:
        from sqlalchemy import text

        from supplylink.db import read_connection

        write_erp_feed(rows_with(Stock="-12"))
        payload = json.loads(invoke("validate", "--source", "erp_core", "--json").stdout)

        assert payload["dry_run"] is True
        assert payload["quarantined"] == 1

        with read_connection() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM meta.pipeline_runs")).scalar_one()
                == 0
            )

    def test_human_output_lists_failing_rules(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(rows_with(Stock="-12"))
        assert "min" in invoke("validate", "--source", "erp_core").stdout


class TestInspection:
    @pytest.fixture
    def loaded(self, erp_seeded, write_erp_feed):
        write_erp_feed(rows_with(Stock="-12"))
        return run_pipeline("erp_core", trigger="test")

    def test_runs_list(self, loaded) -> None:
        result = invoke("runs", "list")
        assert result.exit_code == EXIT_OK
        assert "Pipeline runs" in result.stdout

        runs = json.loads(invoke("runs", "list", "--json").stdout)
        assert [run["source_id"] for run in runs] == ["erp_core"]

    def test_runs_show(self, loaded) -> None:
        output = invoke("runs", "show", str(loaded.run_id)).stdout
        assert "Stage timings" in output
        assert "Reconciliation" in output

    def test_runs_failures(self, loaded) -> None:
        assert "min" in invoke("runs", "failures", str(loaded.run_id)).stdout

    def test_runs_quarantine_shows_the_payload(self, loaded) -> None:
        assert "-12" in invoke("runs", "quarantine", str(loaded.run_id)).stdout

    def test_runs_quality(self, loaded) -> None:
        output = invoke("runs", "quality", str(loaded.run_id)).stdout
        assert "FALLBACK" in output

    def test_a_bad_run_id_exits_one(self, db: None) -> None:
        result = invoke("runs", "failures", "not-a-uuid")
        assert result.exit_code == EXIT_FAILED
        assert "Not a valid run id" in result.stderr

    def test_sku_show(self, loaded) -> None:
        assert "KB002" in invoke("sku", "show", "KB002").stdout

    def test_sku_history(self, loaded) -> None:
        assert "Version history" in invoke("sku", "history", "KB002").stdout

    def test_sku_lineage_reaches_the_source(self, loaded) -> None:
        output = invoke("sku", "lineage", "KB002").stdout
        assert "KB-002" in output
        assert "contract" in output

    def test_sku_as_of(self, loaded) -> None:
        from datetime import UTC, datetime

        moment = datetime.now(UTC).isoformat()
        assert "KB002" in invoke("sku", "as-of", "KB002", moment).stdout

    def test_a_bad_timestamp_exits_one(self, loaded) -> None:
        result = invoke("sku", "as-of", "KB002", "not-a-time")
        assert result.exit_code == EXIT_FAILED
        assert "ISO 8601" in result.stderr

    def test_an_unknown_sku_exits_one(self, loaded) -> None:
        assert invoke("sku", "show", "ZZZ999").exit_code == EXIT_FAILED


class TestSeed:
    def test_seeding_is_idempotent(self, db: None) -> None:
        first = invoke("db", "seed")
        second = invoke("db", "seed")
        assert first.exit_code == EXIT_OK
        assert second.exit_code == EXIT_OK
        assert "10 suppliers written" in second.stdout
