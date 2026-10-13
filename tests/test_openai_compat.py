"""OpenAI-compatible provider: request mapping, parsing, retries, quota handling and the
JSON-schema fallback, all against an in-memory ``httpx.MockTransport`` (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from haulmind.config import Settings
from haulmind.llm import openai_compat
from haulmind.llm.base import (
    LLMError,
    LLMRequest,
    Message,
    QuotaExhausted,
    ToolCallReq,
    ToolResult,
    ToolSpec,
)
from haulmind.llm.openai_compat import (
    OpenAICompatibleClient,
    _retry_hint,
    provider_label,
    to_chat_messages,
)
from haulmind.llm.quota import QuotaGuard

Handler = Callable[[httpx.Request], httpx.Response]
SCHEMA = {"type": "object", "properties": {"decision": {"type": "string"}}}


def _settings(tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_api_key": "test-key",
        "llm_api_base": "https://mock.test/v1",
        "llm_model_triage": "small-model",
        "llm_model_planner": "big-model",
        "llm_rpm_triage": 600,
        "llm_rpm_planner": 600,
        "llm_max_retries": 2,
        "data_dir": tmp_path,
        "gemini_api_key": None,
    }
    base.update(over)
    return Settings(**base)


def _client(tmp_path: Path, handler: Handler, **over: Any) -> OpenAICompatibleClient:
    s = _settings(tmp_path, **over)
    quota = QuotaGuard(
        tmp_path / "quota.json",
        {s.llm_model_triage: s.llm_rpd_triage, s.llm_model_planner: s.llm_rpd_planner},
    )
    return OpenAICompatibleClient(s, quota=quota, transport=httpx.MockTransport(handler))


def _ok(
    content: Any = None,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "big-model-2026",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-x",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        },
    )


def _err(status: int, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"message": message, "type": "x"}}, headers=headers or {}
    )


def _user(text: str = "hi", **kw: Any) -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", text=text)], **kw)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compat, "backoff_delay", lambda *a, **k: 0.0)


async def test_json_schema_request_mapping_and_parse(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        return _ok('{"decision": "act", "reason": "x"}')

    c = _client(tmp_path, handler)
    req = _user(
        role="planner",
        system="be terse",
        response_schema=SCHEMA,
        temperature=0.3,
        max_output_tokens=77,
        task="plan",
    )
    resp = await c.generate(req)
    body = seen[0]
    assert body["model"] == "big-model"
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 77
    assert body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "tools" not in body
    assert resp.parsed == {"decision": "act", "reason": "x"}
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.thought_tokens) == (
        120,
        30,
        5,
    )
    assert resp.model == "big-model-2026"
    assert resp.provider == "openai-compatible"
    assert resp.retries == 0
    assert c.quota.used("big-model") == 1


async def test_tool_calls_round_trip(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return _ok(
                None,
                tool_calls=[
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_vehicle_status",
                            "arguments": '{"vehicle_id": "V-07"}',
                        },
                    }
                ],
            )
        return _ok("Coolant is rising.")

    c = _client(tmp_path, handler)
    spec = ToolSpec(
        name="get_vehicle_status",
        description="status",
        parameters={
            "type": "object",
            "properties": {"vehicle_id": {"type": "string"}},
            "required": ["vehicle_id"],
        },
    )
    req = _user("check V-07", role="planner", tools=[spec], task="investigate")
    first = await c.generate(req)
    assert first.tool_calls == [
        ToolCallReq(id="call_abc", name="get_vehicle_status", args={"vehicle_id": "V-07"})
    ]
    assert first.text == ""
    assert seen[0]["tools"][0]["function"]["name"] == "get_vehicle_status"
    assert seen[0]["tool_choice"] == "auto"
    assert "response_format" not in seen[0]

    follow = req.model_copy(deep=True)
    follow.messages.append(Message(role="model", tool_calls=first.tool_calls))
    follow.messages.append(
        Message(
            role="tool",
            tool_results=[
                ToolResult(call_id="call_abc", name="get_vehicle_status", result={"coolant_f": 231})
            ],
        )
    )
    second = await c.generate(follow)
    assert second.text == "Coolant is rising."
    assert second.tool_calls == []
    msgs = seen[1]["messages"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] is None
    assert msgs[1]["tool_calls"][0]["function"]["arguments"] == '{"vehicle_id": "V-07"}'
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_abc", "content": '{"coolant_f": 231}'}


def test_to_chat_messages_keeps_assistant_text_with_tool_calls() -> None:
    out = to_chat_messages(
        "",
        [Message(role="model", text="Looking.", tool_calls=[ToolCallReq(id="c1", name="t")])],
    )
    assert out == [
        {
            "role": "assistant",
            "content": "Looking.",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ],
        }
    ]


async def test_daily_429_pins_quota_and_stops(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _err(
            429,
            "Rate limit reached for model `big-model` on requests per day (RPD): "
            "Limit 1000, Used 1000, Requested 1. Please try again in 55m3.2s.",
        )

    c = _client(tmp_path, handler)
    with pytest.raises(QuotaExhausted) as ei:
        await c.generate(_user(role="planner"))
    assert "RPD" in str(ei.value)
    assert c.quota.remaining("big-model") == 0
    with pytest.raises(QuotaExhausted):  # the guard now refuses before any request
        await c.generate(_user(role="planner"))
    assert calls["n"] == 1


async def test_per_minute_429_is_retried(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _err(
                429,
                "Rate limit reached on requests per minute (RPM): Limit 30, Used 30. "
                "Please try again in 1.5s.",
                headers={"retry-after": "2"},
            )
        return _ok('{"ok": true}')

    c = _client(tmp_path, handler)
    resp = await c.generate(_user(role="triage", response_schema={"type": "object"}))
    assert resp.retries == 1
    assert calls["n"] == 2
    assert resp.parsed == {"ok": True}
    assert c.quota.used("small-model") == 1


async def test_429_gives_up_after_max_retries(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _err(429, "Rate limit reached on requests per minute (RPM). Try again in 1s.")

    c = _client(tmp_path, handler)
    with pytest.raises(LLMError, match="rate limited after 2 retries"):
        await c.generate(_user())


async def test_5xx_is_retried_then_succeeds(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _err(503, "overloaded") if calls["n"] == 1 else _ok("fine")

    c = _client(tmp_path, handler)
    resp = await c.generate(_user())
    assert resp.text == "fine"
    assert resp.retries == 1


async def test_5xx_gives_up_after_max_retries(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _err(502, "bad gateway")

    c = _client(tmp_path, handler)
    with pytest.raises(LLMError, match="error 502 after retries"):
        await c.generate(_user())


async def test_json_schema_rejected_falls_back_to_json_object(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["response_format"]["type"] == "json_schema":
            return _err(400, "response_format value `json_schema` is not supported for this model")
        return _ok('{"decision": "watch"}')

    c = _client(tmp_path, handler)
    req = _user(role="triage", system="sys", response_schema=SCHEMA)
    resp = await c.generate(req)
    assert resp.parsed == {"decision": "watch"}
    assert resp.retries == 0
    assert [b["response_format"]["type"] for b in seen] == ["json_schema", "json_object"]
    system = seen[1]["messages"][0]["content"]
    assert system.startswith("sys")
    assert "matches this JSON schema" in system
    assert json.dumps(SCHEMA) in system
    await c.generate(req)  # remembered per model: no second probe
    assert len(seen) == 3
    assert seen[2]["response_format"]["type"] == "json_object"


async def test_other_4xx_is_not_retried(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _err(401, "invalid api key")

    c = _client(tmp_path, handler)
    with pytest.raises(LLMError, match="error 401"):
        await c.generate(_user())
    assert calls["n"] == 1
    assert c.quota.used("big-model") == 0


async def test_content_parts_and_fenced_json_are_parsed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([{"type": "text", "text": '```json\n{"a": 1}\n```'}])

    c = _client(tmp_path, handler)
    resp = await c.generate(_user(response_schema={"type": "object"}))
    assert resp.parsed == {"a": 1}


async def test_list_models(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200, json={"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "llama-3.1-8b-instant"}]}
        )

    c = _client(tmp_path, handler)
    assert await c.list_models() == ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]


def test_retry_hint_prefers_header_then_phrase() -> None:
    with_header = httpx.Response(429, headers={"retry-after": "7"})
    assert _retry_hint(with_header, "Please try again in 1s.") == 7.0
    bare = httpx.Response(429)
    assert _retry_hint(bare, "Please try again in 2m59.56s.") == pytest.approx(179.56)
    assert _retry_hint(bare, "nothing useful") is None


def test_provider_label() -> None:
    assert provider_label("https://api.groq.com/openai/v1") == "groq"
    assert provider_label("https://api.mistral.ai/v1") == "mistral"
    assert provider_label("https://openrouter.ai/api/v1") == "openrouter"
    assert provider_label("http://127.0.0.1:11434/v1") == "ollama"
    assert provider_label("http://localhost:8080/v1") == "local"
    assert provider_label("https://mock.test/v1") == "openai-compatible"


def test_key_required_unless_local(tmp_path: Path) -> None:
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        OpenAICompatibleClient(_settings(tmp_path, llm_api_key=None))
    local = OpenAICompatibleClient(
        _settings(tmp_path, llm_api_key=None, llm_api_base="http://127.0.0.1:11434/v1")
    )
    assert local.provider == "ollama"
    assert "authorization" not in local.http.headers


def test_auto_provider_resolution(tmp_path: Path) -> None:
    common: dict[str, Any] = {"llm_provider": "auto", "data_dir": tmp_path}
    assert (
        Settings(gemini_api_key=None, llm_api_key=None, **common).effective_llm_provider == "stub"
    )
    assert (
        Settings(gemini_api_key=None, llm_api_key="k", **common).effective_llm_provider == "openai"
    )
    assert (
        Settings(gemini_api_key="g", llm_api_key="k", **common).effective_llm_provider == "gemini"
    )
    assert (
        Settings(llm_provider="openai", llm_api_key="k", data_dir=tmp_path).effective_llm_provider
        == "openai"
    )


async def test_tool_call_extra_fields_are_echoed_back(tmp_path: Path) -> None:
    """Gemini's OpenAI endpoint returns a thought signature with each call and rejects the
    next turn unless it comes back; any unknown tool-call keys are carried through."""
    seen: list[dict[str, Any]] = []
    extra = {"extra_content": {"google": {"thought_signature": "c2ln"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return _ok(
                None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "t", "arguments": "{}"},
                        **extra,
                    }
                ],
            )
        return _ok("done")

    c = _client(tmp_path, handler)
    req = _user("go", tools=[ToolSpec(name="t", description="d")])
    first = await c.generate(req)
    assert first.tool_calls[0].extra == extra
    follow = req.model_copy(deep=True)
    follow.messages.append(Message(role="model", tool_calls=first.tool_calls))
    follow.messages.append(
        Message(role="tool", tool_results=[ToolResult(call_id="call_1", name="t", result=1)])
    )
    await c.generate(follow)
    sent = seen[1]["messages"][1]["tool_calls"][0]
    assert sent["extra_content"] == extra["extra_content"]
    assert sent["id"] == "call_1"


async def test_check_models_fails_fast_on_unknown_name(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "small-model"}, {"id": "other"}]})

    c = _client(tmp_path, handler)  # planner is "big-model", which the provider lacks
    with pytest.raises(LLMError, match="does not serve big-model"):
        await c.check_models()


async def test_check_models_passes_and_skips_when_listing_fails(tmp_path: Path) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "small-model"}, {"id": "big-model"}]})

    await _client(tmp_path, ok).check_models()

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    await _client(tmp_path, broken).check_models()  # warning only


async def test_verify_llm_models_unwraps_cache_and_ignores_stub(tmp_path: Path) -> None:
    from haulmind.llm.cache import CachingClient, DiskCache
    from haulmind.llm.factory import verify_llm_models
    from haulmind.llm.stub import StubLLMClient

    await verify_llm_models(StubLLMClient())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "small-model"}]})

    wrapped = CachingClient(_client(tmp_path, handler), DiskCache(tmp_path / "cache"), mode="live")
    with pytest.raises(LLMError, match="does not serve big-model"):
        await verify_llm_models(wrapped)


async def test_reasoning_effort_defaults_low_for_gpt_oss_only(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok("ok")

    await _client(tmp_path, handler, llm_model_planner="openai/gpt-oss-120b").generate(_user())
    assert seen[-1]["reasoning_effort"] == "low"
    await _client(tmp_path, handler).generate(_user())
    assert "reasoning_effort" not in seen[-1]
    await _client(tmp_path, handler, llm_reasoning_effort="high").generate(_user())
    assert seen[-1]["reasoning_effort"] == "high"
