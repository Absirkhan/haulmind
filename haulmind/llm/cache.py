"""Disk cache keyed by hash(model, system, messages, tools, schema, params).

Modes: ``live`` (read-through, call on miss), ``replay`` (cache only; miss raises
``CacheMiss`` so evals/demos can prove zero network calls), ``off``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from haulmind.config import LLMCacheMode
from haulmind.llm.base import CacheMiss, LLMClient, LLMRequest, LLMResponse, Role
from haulmind.util import sha256_hex


class DiskCache:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.hits = 0
        self.misses = 0

    def key(self, req: LLMRequest, model: str) -> str:
        return sha256_hex(req.cache_payload(model))

    def _path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> LLMResponse | None:
        p = self._path(key)
        if not p.exists():
            self.misses += 1
            return None
        try:
            data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
            resp = LLMResponse.model_validate(data["response"])
        except (json.JSONDecodeError, KeyError, ValueError):
            self.misses += 1
            return None
        self.hits += 1
        resp.cached = True
        resp.latency_ms = 0
        return resp

    def put(self, key: str, req: LLMRequest, model: str, resp: LLMResponse) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key,
            "request": req.cache_payload(model),
            "task": req.task,
            "response": resp.model_dump(mode="json"),
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    def size(self) -> int:
        return sum(1 for _ in self.dir.glob("*/*.json")) if self.dir.exists() else 0


class CachingClient:
    """Wraps any ``LLMClient`` with the disk cache."""

    def __init__(self, inner: LLMClient, cache: DiskCache, mode: LLMCacheMode = "live") -> None:
        self.inner = inner
        self.cache = cache
        self.mode = mode
        self.provider = inner.provider

    def model_for(self, role: Role) -> str:
        return self.inner.model_for(role)

    async def generate(self, req: LLMRequest) -> LLMResponse:
        if self.mode == "off":
            return await self.inner.generate(req)
        model = self.inner.model_for(req.role)
        key = self.cache.key(req, model)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        if self.mode == "replay":
            raise CacheMiss(
                f"no cached response for task={req.task!r} model={model} key={key[:12]}"
            )
        resp = await self.inner.generate(req)
        self.cache.put(key, req, model, resp)
        return resp
