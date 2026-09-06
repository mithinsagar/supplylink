"""Provider selection from configuration."""

from __future__ import annotations

from supplylink.config import get_settings
from supplylink.llm.openai_compatible import OpenAICompatibleProvider
from supplylink.llm.provider import LLMProvider, NullProvider
from supplylink.observability import get_logger

log = get_logger(__name__)


def build_provider() -> LLMProvider:
    """The configured provider, or a :class:`NullProvider` with the reason why.

    Never raises. Configuration problems degrade the quality report; they do not
    stop a data pipeline.
    """
    settings = get_settings()

    if not settings.llm_enabled:
        return NullProvider("LLM generation is disabled (SUPPLYLINK_LLM_ENABLED=false)")
    if settings.llm_provider == "null":
        return NullProvider("LLM provider is set to 'null'")
    if settings.llm_api_key is None:
        return NullProvider(
            "no API key configured — set SUPPLYLINK_LLM_API_KEY to enable generated reports"
        )

    try:
        return OpenAICompatibleProvider(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
            max_attempts=settings.llm_max_attempts,
        )
    except Exception as exc:  # configuration must never fail a run
        log.warning("LLM provider unavailable", extra={"error": str(exc)})
        return NullProvider(f"provider could not be constructed: {exc}")
