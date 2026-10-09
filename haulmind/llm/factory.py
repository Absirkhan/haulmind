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
    log.info("llm provider: offline stub (set GEMINI_API_KEY to use Gemini)")
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
