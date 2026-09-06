"""The clock seam and structured logging context."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from supplylink.clock import frozen_clock, utcnow
from supplylink.observability import (
    ConsoleFormatter,
    JsonFormatter,
    get_log_context,
    log_context,
)


class TestClock:
    def test_utcnow_is_timezone_aware(self) -> None:
        assert utcnow().tzinfo is not None

    def test_frozen_clock_pins_time(self) -> None:
        moment = datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
        with frozen_clock(moment):
            assert utcnow() == moment == utcnow()

    def test_the_clock_is_restored_afterwards(self) -> None:
        moment = datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
        with frozen_clock(moment):
            pass
        assert utcnow() != moment

    def test_the_clock_is_restored_after_an_exception(self) -> None:
        moment = datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
        with pytest.raises(RuntimeError), frozen_clock(moment):
            raise RuntimeError("boom")
        assert utcnow() != moment

    def test_a_naive_datetime_is_refused(self) -> None:
        naive = datetime(2026, 3, 14, 9, 30)
        with pytest.raises(ValueError, match="timezone-aware"), frozen_clock(naive):
            pass


class TestLogContext:
    def test_fields_are_bound_and_unbound(self) -> None:
        assert get_log_context() == {}
        with log_context(run_id="abc"):
            assert get_log_context() == {"run_id": "abc"}
        assert get_log_context() == {}

    def test_nested_contexts_merge(self) -> None:
        with log_context(run_id="abc"), log_context(stage="validation"):
            assert get_log_context() == {"run_id": "abc", "stage": "validation"}

    def test_unbinding_is_exception_safe(self) -> None:
        with pytest.raises(RuntimeError), log_context(run_id="abc"):
            raise RuntimeError("boom")
        assert get_log_context() == {}

    def test_an_inner_context_can_override(self) -> None:
        with log_context(stage="a"), log_context(stage="b"):
            assert get_log_context()["stage"] == "b"


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "supplylink.test", logging.INFO, __file__, 1, "something happened", None, None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestFormatters:
    def test_console_puts_context_first(self) -> None:
        line = ConsoleFormatter().format(
            _record(zebra=1, run_id="abc", source_id="erp", stage="validation")
        )
        assert line.index("run_id=") < line.index("source_id=") < line.index("stage=")
        assert line.index("stage=") < line.index("zebra=")

    def test_console_quotes_values_containing_spaces(self) -> None:
        assert 'reason="two words"' in ConsoleFormatter().format(_record(reason="two words"))

    def test_json_emits_one_object_per_line(self) -> None:
        payload = json.loads(JsonFormatter().format(_record(run_id="abc", records=15000)))
        assert payload["message"] == "something happened"
        assert payload["level"] == "INFO"
        assert payload["run_id"] == "abc"
        assert payload["records"] == 15000
        assert payload["logger"] == "supplylink.test"

    def test_json_serialises_non_primitive_extras(self) -> None:
        from uuid import uuid4

        identifier = uuid4()
        payload = json.loads(JsonFormatter().format(_record(run_id=identifier)))
        assert payload["run_id"] == str(identifier)


class TestRichEscaping:
    """Data values must not be parsed as Rich style markup.

    Square brackets in a value — a dimension-qualified check label, or a product
    name like ``USB Hub [refurb]`` — are markup to Rich and would be silently
    swallowed, showing the operator less than the data actually contains.
    """

    def test_square_brackets_survive(self) -> None:
        from supplylink.cli.render import cell

        assert cell("null_rate_pct[product_name]") == r"null_rate_pct\[product_name]"

    def test_a_product_name_with_brackets_survives(self) -> None:
        from rich.console import Console

        from supplylink.cli.render import cell

        console = Console(file=None, width=80, record=True)
        console.print(cell("USB Hub [refurb]"))
        assert "USB Hub [refurb]" in console.export_text()

    def test_none_renders_as_a_dash(self) -> None:
        from supplylink.cli.render import cell

        assert cell(None) == "—"
