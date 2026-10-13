"""LLM plumbing (cache, replay, rate limiter, quota) and the RAG index."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from haulmind.agents.schemas import ActionProposal, PlanOutput, json_schema
from haulmind.config import get_settings
from haulmind.llm.base import CacheMiss, LLMRequest, Message, QuotaExhausted
from haulmind.llm.cache import CachingClient, DiskCache
from haulmind.llm.gemini import _quota_detail
from haulmind.llm.quota import QuotaGuard
from haulmind.llm.ratelimit import AsyncRateLimiter
from haulmind.llm.stub import StubLLMClient


def _req(text: str = "hello") -> LLMRequest:
    return LLMRequest(
        role="triage",
        system="s",
        messages=[Message(role="user", text=text)],
        task="triage",
        meta={"payload": {"domain": "spend", "signals": {"overfill": True}}},
    )


async def test_cache_hit_then_replay_miss(tmp_path: Path) -> None:
    inner = StubLLMClient()
    cache = DiskCache(tmp_path / "cache")
    live = CachingClient(inner, cache, mode="live")
    r1 = await live.generate(_req())
    r2 = await live.generate(_req())
    assert not r1.cached and r2.cached and r2.parsed == r1.parsed
    assert inner.requests == 1
    replay = CachingClient(StubLLMClient(), cache, mode="replay")
    r3 = await replay.generate(_req())
    assert r3.cached
    with pytest.raises(CacheMiss):
        await replay.generate(_req("different prompt"))
    assert cache.size() == 1


def test_cache_key_ignores_meta_but_not_params() -> None:
    cache = DiskCache(Path("."))
    a = _req()
    b = _req()
    b.meta = {"payload": {"other": 1}}
    assert cache.key(a, "m") == cache.key(b, "m")
    c = _req()
    c.temperature = 0.9
    assert cache.key(a, "m") != cache.key(c, "m")
    assert cache.key(a, "m") != cache.key(a, "m2")


async def test_rate_limiter_blocks_beyond_rpm() -> None:
    rl = AsyncRateLimiter(rpm=3, window_s=0.6)
    t0 = time.monotonic()
    for _ in range(3):
        await rl.acquire()
    assert time.monotonic() - t0 < 0.3
    await rl.acquire()  # fourth must wait for the window
    assert time.monotonic() - t0 >= 0.5


def test_quota_guard(tmp_path: Path) -> None:
    q = QuotaGuard(tmp_path / "quota.json", {"m": 2})
    q.check("m")
    q.record("m")
    q.record("m")
    with pytest.raises(QuotaExhausted):
        q.check("m")
    q2 = QuotaGuard(tmp_path / "quota.json", {"m": 2})
    assert q2.used("m") == 2, "usage persists across processes"
    assert q2.snapshot()["m"]["remaining"] == 0


def test_stub_is_deterministic() -> None:
    stub = StubLLMClient()
    a = asyncio.run(stub.generate(_req()))
    b = asyncio.run(stub.generate(_req()))
    assert a.parsed == b.parsed and a.provider == "stub"


def test_rag_index_finds_the_right_doc() -> None:
    from haulmind.rag.index import build_index, reset_index_cache

    get_settings().ensure_dirs()
    reset_index_cache()
    idx = build_index(force=True)
    assert len(idx.chunks) > 30
    hits = idx.search("engine coolant over temperature", k=3, codes=["P0217"])
    assert hits and hits[0].chunk.doc.startswith("dtc-p0217")
    hits = idx.search("alternator voltage dropping", k=3, codes=["P0562"])
    assert any("p0562" in h.chunk.doc or "charging" in h.chunk.doc for h in hits)
    hits = idx.search("reefer tank combined fill fuel card", k=3)
    assert any("fuel-card" in h.chunk.doc for h in hits)
    assert all(h.citation.endswith(tuple("abcdefghijklmnopqrstuvwxyz0123456789-")) for h in hits)


def test_json_schema_strips_titles_but_keeps_title_field() -> None:
    """Gemini rejects a schema whose ``required`` names a property that is not defined.

    ``ActionProposal.title`` is a real field; only the schema metadata ``title`` must go.
    """
    schema = json_schema(PlanOutput)
    assert "title" not in schema
    action = schema["$defs"]["ActionProposal"]
    assert "title" not in action
    assert "title" in action["properties"]
    assert action["properties"]["title"]["type"] == "string"
    assert set(action["required"]) <= set(action["properties"])
    for sub in action["properties"].values():
        assert "title" not in sub
    assert "title" in json_schema(ActionProposal)["properties"]


def test_quota_detail_keeps_the_informative_part_of_a_429() -> None:
    body = """429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota.
* Quota exceeded for metric: generate_content_free_tier_requests, limit: 20, model: gemini-3.6-flash
Please retry in 2h34m42.224738582s.', 'details': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}"""
    detail = _quota_detail(body)
    assert "limit: 20, model: gemini-3.6-flash" in detail
    assert "quotaId=GenerateRequestsPerDayPerProjectPerModel-FreeTier" in detail
    assert detail.endswith("retry in 2h34m42.224738582s")
    assert _quota_detail("429 no structure here") == "429 no structure here"
