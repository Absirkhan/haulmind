"""Provider-neutral helpers for turning model text into structured data."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from model output.

    Handles bare JSON, fenced code blocks, and prose around a JSON object or array (for
    example a reasoning preamble). Returns ``None`` when nothing parseable is found.
    """
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text)
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
