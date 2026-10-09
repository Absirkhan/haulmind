"""Small shared helpers: ids, time, hashing, geo."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import UTC, datetime
from typing import Any


def new_id(prefix: str, nbytes: int = 4) -> str:
    """Short, URL-safe, human-readable id like ``INC-9f3a1c2b``."""
    return f"{prefix}-{secrets.token_hex(nbytes)}"


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(UTC)


def to_naive_utc(dt: datetime) -> datetime:
    """SQLite stores naive datetimes; normalise everything to naive UTC on the way in."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def to_aware_utc(dt: datetime | None) -> datetime | None:
    """Treat naive datetimes from SQLite as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    """Compact ISO-8601 with ``Z`` suffix."""
    if dt is None:
        return None
    aware = to_aware_utc(dt)
    assert aware is not None
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for hashing and cache keys."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def sha256_hex(*parts: Any) -> str:
    """SHA-256 over the canonical JSON of the parts."""
    h = hashlib.sha256()
    for part in parts:
        h.update(canonical_json(part).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
