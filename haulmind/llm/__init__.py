"""LLM access behind one ``LLMClient`` interface: Gemini (google-genai) or an offline stub,
wrapped with a disk cache, an async rate limiter with backoff, and a daily quota guard."""

from haulmind.llm.base import (
    CacheMiss,
    LLMClient,
    LLMRequest,
    LLMResponse,
    Message,
    QuotaExhausted,
    ToolCallReq,
    ToolResult,
    ToolSpec,
    Usage,
)
from haulmind.llm.factory import build_llm_client, get_llm_client, reset_llm_client

__all__ = [
    "CacheMiss",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "QuotaExhausted",
    "ToolCallReq",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "build_llm_client",
    "get_llm_client",
    "reset_llm_client",
]
