"""The HTTP LLM provider: retries, error containment, and secret hygiene."""

from __future__ import annotations

import httpx
import pytest

from supplylink.llm.openai_compatible import DEFAULT_BASE_URLS, OpenAICompatibleProvider
from supplylink.llm.provider import LLMUnavailableError


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("supplylink.llm.openai_compatible.time.sleep", lambda _s: None)


def provider(handler, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider="xai",
        api_key="secret-key-value",
        model="grok-4-fast",
        max_attempts=3,
        client=httpx.Client(
            base_url=DEFAULT_BASE_URLS["xai"], transport=httpx.MockTransport(handler)
        ),
        **kwargs,
    )


def completion(text: str = "A summary.") -> dict[str, object]:
    return {
        "model": "grok-4-fast",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 45},
    }


class TestSuccess:
    def test_returns_the_completion_and_its_usage(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=completion())

        result = provider(handler).complete("s", "u")

        assert result.text == "A summary."
        assert result.model == "grok-4-fast"
        assert result.provider == "xai"
        assert result.prompt_tokens == 120
        assert result.completion_tokens == 45
        assert result.latency_ms is not None

    def test_temperature_is_pinned_for_reproducibility(self) -> None:
        """A report that reads differently on identical data trains people to ignore it."""
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json=completion())

        provider(handler, temperature=0.0).complete("system text", "user text")

        assert captured["temperature"] == 0.0
        assert captured["messages"][0] == {"role": "system", "content": "system text"}
        assert captured["messages"][1] == {"role": "user", "content": "user text"}

    def test_the_api_key_is_sent_as_a_bearer_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json=completion())

        provider(handler).complete("s", "u")
        assert seen["auth"] == "Bearer secret-key-value"


class TestFailureHandling:
    def test_a_transient_status_is_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return (
                httpx.Response(429)
                if attempts["n"] == 1
                else httpx.Response(200, json=completion())
            )

        assert provider(handler).complete("s", "u").text == "A summary."
        assert attempts["n"] == 2

    def test_an_authentication_failure_is_not_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(401, json={"error": "invalid key"})

        with pytest.raises(LLMUnavailableError, match="HTTP 401"):
            provider(handler).complete("s", "u")
        assert attempts["n"] == 1

    def test_retries_are_bounded(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(503)

        with pytest.raises(LLMUnavailableError, match="after 3 attempts"):
            provider(handler).complete("s", "u")
        assert attempts["n"] == 3

    def test_a_timeout_is_retried_then_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        with pytest.raises(LLMUnavailableError, match="ReadTimeout"):
            provider(handler).complete("s", "u")

    def test_a_malformed_response_is_contained(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        with pytest.raises(LLMUnavailableError, match="unexpected response shape"):
            provider(handler).complete("s", "u")

    def test_a_non_json_response_is_contained(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>gateway</html>")

        with pytest.raises(LLMUnavailableError):
            provider(handler).complete("s", "u")

    def test_an_empty_completion_is_rejected(self) -> None:
        """An empty report is worse than a deterministic one."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=completion("   "))

        with pytest.raises(LLMUnavailableError, match="empty completion"):
            provider(handler).complete("s", "u")

    def test_error_bodies_are_truncated(self) -> None:
        """A provider error page can be a megabyte, and may echo request content."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="x" * 10_000)

        with pytest.raises(LLMUnavailableError) as caught:
            provider(handler).complete("s", "u")
        assert len(str(caught.value)) < 600


class TestConfiguration:
    def test_an_unknown_provider_without_a_base_url_is_refused(self) -> None:
        with pytest.raises(LLMUnavailableError, match="no base URL"):
            OpenAICompatibleProvider(provider="mystery", api_key="k", model="m")

    def test_a_base_url_override_is_honoured(self) -> None:
        instance = OpenAICompatibleProvider(
            provider="xai", api_key="k", model="m", base_url="http://proxy.local/v1/"
        )
        try:
            assert instance._base_url == "http://proxy.local/v1"
        finally:
            instance.close()
