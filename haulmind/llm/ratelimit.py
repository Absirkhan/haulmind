"""Async sliding-window rate limiter (requests per minute) and backoff helper."""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque


class AsyncRateLimiter:
    """Allow at most ``rpm`` acquisitions in any 60-second window."""

    def __init__(self, rpm: int, window_s: float = 60.0) -> None:
        self.rpm = max(1, rpm)
        self.window_s = window_s
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Block until a slot is free. Returns seconds waited."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= self.window_s:
                    self._stamps.popleft()
                if len(self._stamps) < self.rpm:
                    self._stamps.append(now)
                    return waited
                sleep_for = self.window_s - (now - self._stamps[0]) + 0.05
            await asyncio.sleep(sleep_for)
            waited += sleep_for

    def penalize(self, seconds: float) -> None:
        """After a 429, pretend the window is full for ``seconds``."""
        now = time.monotonic()
        self._stamps.clear()
        for _ in range(self.rpm):
            self._stamps.append(now - self.window_s + seconds)


def backoff_delay(
    attempt: int, base: float = 2.0, cap: float = 60.0, hint: float | None = None
) -> float:
    """Exponential backoff with full jitter; honours a server retry hint when given."""
    if hint is not None and hint > 0:
        return min(cap, hint + random.uniform(0, 1.0))
    return min(cap, base * (2.0**attempt)) * random.uniform(0.5, 1.0)
