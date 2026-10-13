"""OpenAI-compatible chat-completions provider over plain ``httpx``.

One client covers every service that speaks the OpenAI chat API: Groq, Mistral, OpenRouter,
Ollama, GitHub Models, Cerebras, Gemini's OpenAI endpoint and OpenAI itself. Configure it
with ``LLM_API_BASE`` and ``LLM_API_KEY`` (see ``.env.example``). It reuses the same rate
limiter, backoff, daily quota guard and JSON/tool-call conventions as the Gemini client, so
the orchestrator, cache, replay mode and eval harness do not know the difference.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from haulmind.config import Settings
from haulmind.llm.base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    QuotaExhausted,
    Role,
    ToolCallReq,
    Usage,
)
from haulmind.llm.parsing import extract_json
from haulmind.llm.quota import QuotaGuard
from haulmind.llm.ratelimit import AsyncRateLimiter, backoff_delay
from haulmind.logging_setup import get_logger

log = get_logger(__name__)

SchemaMode = Literal["json_schema", "json_object"]

# Groq: "... on requests per day (RPD): Limit 1000, Used 1000 ..."; also "tokens per day (TPD)".
# Gemini's OpenAI endpoint: "... GenerateRequestsPerDayPerProjectPerModel ...".
_DAILY_RE = re.compile(r"per\s*day|daily|\bRPD\b|\bTPD\b|PerDay", re.IGNORECASE)
# Groq: "Please try again in 2m59.56s" / "in 1.5s"; OpenAI: "Please try again in 20s".
_TRY_AGAIN_RE = re.compile(r"try again in\s*((?:\d+(?:\.\d+)?[hms])+)", re.IGNORECASE)
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)([hms])")
_SCHEMA_REJECTED_RE = re.compile(r"response_format|json_schema|structured output", re.IGNORECASE)

_KNOWN_HOSTS = {
    "api.groq.com": "groq",
    "api.mistral.ai": "mistral",
    "openrouter.ai": "openrouter",
    "api.openai.com": "openai",
    "generativelanguage.googleapis.com": "gemini-openai",
    "models.github.ai": "github-models",
    "api.cerebras.ai": "cerebras",
    "api.together.xyz": "together",
}


def provider_label(base_url: str) -> str:
    """Short provider name for traces and eval tables, derived from the API host."""
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host in _KNOWN_HOSTS:
        return _KNOWN_HOSTS[host]
    if host in ("localhost", "127.0.0.1", "::1"):
        return "ollama" if parsed.port == 11434 else "local"
    return "openai-compatible"


def _is_local(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "").lower() in ("localhost", "127.0.0.1", "::1")


def _parse_duration(text: str) -> float:
    total = 0.0
    for num, unit in _DURATION_PART_RE.findall(text):
        total += float(num) * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]
    return total


def _retry_hint(resp: httpx.Response, message: str) -> float | None:
    """Seconds to wait, from a ``Retry-After`` header or a "try again in ..." phrase."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = _TRY_AGAIN_RE.search(message)
    return _parse_duration(m.group(1)) if m else None


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:500]
    if isinstance(err, str):
        return err[:500]
    return resp.text[:500]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def to_chat_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """Map provider-neutral messages onto the OpenAI chat format."""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "tool":
            for tr in m.tool_results:
                out.append(
                    {"role": "tool", "tool_call_id": tr.call_id, "content": _dump(tr.result)}
                )
            continue
        if m.role == "model":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or ""}
            if m.tool_calls:
                # ``tc.extra`` carries provider state returned with the call (for example
                # Gemini's ``extra_content.google.thought_signature``); it is echoed verbatim.
                msg["tool_calls"] = [
                    {
                        **tc.extra,
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": _dump(tc.args)},
                    }
                    for tc in m.tool_calls
                ]
                if not m.text:
                    msg["content"] = None
            out.append(msg)
            continue
        out.append({"role": "user", "content": m.text or ""})
    return out


