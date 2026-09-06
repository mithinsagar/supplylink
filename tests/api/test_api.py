"""API behaviour: contracts, status codes, and error sanitisation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from supplylink.api import app
from supplylink.pipeline import run_pipeline
from tests.integration.feeds import GOOD_ROWS, rows_with

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def loaded(erp_seeded, write_erp_feed):
    """A completed run with one quarantined record, so failure endpoints have data."""
    write_erp_feed(rows_with(Stock="-12"))
    return run_pipeline("erp_core", trigger="test")


class TestSystem:
    def test_health_reports_every_component(self, client: TestClient, db: None) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["database"]["ok"] is True
        assert body["contracts"]["ok"] is True
        assert body["llm_configured"] is False

    def test_the_canonical_model_is_published(self, client: TestClient) -> None:
        fields = {field["name"]: field for field in client.get("/model").json()}
        assert fields["canonical_sku"]["role"] == "key"
        assert fields["quantity_available"]["role"] == "measure"
        assert fields["unit_price"]["type"] == "decimal"

    def test_openapi_document_is_generated(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"] == "SupplyLink"
        assert "/runs/{run_id}/reconciliation" in schema["paths"]


class TestSources:
    def test_lists_sources_in_precedence_order(self, client: TestClient, loaded) -> None:
        sources = client.get("/sources").json()
        precedences = [source["precedence"] for source in sources]
        assert precedences == sorted(precedences)
        assert any(source["source_id"] == "erp_core" for source in sources)

    def test_reports_the_last_run(self, client: TestClient, loaded) -> None:
        erp = next(s for s in client.get("/sources").json() if s["source_id"] == "erp_core")
        assert erp["last_run_status"] == "SUCCEEDED"
        assert erp["contract_version"]


class TestRuns:
    def test_lists_runs(self, client: TestClient, loaded) -> None:
        runs = client.get("/runs").json()
        assert len(runs) == 1
        assert runs[0]["records_received"] == 3
        assert runs[0]["records_quarantined"] == 1

    def test_filters_by_source(self, client: TestClient, loaded) -> None:
        assert client.get("/runs", params={"source_id": "erp_core"}).json()
        assert client.get("/runs", params={"source_id": "no_such"}).json() == []

    def test_run_detail_includes_stage_timings(self, client: TestClient, loaded) -> None:
        body = client.get(f"/runs/{loaded.run_id}").json()
        assert body["run_id"] == str(loaded.run_id)
        assert set(body["stage_timings"]) >= {"ingestion", "validation", "canonical_load"}

    def test_failures_are_ranked(self, client: TestClient, loaded) -> None:
        failures = client.get(f"/runs/{loaded.run_id}/failures").json()
        assert failures
        counts = [failure["failure_count"] for failure in failures]
        assert counts == sorted(counts, reverse=True)

    def test_quarantine_returns_the_original_payload(self, client: TestClient, loaded) -> None:
        records = client.get(f"/runs/{loaded.run_id}/quarantine").json()
        assert records[0]["raw_payload"]["Stock"] == "-12"
        assert records[0]["failures"]

    def test_reconciliation_labels_check_kinds(self, client: TestClient, loaded) -> None:
        body = client.get(f"/runs/{loaded.run_id}/reconciliation").json()
        kinds = {check["check_kind"] for check in body["checks"]}
        assert kinds == {"volume", "content"}
        assert body["overall_status"] in {"PASS", "WARN", "FAIL"}

    def test_quality_report_exposes_its_inputs(self, client: TestClient, loaded) -> None:
        body = client.get(f"/runs/{loaded.run_id}/quality").json()
        assert body["status"] == "FALLBACK"
        assert body["input_metrics"]["volumes"]["records_received"] == 3
        assert body["numeric_guard"]["verified"] is True

    def test_latest_quality_report(self, client: TestClient, loaded) -> None:
        assert client.get("/quality/latest").json()["run_id"] == str(loaded.run_id)

    def test_a_run_is_fetched_directly_not_by_scanning(
        self, client: TestClient, erp_seeded, write_erp_feed
    ) -> None:
        """Looking one run up must filter in SQL.

        Fetching a page of recent runs and picking the match in Python works
        until a source has more runs than the page holds, at which point a
        perfectly valid id starts returning 404.
        """
        from sqlalchemy import text

        from supplylink.db import read_connection

        write_erp_feed(GOOD_ROWS)
        target = run_pipeline("erp_core", trigger="test")
        for _ in range(3):
            run_pipeline("erp_core", trigger="test", force=True)

        with read_connection() as connection:
            total = connection.execute(text("SELECT count(*) FROM meta.pipeline_runs")).scalar_one()
        assert total > 1, "need several runs for this to mean anything"

        body = client.get(f"/runs/{target.run_id}").json()
        assert body["run_id"] == str(target.run_id)

    def test_unknown_run_is_404(self, client: TestClient, db: None) -> None:
        response = client.get("/runs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    def test_malformed_run_id_is_422(self, client: TestClient, db: None) -> None:
        assert client.get("/runs/not-a-uuid").status_code == 422


class TestTriggeringRuns:
    def test_a_run_can_be_triggered(self, client: TestClient, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        response = client.post("/runs", json={"source_id": "erp_core"})

        assert response.status_code == 201
        body = response.json()
        assert body["records_received"] == 3
        assert body["rows_inserted"] == 3

    def test_a_duplicate_payload_is_409(
        self, client: TestClient, erp_seeded, write_erp_feed
    ) -> None:
        write_erp_feed(GOOD_ROWS)
        client.post("/runs", json={"source_id": "erp_core"})
        assert client.post("/runs", json={"source_id": "erp_core"}).status_code == 409

    def test_force_overrides_the_duplicate_guard(
        self, client: TestClient, erp_seeded, write_erp_feed
    ) -> None:
        write_erp_feed(GOOD_ROWS)
        client.post("/runs", json={"source_id": "erp_core"})
        response = client.post("/runs", json={"source_id": "erp_core", "force": True})
        assert response.status_code == 201
        assert response.json()["rows_unchanged"] == 3

    def test_an_unknown_source_is_404(self, client: TestClient, db: None) -> None:
        assert client.post("/runs", json={"source_id": "no_such_source"}).status_code == 404

    def test_an_unreadable_source_is_502(
        self, client: TestClient, erp_seeded, isolated_data_dir
    ) -> None:
        """A source problem is an upstream problem, not a client error."""
        assert client.post("/runs", json={"source_id": "erp_core"}).status_code == 502

    def test_an_unknown_body_field_is_rejected(self, client: TestClient, db: None) -> None:
        response = client.post("/runs", json={"source_id": "erp_core", "typo": True})
        assert response.status_code == 422

    def test_a_malformed_source_id_is_rejected(self, client: TestClient, db: None) -> None:
        assert client.post("/runs", json={"source_id": "Bad Source!"}).status_code == 422


class TestProducts:
    def test_current_state(self, client: TestClient, loaded) -> None:
        body = client.get("/skus/KB002").json()
        assert body["canonical_sku"] == "KB002"
        assert body["is_current"] is True

    def test_lookup_is_case_insensitive(self, client: TestClient, loaded) -> None:
        assert client.get("/skus/kb002").json()["canonical_sku"] == "KB002"

    def test_history_reports_changed_attributes(
        self, client: TestClient, erp_seeded, write_erp_feed
    ) -> None:
        write_erp_feed(GOOD_ROWS)
        run_pipeline("erp_core", trigger="test")
        write_erp_feed(rows_with(Price="899.00"))
        run_pipeline("erp_core", trigger="test")

        history = client.get("/skus/WM001/history").json()
        assert len(history) == 2
        assert history[1]["changed_attributes"] == ["unit_price"]
        assert float(history[1]["price_change_pct"]) == pytest.approx(12.52, abs=0.01)

    def test_point_in_time_lookup(self, client: TestClient, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run_pipeline("erp_core", trigger="test")
        write_erp_feed(rows_with(Price="899.00"))
        run_pipeline("erp_core", trigger="test")

        history = client.get("/skus/WM001/history").json()
        boundary = datetime.fromisoformat(history[0]["effective_to"])

        # Back-to-back runs sit microseconds apart, so step just inside the
        # first version's window rather than a whole second before it.
        before = client.get(
            "/skus/WM001/as-of",
            params={"at": (boundary - timedelta(microseconds=1)).isoformat()},
        ).json()
        after = client.get("/skus/WM001/as-of", params={"at": boundary.isoformat()}).json()

        assert float(before["unit_price"]) == 799.0
        assert float(after["unit_price"]) == 899.0

    def test_point_in_time_before_existence_is_404(self, client: TestClient, loaded) -> None:
        past = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        assert client.get("/skus/KB002/as-of", params={"at": past}).status_code == 404

    def test_lineage_reaches_the_source_payload(self, client: TestClient, loaded) -> None:
        steps = client.get("/skus/KB002/lineage").json()
        assert steps[0]["source_payload"]["SKU"] == "KB-002"
        assert steps[0]["contract_version"]
        assert steps[0]["source_line"] == 1

    def test_unknown_sku_is_404(self, client: TestClient, db: None) -> None:
        assert client.get("/skus/ZZZ999").status_code == 404

    def test_a_malformed_sku_is_422(self, client: TestClient, db: None) -> None:
        assert client.get("/skus/bad-sku!").status_code == 422


class TestErrorSanitisation:
    def test_a_database_failure_does_not_leak_internals(
        self, client: TestClient, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A driver exception stringifies to the statement, its parameters and the URL."""
        from sqlalchemy.exc import OperationalError

        def explode(*_args: object, **_kwargs: object) -> None:
            raise OperationalError(
                "SELECT * FROM secret", {"password": "hunter2"}, Exception("boom")
            )

        monkeypatch.setattr("supplylink.api.routes.load_sql", explode)
        response = client.get("/runs")

        assert response.status_code == 503
        body = response.text
        assert "hunter2" not in body
        assert "SELECT" not in body
        assert response.json() == {
            "error": "DatabaseError",
            "detail": "the warehouse is unavailable",
        }
