"""Quality report generation, verification and persistence.

The behaviour that matters: a generated report is stored with the exact metrics
it was given, an unverifiable figure rejects the report rather than shipping it,
and no failure of any kind can fail the run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from supplylink.db import read_connection
from supplylink.llm.provider import LLMProvider, LLMResponse, LLMUnavailableError
from supplylink.pipeline import run_pipeline
from tests.integration.feeds import GOOD_ROWS

pytestmark = pytest.mark.integration


class _Faithful(LLMProvider):
    """Writes a report using only figures that appear in the payload."""

    name = "stub"

    @property
    def model(self) -> str:
        return "stub-faithful"

    def complete(self, system: str, user: str) -> LLMResponse:
        return LLMResponse(
            text=(
                "## Summary\nThe run processed 3 records and quarantined 0.\n\n"
                "## Recommended actions\nNothing requires attention.\n"
            ),
            model="stub-faithful",
            provider="stub",
            prompt_tokens=100,
            completion_tokens=40,
            latency_ms=250,
        )


class _Hallucinating(LLMProvider):
    name = "stub"

    @property
    def model(self) -> str:
        return "stub-hallucinating"

    def complete(self, system: str, user: str) -> LLMResponse:
        return LLMResponse(
            text="The run processed 3 records, a 47% drop from last week's 918.",
            model="stub-hallucinating",
            provider="stub",
        )


class _Unavailable(LLMProvider):
    name = "stub"

    @property
    def model(self) -> str:
        return "stub-down"

    def complete(self, system: str, user: str) -> LLMResponse:
        raise LLMUnavailableError("provider returned 503")


class _Exploding(LLMProvider):
    """Fails in a way the provider layer does not anticipate."""

    name = "stub"

    @property
    def model(self) -> str:
        return "stub-explode"

    def complete(self, system: str, user: str) -> LLMResponse:
        raise RuntimeError("something entirely unexpected")


def run_with(provider: LLMProvider | None) -> object:
    return run_pipeline("erp_core", trigger="test", llm_provider=provider)


def stored_report() -> object:
    with read_connection() as connection:
        return connection.execute(text("SELECT * FROM dq.quality_reports")).one()


class TestGeneration:
    def test_a_faithful_report_is_stored_as_generated(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = run_with(_Faithful())

        assert result.quality_report["status"] == "GENERATED"
        report = stored_report()
        assert report.status == "GENERATED"
        assert report.model == "stub-faithful"
        assert report.prompt_tokens == 100
        assert report.numeric_guard["verified"] is True

    def test_the_exact_metrics_sent_are_stored(self, erp_seeded, write_erp_feed) -> None:
        """So any number in the narrative can be traced back months later."""
        write_erp_feed(GOOD_ROWS)
        run_with(_Faithful())

        metrics = stored_report().input_metrics
        assert metrics["volumes"]["records_received"] == 3
        assert "reconciliation" in metrics
        assert metrics["run"]["contract_version"]

    def test_no_source_data_is_sent_to_the_model(self, erp_seeded, write_erp_feed) -> None:
        """Counts, rates and rule names — never product records or supplier names."""
        import json

        write_erp_feed(GOOD_ROWS)
        run_with(_Faithful())

        payload = json.dumps(stored_report().input_metrics)
        for leaked in ("Wireless Mouse", "Mechanical Keyboard", "27 Inch QHD Monitor"):
            assert leaked not in payload

    def test_the_prompt_version_is_recorded(self, erp_seeded, write_erp_feed) -> None:
        """Wording changes output; without a version, two reports are indistinguishable."""
        from supplylink.quality import PROMPT_VERSION

        write_erp_feed(GOOD_ROWS)
        run_with(_Faithful())
        assert stored_report().prompt_version == PROMPT_VERSION


class TestNumericGuard:
    def test_an_invented_figure_rejects_the_report(self, erp_seeded, write_erp_feed) -> None:
        """An untrustworthy number in a confident sentence is worse than no report."""
        write_erp_feed(GOOD_ROWS)
        result = run_with(_Hallucinating())

        assert result.quality_report["status"] == "FALLBACK"
        report = stored_report()
        assert report.status == "FALLBACK"
        assert report.numeric_guard["verified"] is False
        assert set(report.numeric_guard["unverified"]) == {"47", "918"}

    def test_the_rejected_draft_is_kept_for_inspection(self, erp_seeded, write_erp_feed) -> None:
        """A recurring hallucination should be diagnosable, not invisible."""
        write_erp_feed(GOOD_ROWS)
        run_with(_Hallucinating())

        assert "47%" in stored_report().numeric_guard["rejected_draft"]

    def test_the_stored_report_is_the_deterministic_one(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        run_with(_Hallucinating())

        markdown = stored_report().summary_markdown
        assert "47%" not in markdown
        assert "## Summary" in markdown


class TestFallback:
    def test_a_provider_outage_does_not_fail_the_run(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = run_with(_Unavailable())

        assert result.status == "SUCCEEDED"
        assert result.quality_report["status"] == "FALLBACK"
        assert "503" in stored_report().error_message

    def test_an_unexpected_provider_error_does_not_fail_the_run(
        self, erp_seeded, write_erp_feed
    ) -> None:
        """Anything at all from a provider is contained."""
        write_erp_feed(GOOD_ROWS)
        result = run_with(_Exploding())

        assert result.status == "SUCCEEDED"
        assert result.quality_report["status"] == "FALLBACK"
        assert "RuntimeError" in stored_report().error_message

    def test_no_key_configured_produces_a_fallback(self, erp_seeded, write_erp_feed) -> None:
        """The default path in any environment without an API key — including CI."""
        write_erp_feed(GOOD_ROWS)
        result = run_with(None)

        assert result.quality_report["status"] == "FALLBACK"
        assert stored_report().provider == "null"

    def test_the_warehouse_is_fully_loaded_regardless(self, erp_seeded, write_erp_feed) -> None:
        """The report is narration of committed work, not part of it."""
        write_erp_feed(GOOD_ROWS)
        run_with(_Unavailable())

        with read_connection() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM core.product_versions")).scalar_one()
                == 3
            )

    def test_skipping_the_report_entirely_is_recorded(self, erp_seeded, write_erp_feed) -> None:
        write_erp_feed(GOOD_ROWS)
        result = run_pipeline("erp_core", trigger="test", skip_quality_report=True)

        assert result.quality_report is None
        with read_connection() as connection:
            stage = connection.execute(
                text(
                    "SELECT status FROM meta.run_stages "
                    "WHERE run_id = :r AND stage = 'quality_report'"
                ),
                {"r": result.run_id},
            ).scalar_one()
        assert stage == "SKIPPED"


def test_regenerating_replaces_rather_than_duplicates(erp_seeded, write_erp_feed) -> None:
    """One report per run, enforced by a unique constraint."""
    from supplylink.config import get_settings
    from supplylink.contracts.loader import ContractRegistry
    from supplylink.db import transaction
    from supplylink.quality.report import generate_quality_report

    write_erp_feed(GOOD_ROWS)
    result = run_with(None)
    contract = ContractRegistry.discover(get_settings().contracts_dir).get("erp_core")

    with transaction() as connection:
        generate_quality_report(
            connection,
            result.run_id,
            contract,
            reconciliation=result.reconciliation,
            provider=_Faithful(),
        )

    with read_connection() as connection:
        rows = connection.execute(text("SELECT status FROM dq.quality_reports")).scalars().all()
    assert rows == ["GENERATED"]
