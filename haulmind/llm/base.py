"""Provider-agnostic request/response models and the ``LLMClient`` protocol."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Role = Literal["triage", "planner"]


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolCallReq(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    # Opaque provider state that must be echoed back with the call on the next turn (for
    # example Gemini's thought signatures on its OpenAI-compatible endpoint). Never inspected.
    extra: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    name: str
    result: Any


class Message(BaseModel):
    role: Literal["user", "model", "tool"]
    text: str | None = None
    tool_calls: list[ToolCallReq] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)


class LLMRequest(BaseModel):
    role: Role = "planner"
    system: str = ""
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    temperature: float | None = None
    max_output_tokens: int = 1024
    task: str = ""
    # Structured payload for the offline stub and for tracing. Never sent to a provider and
    # excluded from the cache key.
    meta: dict[str, Any] = Field(default_factory=dict)

    def cache_payload(self, model: str) -> dict[str, Any]:
        return {
            "model": model,
            "system": self.system,
            "messages": [m.model_dump(mode="json") for m in self.messages],
            "tools": [t.model_dump(mode="json") for t in self.tools],
            "response_schema": self.response_schema,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thought_tokens


class LLMResponse(BaseModel):
    text: str = ""
    parsed: Any = None
    tool_calls: list[ToolCallReq] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str
    provider: str
    cached: bool = False
    latency_ms: int = 0
    retries: int = 0


class LLMClient(Protocol):
    provider: str

    def model_for(self, role: Role) -> str: ...

    async def generate(self, req: LLMRequest) -> LLMResponse: ...


class QuotaExhausted(Exception):
    """Daily request budget for a model is used up. Callers checkpoint and stop gracefully."""

    def __init__(self, model: str, detail: str = "") -> None:
        super().__init__(f"daily quota exhausted for {model}: {detail}")
        self.model = model


class CacheMiss(Exception):
    """Raised in replay mode when a prompt is not in the disk cache."""


class LLMError(Exception):
    """Non-retryable provider error."""
