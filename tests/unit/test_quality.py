"""The numeric guard, the fallback report, and the LLM failure path.

Between them these are the reason a generated narrative can be trusted: the
guard makes hallucinated figures detectable, and the fallback means a provider
outage degrades the report rather than the pipeline.
"""

from __future__ import annotations

import pytest

from supplylink.llm.provider import LLMProvider, LLMResponse, LLMUnavailableError, NullProvider
from supplylink.quality import fallback
from supplylink.quality.verifier import collect_allowed_numbers, verify

METRICS = {
    "run": {
        "run_id": "abc",
        "source_id": "acme_supplier",
        "source_name": "ACME Components Ltd",
        "contract_version": "1.1.0",
        "started_at": "2026-03-14T09:30:00+00:00",
        "trigger": "cli",
        "source_type": "csv",
    },
    "volumes": {
        "records_received": 14820,
        "records_valid": 13991,
        "records_quarantined": 829,
        "quarantine_rate_pct": 5.59,
        "rows_inserted": 9821,
        "rows_updated": 4170,
        "rows_unchanged": 0,
        "rows_superseded": 0,
    },
    "top_failures": [
        {
            "rank": 1,
            "rule": "min",
            "field": "quantity_available",
            "failure_count": 611,
            "affected_records": 611,
            "share_of_failures_pct": 66.34,
            "explanation": "negative stock",
        },
        {
            "rank": 2,
            "rule": "references",
            "field": "supplier_id",
            "failure_count": 218,
            "affected_records": 218,
            "share_of_failures_pct": 23.67,
            "explanation": "unknown supplier",
        },
    ],
    "warnings": [],
    "reconciliation": {
        "overall_status": "WARN",
        "checks_run": 10,
        "checks_passing": 8,
        "checks": [
            {
                "name": "content_checksum",
                "kind": "content",
                "status": "PASS",
                "source_value": "abc123",
                "target_value": "abc123",
                "delta_pct": None,
            }
        ],
    },
    "comparison": {"available": False, "note": "no previous successful run"},
}


class TestNumericGuard:
    def test_a_faithful_report_passes(self) -> None:
        text = (
            "## Summary\n"
            "ACME submitted 14,820 records; 13,991 passed and 829 were quarantined "
            "(5.59%).\n\n"
            "## What failed\n"
            "1. Negative inventory: 611 failures, 66.34% of the total.\n"
            "2. Unknown supplier references: 218 failures.\n"
        )
        result = verify(text, METRICS)
        assert result.verified
        assert result.numbers_checked >= 6

    def test_invented_figures_are_caught(self) -> None:
        text = "829 were quarantined, a 42% rise on last week's 584 rejections."
        result = verify(text, METRICS)
        assert not result.verified
        assert set(result.unverified) == {"42", "584"}

    def test_markdown_list_markers_are_not_treated_as_claims(self) -> None:
        """Formatting is not a metric. The alternative — allowing 'small numbers' —
        would punch a hole exactly where a wrong small count could hide."""
        assert verify("1. first\n2. second\n3. third\n", METRICS).verified

    def test_a_wrong_small_count_is_still_caught(self) -> None:
        assert not verify("Only 7 records were rejected.", METRICS).verified

    def test_rounding_is_permitted(self) -> None:
        """12.34 may legitimately be written as 12.3 or 12."""
        assert verify("The quarantine rate was 5.6%.", METRICS).verified
        assert verify("The quarantine rate was about 6%.", METRICS).verified

    def test_thousands_separators_are_understood(self) -> None:
        assert verify("14,820 records arrived.", METRICS).verified

    def test_code_fences_are_ignored(self) -> None:
        assert verify('```json\n{"invented": 99999}\n```\n', METRICS).verified

    def test_prose_without_numbers_always_passes(self) -> None:
        result = verify("Data quality degraded materially this week.", METRICS)
        assert result.verified
        assert result.numbers_checked == 0

    def test_nested_metrics_are_available(self) -> None:
        allowed = collect_allowed_numbers(METRICS)
        assert any(str(value) == "66.34" for value in allowed)

    def test_identifiers_do_not_whitelist_invented_figures(self) -> None:
        """The hole that a UUID run id would otherwise open.

        A run id is a UUID and a checksum is 64 hex characters. If digits were
        harvested from those, almost any two- or three-digit number a model
        invented would find a match somewhere in the payload and be waved
        through — which is how a guard passes while guarding nothing.
        """
        payload = {
            "run": {"run_id": "8d21c0ff-4718-4a29-9184-000000918000"},
            "reconciliation": {"checksum": "a" * 40 + "4718" + "b" * 20},
            "volumes": {"records_received": 3},
        }
        result = verify("The feed dropped 47% to 918 records.", payload)
        assert not result.verified
        assert set(result.unverified) == {"47", "918"}

    def test_identifiers_in_the_text_are_not_treated_as_claims(self) -> None:
        """Symmetrically: a SKU or a checksum quoted in prose is not a figure."""
        payload = {"volumes": {"records_received": 3}}
        assert verify("Records for WM001 and KB002: 3 in total.", payload).verified

    def test_a_quoted_identifier_does_not_hide_a_real_claim(self) -> None:
        payload = {"volumes": {"records_received": 3}}
        assert not verify("SKU WM001 accounts for 47 rejections.", payload).verified

    def test_a_contract_version_may_be_quoted(self) -> None:
        payload = {"run": {"contract_version": "1.3.0"}, "volumes": {"records_received": 3}}
        assert verify("Run under contract version 1.3.0 processed 3 records.", payload).verified

    def test_a_timestamp_may_be_quoted(self) -> None:
        payload = {"run": {"started_at": "2026-03-14T09:30:00+00:00"}}
        assert verify("The run started on 2026-03-14.", payload).verified

    def test_guard_result_serialises_for_storage(self) -> None:
        payload = verify("999999 records", METRICS).as_dict()
        assert payload["verified"] is False
        assert "999999" in payload["unverified"]


