"""Daily request-count guard persisted to ``data/quota.json``.

Gemini free-tier daily quotas reset at midnight Pacific; we track by UTC date, which is
conservative (we reset a few hours later than Google does).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from haulmind.llm.base import QuotaExhausted


class QuotaGuard:
    def __init__(self, path: Path, budgets: dict[str, int]) -> None:
        self.path = path
        self.budgets = budgets
        self._state: dict[str, dict[str, int]] = {}
        self._load()

    def _today(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except json.JSONDecodeError:
            raw = {}
        self._state = raw.get(self._today(), {}) if isinstance(raw, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({self._today(): self._state}, indent=2), encoding="utf-8")

    def used(self, model: str) -> int:
        self._load()
        return self._state.get(model, {}).get("requests", 0)

    def remaining(self, model: str) -> int | None:
        budget = self.budgets.get(model)
        if budget is None:
            return None
        return max(0, budget - self.used(model))

    def check(self, model: str) -> None:
        """Raise ``QuotaExhausted`` if the configured daily budget is used up."""
        rem = self.remaining(model)
        if rem is not None and rem <= 0:
            raise QuotaExhausted(model, f"{self.budgets[model]} requests/day budget reached")

    def record(self, model: str, n: int = 1) -> None:
        self._load()
        entry = self._state.setdefault(model, {"requests": 0})
        entry["requests"] += n
        self._save()

    def mark_exhausted(self, model: str) -> None:
        """Server said the daily quota is gone: pin usage at the budget so we stop retrying."""
        self._load()
        budget = self.budgets.get(model, 0)
        self._state[model] = {"requests": max(budget, self.used(model))}
        self._save()

    def snapshot(self) -> dict[str, dict[str, int | None]]:
        self._load()
        return {
            m: {"used": self.used(m), "budget": b, "remaining": self.remaining(m)}
            for m, b in self.budgets.items()
        }
