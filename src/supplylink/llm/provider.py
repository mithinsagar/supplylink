"""The LLM provider interface.

SupplyLink's use of an LLM is narrow by design: it turns metrics the pipeline
already computed into prose. It never computes anything, and the pipeline never
depends on it — a provider outage degrades the quality report to a deterministic
template and changes nothing else. See ADR-005.

The interface is deliberately small. Everything a caller needs is "given these
messages, return text and tell me what it cost".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A completion, plus the metadata worth auditing."""

    text: str
    model: str
    provider: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMUnavailableError(RuntimeError):
    """The provider could not be reached or refused the request.

    Deliberately *not* a :class:`~supplylink.errors.PipelineError`: an LLM
    failure must never fail a data pipeline. It is caught at the quality-report
    boundary and turned into a fallback report.
    """


class LLMProvider(ABC):
    """Generates text from a system prompt and a user message."""

    name: str = "abstract"

    @abstractmethod
    def complete(self, system: str, user: str) -> LLMResponse:
        """Return a completion, or raise :class:`LLMUnavailableError`."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The model this provider will use, for the audit record."""

    def close(self) -> None:
        """Release any transport resources. Providers without any need not override."""
        return


class NullProvider(LLMProvider):
    """A provider that always declines.

    Used when no key is configured, when ``SUPPLYLINK_LLM_ENABLED=false``, and in
    tests. It makes "no LLM" an ordinary, exercised code path rather than a
    special case that only executes in production when something breaks.
    """

    name = "null"

    def __init__(self, reason: str = "no LLM provider is configured") -> None:
        self._reason = reason

    @property
    def model(self) -> str:
        return "none"

    def complete(self, system: str, user: str) -> LLMResponse:  # noqa: ARG002
        raise LLMUnavailableError(self._reason)
