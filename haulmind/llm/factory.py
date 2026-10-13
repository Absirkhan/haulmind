"""Build the configured LLM client stack."""

from __future__ import annotations

from haulmind.config import Settings, get_settings
from haulmind.llm.base import LLMClient
from haulmind.llm.cache import CachingClient, DiskCache
from haulmind.llm.stub import StubLLMClient
from haulmind.logging_setup import get_logger

log = get_logger(__name__)
_client: LLMClient | None = None


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    provider = s.effective_llm_provider
    if provider == "gemini":
        from haulmind.llm.gemini import GeminiClient

        inner: LLMClient = GeminiClient(s)
        s.ensure_dirs()
        client: LLMClient = CachingClient(inner, DiskCache(s.llm_cache_dir), mode=s.llm_cache_mode)
        log.info(
            "llm provider: gemini",
            triage=s.llm_model_triage,
            planner=s.llm_model_planner,
            cache_mode=s.llm_cache_mode,
        )
        return client
    if provider == "openai":
        from haulmind.llm.openai_compat import OpenAICompatibleClient

        compat = OpenAICompatibleClient(s)
        s.ensure_dirs()
        wrapped: LLMClient = CachingClient(
            compat, DiskCache(s.llm_cache_dir), mode=s.llm_cache_mode
        )
        if compat.provider != "gemini-openai" and (
            s.llm_model_triage.startswith("gemini-") or s.llm_model_planner.startswith("gemini-")
        ):
            log.warning(
                "model names look like Gemini models; set LLM_MODEL_TRIAGE / LLM_MODEL_PLANNER "
                "to names your provider serves (`make models` lists them)",
                triage=s.llm_model_triage,
                planner=s.llm_model_planner,
            )
        log.info(
            f"llm provider: {compat.provider} (openai-compatible)",
            base=s.llm_api_base,
            triage=s.llm_model_triage,
            planner=s.llm_model_planner,
            cache_mode=s.llm_cache_mode,
        )
        return wrapped
    log.info("llm provider: offline stub (set GEMINI_API_KEY or LLM_API_KEY to use a real model)")
    return StubLLMClient()


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = build_llm_client()
    return _client


def set_llm_client(client: LLMClient | None) -> None:
    """Testing hook."""
    global _client
    _client = client


def reset_llm_client() -> None:
    global _client
    _client = None


async def verify_llm_models(client: LLMClient) -> None:
    """Ask the provider (through any cache wrapper) to confirm the configured model names.

    The stub and Gemini are no-ops; the OpenAI-compatible client raises ``LLMError`` naming the
    models it does serve, so a wrong name fails at startup instead of on the first incident.
    """
    inner = getattr(client, "inner", client)
    check = getattr(inner, "check_models", None)
    if check is not None:
        await check()