class TestFallbackReport:
    def test_renders_every_section(self) -> None:
        report = fallback.render(METRICS, "no API key configured")
        for heading in (
            "## Summary",
            "## What failed",
            "## Reconciliation",
            "## Compared with the previous run",
        ):
            assert heading in report

    def test_states_why_it_was_used(self) -> None:
        assert "no API key configured" in fallback.render(METRICS, "no API key configured")

    def test_contains_only_computed_figures(self) -> None:
        """The fallback is never less accurate than a generated report."""
        assert verify(fallback.render(METRICS, "reason"), METRICS).verified

    def test_a_guard_rejection_reason_carries_no_figures(self) -> None:
        """Otherwise the deterministic report would fail its own guard.

        The reason is rendered into the report, so *any* number in it — the
        invented figures, or even how many there were — would be an unverifiable
        figure printed in the very document written to replace one.
        """
        reason = (
            "the numeric guard rejected the generated report: it contained figures "
            "that do not correspond to any computed metric (see numeric_guard.unverified)"
        )
        assert verify(fallback.render(METRICS, reason), METRICS).verified

    def test_handles_a_run_with_no_failures(self) -> None:
        clean = {**METRICS, "top_failures": []}
        assert "No records were rejected." in fallback.render(clean, "reason")

    def test_handles_a_missing_previous_run(self) -> None:
        assert "no previous successful run" in fallback.render(METRICS, "reason")

    def test_abbreviates_long_values(self) -> None:
        """A 64-character checksum would destroy the table layout."""
        long_metrics = {
            **METRICS,
            "reconciliation": {
                **METRICS["reconciliation"],
                "checks": [
                    {
                        "name": "content_checksum",
                        "kind": "content",
                        "status": "PASS",
                        "source_value": "a" * 64,
                        "target_value": "a" * 64,
                        "delta_pct": None,
                    }
                ],
            },
        }
        assert "a" * 64 not in fallback.render(long_metrics, "reason")


class _AlwaysFails(LLMProvider):
    name = "boom"

    @property
    def model(self) -> str:
        return "boom-1"

    def complete(self, system: str, user: str) -> LLMResponse:
        raise LLMUnavailableError("provider is down")


class _Hallucinates(LLMProvider):
    name = "stub"

    @property
    def model(self) -> str:
        return "stub-1"

    def complete(self, system: str, user: str) -> LLMResponse:
        return LLMResponse(
            text="A total of 999999 records were processed.",
            model="stub-1",
            provider="stub",
        )


class TestProviders:
    def test_null_provider_declines_with_a_reason(self) -> None:
        with pytest.raises(LLMUnavailableError, match="not configured"):
            NullProvider("not configured").complete("s", "u")

    def test_provider_failure_is_not_a_pipeline_error(self) -> None:
        """LLMUnavailableError deliberately does not inherit from SupplyLinkError."""
        from supplylink.errors import SupplyLinkError

        assert not issubclass(LLMUnavailableError, SupplyLinkError)

    def test_factory_returns_null_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from supplylink.config import reset_settings_cache
        from supplylink.llm import build_provider

        monkeypatch.setenv("SUPPLYLINK_LLM_ENABLED", "false")
        reset_settings_cache()
        try:
            provider = build_provider()
            assert isinstance(provider, NullProvider)
        finally:
            reset_settings_cache()

    def test_factory_returns_null_without_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from supplylink.config import reset_settings_cache
        from supplylink.llm import build_provider

        monkeypatch.setenv("SUPPLYLINK_LLM_ENABLED", "true")
        monkeypatch.delenv("SUPPLYLINK_LLM_API_KEY", raising=False)
        reset_settings_cache()
        try:
            assert isinstance(build_provider(), NullProvider)
        finally:
            reset_settings_cache()


def test_stub_providers_are_usable_by_the_report_layer() -> None:
    """Sanity check on the seams the integration tests drive."""
    assert _AlwaysFails().name == "boom"
    assert _Hallucinates().complete("s", "u").text.startswith("A total")