class OpenAICompatibleClient:
    """``LLMClient`` for any OpenAI-style ``/chat/completions`` endpoint."""

    def __init__(
        self,
        settings: Settings,
        quota: QuotaGuard | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = settings.llm_api_base.rstrip("/")
        if not settings.llm_api_key and not _is_local(base):
            raise LLMError("LLM_API_KEY is not set (required unless LLM_API_BASE is local)")
        self.settings = settings
        self.base_url = base
        self.provider = provider_label(base)
        headers = (
            {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
        )
        self.http = httpx.AsyncClient(
            base_url=base + "/",
            headers=headers,
            timeout=settings.llm_timeout_s,
            transport=transport,
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
        # Providers that reject ``json_schema`` response formats get ``json_object`` plus the
        # schema in the system prompt; remembered per model after the first rejection.
        self.schema_mode: dict[str, SchemaMode] = {}

    def model_for(self, role: Role) -> str:
        return self.models[role]

    # --- request building ----------------------------------------------------------------------

    def build_body(self, req: LLMRequest, model: str) -> dict[str, Any]:
        system = req.system
        body: dict[str, Any] = {
            "model": model,
            "temperature": self.settings.llm_temperature
            if req.temperature is None
            else req.temperature,
            "max_tokens": req.max_output_tokens,
        }
        effort = self._reasoning_effort(model)
        if effort:
            body["reasoning_effort"] = effort
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in req.tools
            ]
            body["tool_choice"] = "auto"
        elif req.response_schema is not None:
            if self.schema_mode.get(model, "json_schema") == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": req.response_schema},
                }
            else:
                body["response_format"] = {"type": "json_object"}
                hint = (
                    "Respond with a single JSON object that matches this JSON schema:\n"
                    + json.dumps(req.response_schema)
                )
                system = f"{system}\n\n{hint}" if system else hint
        body["messages"] = to_chat_messages(system, req.messages)
        return body

    def _reasoning_effort(self, model: str) -> str | None:
        """Explicit ``LLM_REASONING_EFFORT`` wins. gpt-oss models default to ``low`` so hidden
        reasoning does not eat the output budget or a free tier's tokens-per-minute cap."""
        if self.settings.llm_reasoning_effort:
            return self.settings.llm_reasoning_effort
        return "low" if "gpt-oss" in model else None

    # --- generation ----------------------------------------------------------------------------

    async def generate(self, req: LLMRequest) -> LLMResponse:
        model = self.model_for(req.role)
        limiter = self.limiters.setdefault(model, AsyncRateLimiter(5))
        max_retries = self.settings.llm_max_retries
        attempt = 0
        t0 = time.perf_counter()
        while True:
            self.quota.check(model)
            await limiter.acquire()
            body = self.build_body(req, model)
            try:
                resp = await self.http.post("chat/completions", json=body)
            except httpx.HTTPError as e:
                if attempt >= max_retries:
                    raise LLMError(
                        f"{self.provider} request failed after {attempt} retries: {e}"
                    ) from e
                delay = backoff_delay(attempt)
                log.warning(
                    "llm request failed, retrying",
                    provider=self.provider,
                    model=model,
                    attempt=attempt,
                    error=str(e)[:200],
                    delay=round(delay, 1),
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if resp.status_code == 200:
                self.requests += 1
                self.quota.record(model)
                out = self._parse(resp.json(), req, model)
                out.latency_ms = int((time.perf_counter() - t0) * 1000)
                out.retries = attempt
                return out

            message = _error_message(resp)
            status = resp.status_code
            if status == 429:
                if _DAILY_RE.search(message):
                    log.warning(
                        "daily quota exhausted; pinning model for today",
                        provider=self.provider,
                        model=model,
                        detail=message[:300],
                    )
                    self.quota.mark_exhausted(model)
                    raise QuotaExhausted(model, message[:200])
                if attempt >= max_retries:
                    raise LLMError(f"rate limited after {attempt} retries: {message[:200]}")
                delay = backoff_delay(attempt, hint=_retry_hint(resp, message))
                limiter.penalize(delay)
                log.warning(
                    "llm 429, backing off",
                    provider=self.provider,
                    model=model,
                    attempt=attempt,
                    delay=round(delay, 1),
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            if status >= 500:
                if attempt >= max_retries:
                    raise LLMError(f"{self.provider} error {status} after retries: {message[:200]}")
                delay = backoff_delay(attempt)
                log.warning(
                    "llm 5xx, retrying",
                    provider=self.provider,
                    model=model,
                    status=status,
                    attempt=attempt,
                    delay=round(delay, 1),
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            if (
                status == 400
                and req.response_schema is not None
                and not req.tools
                and self.schema_mode.get(model, "json_schema") == "json_schema"
                and _SCHEMA_REJECTED_RE.search(message)
            ):
                log.info(
                    "provider rejected json_schema response format; using json_object",
                    provider=self.provider,
                    model=model,
                    detail=message[:200],
                )
                self.schema_mode[model] = "json_object"
                continue
            raise LLMError(f"{self.provider} error {status}: {message[:300]}")

    def _parse(self, data: dict[str, Any], req: LLMRequest, model: str) -> LLMResponse:
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some providers return content parts
            text = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        else:
            text = content if isinstance(content, str) else ""
        calls: list[ToolCallReq] = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            args = (
                raw
                if isinstance(raw, dict)
                else extract_json(raw)
                if isinstance(raw, str)
                else None
            )
            calls.append(
                ToolCallReq(
                    id=str(tc.get("id") or f"call_{i}"),
                    name=str(fn.get("name") or ""),
                    args=args if isinstance(args, dict) else {},
                    extra={k: v for k, v in tc.items() if k not in ("id", "type", "function")},
                )
            )
        usage_raw = data.get("usage") or {}
        details = usage_raw.get("completion_tokens_details") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("prompt_tokens") or 0),
            output_tokens=int(usage_raw.get("completion_tokens") or 0),
            thought_tokens=int(details.get("reasoning_tokens") or 0),
        )
        parsed = (
            extract_json(text)
            if (req.response_schema is not None or text.lstrip().startswith(("{", "[")))
            else None
        )
        return LLMResponse(
            text=text,
            parsed=parsed,
            tool_calls=calls,
            usage=usage,
            model=str(data.get("model") or model),
            provider=self.provider,
        )

    async def list_models(self) -> list[str]:
        resp = await self.http.get("models")
        if resp.status_code != 200:
            raise LLMError(f"{self.provider} error {resp.status_code}: {_error_message(resp)}")
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        return sorted(
            str(m.get("id")).removeprefix("models/")
            for m in items or []
            if isinstance(m, dict) and m.get("id")
        )

    async def check_models(self) -> None:
        """Fail fast when a configured model name is not served by this provider.

        A wrong name otherwise surfaces minutes later as a 404 on the first incident. When the
        provider cannot list models the check is skipped with a warning.
        """
        try:
            names = await self.list_models()
        except (LLMError, httpx.HTTPError) as e:
            log.warning(
                "could not list models; skipping model-name check",
                provider=self.provider,
                error=str(e)[:200],
            )
            return
        if not names:
            return
        missing = sorted({m for m in self.models.values() if m not in names})
        if missing:
            sample = ", ".join(names[:12]) + (", ..." if len(names) > 12 else "")
            raise LLMError(
                f"{self.provider} does not serve {', '.join(missing)}; set LLM_MODEL_TRIAGE / "
                f"LLM_MODEL_PLANNER in .env to models it lists (`make models`): {sample}"
            )

    async def aclose(self) -> None:
        await self.http.aclose()
