"""Gemini provider via the official google-genai SDK, with native function calling and
schema-constrained JSON output, plus rate limiting, backoff and the daily quota guard."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from haulmind.config import Settings
from haulmind.llm.base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    QuotaExhausted,
    Role,
    ToolCallReq,
    ToolSpec,
    Usage,
)
from haulmind.llm.quota import QuotaGuard
from haulmind.llm.ratelimit import AsyncRateLimiter, backoff_delay
from haulmind.logging_setup import get_logger

log = get_logger(__name__)

_RETRY_DELAY_RE = re.compile(r"retry(?:Delay|\s+in)\D*?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_DAILY_RE = re.compile(r"per\s*day|daily|PerDay|RequestsPerDay", re.IGNORECASE)


def _strip_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove JSON-schema keywords Gemini's function-declaration parser rejects."""
    drop = {"title", "$schema", "additionalProperties", "default", "$defs", "definitions"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in drop:
            continue
        if isinstance(v, dict):
            out[k] = _strip_schema(v)
        elif isinstance(v, list):
            out[k] = [_strip_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def _to_contents(messages: list[Message]) -> list[types.Content]:
    contents: list[types.Content] = []
    for m in messages:
        parts: list[types.Part] = []
        if m.role == "tool":
            for tr in m.tool_results:
                result = tr.result if isinstance(tr.result, dict) else {"result": tr.result}
                parts.append(types.Part.from_function_response(name=tr.name, response=result))
            contents.append(types.Content(role="user", parts=parts))
            continue
        if m.text:
            parts.append(types.Part.from_text(text=m.text))
        for tc in m.tool_calls:
            parts.append(types.Part(function_call=types.FunctionCall(name=tc.name, args=tc.args)))
        contents.append(types.Content(role="user" if m.role == "user" else "model", parts=parts))
    return contents


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            return None
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
    return None


class GeminiClient:
    provider = "gemini"

    def __init__(self, settings: Settings, quota: QuotaGuard | None = None) -> None:
        if not settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self.settings = settings
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000)),
        )
        self.models: dict[Role, str] = {
            "triage": settings.llm_model_triage,
            "planner": settings.llm_model_planner,
        }
        self.limiters: dict[str, AsyncRateLimiter] = {}
        for role, rpm in (
            ("triage", settings.llm_rpm_triage),
            ("planner", settings.llm_rpm_planner),
        ):
            model = self.models[role]  # type: ignore[index]
            self.limiters.setdefault(model, AsyncRateLimiter(rpm))
        self.quota = quota or QuotaGuard(
            settings.quota_file,
            {
                settings.llm_model_triage: settings.llm_rpd_triage,
                settings.llm_model_planner: settings.llm_rpd_planner,
            },
        )
        self.requests = 0

    def model_for(self, role: Role) -> str:
        return self.models[role]

    # --- request building ----------------------------------------------------------------------

    def _config(self, req: LLMRequest) -> types.GenerateContentConfig:
        cfg: dict[str, Any] = {
            "temperature": self.settings.llm_temperature
            if req.temperature is None
            else req.temperature,
            "max_output_tokens": req.max_output_tokens,
        }
        if req.system:
            cfg["system_instruction"] = req.system
        if req.tools:
            cfg["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=_strip_schema(t.parameters),
                        )
                        for t in req.tools
                    ]
                )
            ]
            cfg["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            )
            cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        elif req.response_schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = req.response_schema
        if self.settings.llm_thinking_budget is not None:
            cfg["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.settings.llm_thinking_budget
            )
        return types.GenerateContentConfig(**cfg)

    # --- generation ----------------------------------------------------------------------------

    async def generate(self, req: LLMRequest) -> LLMResponse:
        model = self.model_for(req.role)
        limiter = self.limiters.setdefault(model, AsyncRateLimiter(5))
        attempt = 0
        t0 = time.perf_counter()
        while True:
            self.quota.check(model)
            await limiter.acquire()
            try:
                resp = await self.client.aio.models.generate_content(
                    model=model, contents=_to_contents(req.messages), config=self._config(req)
                )
                self.requests += 1
                self.quota.record(model)
                out = self._parse(resp, req, model)
                out.latency_ms = int((time.perf_counter() - t0) * 1000)
                out.retries = attempt
                return out
            except genai_errors.ClientError as e:
                code = getattr(e, "code", None)
                msg = str(e)
                if code == 429 or "RESOURCE_EXHAUSTED" in msg:
                    if _DAILY_RE.search(msg):
                        self.quota.mark_exhausted(model)
                        raise QuotaExhausted(model, msg[:200]) from e
                    if attempt >= self.settings.llm_max_retries:
                        raise LLMError(f"rate limited after {attempt} retries: {msg[:200]}") from e
                    m = _RETRY_DELAY_RE.search(msg)
                    delay = backoff_delay(attempt, hint=float(m.group(1)) if m else None)
                    limiter.penalize(delay)
                    log.warning(
                        "gemini 429, backing off",
                        model=model,
                        attempt=attempt,
                        delay=round(delay, 1),
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise LLMError(f"gemini client error {code}: {msg[:300]}") from e
            except genai_errors.ServerError as e:
                if attempt >= self.settings.llm_max_retries:
                    raise LLMError(f"gemini server error after retries: {str(e)[:200]}") from e
                delay = backoff_delay(attempt)
                log.warning(
                    "gemini 5xx, retrying", model=model, attempt=attempt, delay=round(delay, 1)
                )
                await asyncio.sleep(delay)
                attempt += 1

    def _parse(
        self, resp: types.GenerateContentResponse, req: LLMRequest, model: str
    ) -> LLMResponse:
        text_parts: list[str] = []
        calls: list[ToolCallReq] = []
        cand = resp.candidates[0] if resp.candidates else None
        if cand is not None and cand.content is not None and cand.content.parts:
            for i, part in enumerate(cand.content.parts):
                if part.function_call is not None and part.function_call.name:
                    calls.append(
                        ToolCallReq(
                            id=part.function_call.id or f"call_{i}",
                            name=part.function_call.name,
                            args=dict(part.function_call.args or {}),
                        )
                    )
                elif part.text and not part.thought:
                    text_parts.append(part.text)
        text = "".join(text_parts)
        usage = Usage()
        um = resp.usage_metadata
        if um is not None:
            usage = Usage(
                input_tokens=int(um.prompt_token_count or 0),
                output_tokens=int(um.candidates_token_count or 0),
                thought_tokens=int(um.thoughts_token_count or 0),
            )
        parsed = (
            _extract_json(text)
            if (req.response_schema is not None or text.lstrip().startswith(("{", "[")))
            else None
        )
        return LLMResponse(
            text=text,
            parsed=parsed,
            tool_calls=calls,
            usage=usage,
            model=resp.model_version or model,
            provider=self.provider,
        )

    async def list_models(self) -> list[str]:
        names: list[str] = []
        async for m in await self.client.aio.models.list():
            if m.name:
                names.append(m.name.removeprefix("models/"))
        return sorted(names)


def tool_specs_from_mcp(tools: list[dict[str, Any]]) -> list[ToolSpec]:
    """Convert MCP ``list_tools`` entries into provider-neutral ``ToolSpec``s."""
    return [
        ToolSpec(
            name=t["name"],
            description=t.get("description") or "",
            parameters=t.get("input_schema") or {"type": "object", "properties": {}},
        )
        for t in tools
    ]
