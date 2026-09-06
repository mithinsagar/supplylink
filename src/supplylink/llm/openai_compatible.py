"""OpenAI-compatible chat-completions provider.

Covers xAI (Grok) and OpenAI itself, which share a wire format:
``POST {base_url}/chat/completions`` with a bearer token and a ``messages``
array. One implementation, two defaults.

Retries mirror the REST ingestion adapter's policy — transient statuses and
network faults only, exponential backoff with full jitter — because the reasoning
is identical: a 401 will not fix itself, and synchronised retries make a
struggling service worse.

The request is pinned to a fixed temperature (0 by default) so the same metrics
produce a stable narrative. A quality report that reads differently on every run
over identical data trains people to ignore it.
"""

from __future__ import annotations

import random
import time
from typing import Any, Final

import httpx

from supplylink.llm.provider import LLMProvider, LLMResponse, LLMUnavailableError
from supplylink.observability import get_logger

log = get_logger(__name__)

#: Default endpoints. Overridable via SUPPLYLINK_LLM_BASE_URL for proxies,
#: gateways and locally-hosted models.
DEFAULT_BASE_URLS: Final[dict[str, str]] = {
    "xai": "https://api.x.ai/v1",
    "openai": "https://api.openai.com/v1",
}

_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class OpenAICompatibleProvider(LLMProvider):
    """Chat completions over the OpenAI-compatible API shape."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 900,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self.name = provider
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._timeout = timeout_seconds
        self._base_url = (base_url or DEFAULT_BASE_URLS.get(provider, "")).rstrip("/")

        if not self._base_url:
            raise LLMUnavailableError(f"no base URL known for provider {provider!r}")

        # Credentials are attached per request rather than baked into the
        # client, so an injected client (tests, a shared connection pool) cannot
        # silently end up unauthenticated.
        self._auth_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=httpx.Timeout(timeout_seconds)
        )

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(self, system: str, user: str) -> LLMResponse:
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        started = time.monotonic()
        response = self._post_with_retries(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        return self._parse(response, latency_ms)

    # -- transport --------------------------------------------------------

    def _post_with_retries(self, payload: dict[str, Any]) -> httpx.Response:
        last_error = "unknown"

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post(
                    "/chat/completions", json=payload, headers=self._auth_headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.is_error:
                        raise LLMUnavailableError(
                            f"{self.name} returned HTTP {response.status_code}: "
                            f"{_safe_body(response)}"
                        )
                    return response
                last_error = f"HTTP {response.status_code}"

            if attempt >= self._max_attempts:
                break

            delay = random.uniform(0, min(0.5 * 2 ** (attempt - 1), 8.0))  # noqa: S311
            log.warning(
                "retrying LLM request",
                extra={
                    "provider": self.name,
                    "attempt": attempt,
                    "max_attempts": self._max_attempts,
                    "reason": last_error,
                    "stage": "quality_report",
                },
            )
            time.sleep(delay)

        raise LLMUnavailableError(
            f"{self.name} unavailable after {self._max_attempts} attempts: {last_error}"
        )

    def _parse(self, response: httpx.Response, latency_ms: int) -> LLMResponse:
        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailableError(
                f"{self.name} returned an unexpected response shape: {exc}"
            ) from exc

        if not isinstance(text, str) or not text.strip():
            raise LLMUnavailableError(f"{self.name} returned an empty completion")

        usage = body.get("usage") or {}
        return LLMResponse(
            text=text.strip(),
            model=str(body.get("model", self._model)),
            provider=self.name,
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
            latency_ms=latency_ms,
            raw={"finish_reason": choice.get("finish_reason"), "usage": usage},
        )


def _safe_body(response: httpx.Response) -> str:
    """A bounded excerpt of an error body, safe to log.

    Provider error pages can be large, and the response may echo request content.
    Only a short prefix is kept.
    """
    return response.text[:300].replace("\n", " ")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
