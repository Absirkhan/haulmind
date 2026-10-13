"""Gemini request mapping that needs no network: thought signatures survive a tool round trip."""

from __future__ import annotations

import base64

from google.genai import types

from haulmind.llm.base import Message, ToolCallReq, ToolResult
from haulmind.llm.gemini import _to_contents


def test_thought_signature_is_echoed_on_function_call_parts() -> None:
    sig = base64.b64encode(b"opaque-signature").decode()
    call = ToolCallReq(id="c1", name="get_vehicle_status", args={"vehicle_id": "V-07"})
    call.extra = {"thought_signature": sig}
    contents = _to_contents(
        [
            Message(role="user", text="check V-07"),
            Message(role="model", tool_calls=[call]),
            Message(
                role="tool",
                tool_results=[
                    ToolResult(call_id="c1", name="get_vehicle_status", result={"ok": 1})
                ],
            ),
        ]
    )
    assert [c.role for c in contents] == ["user", "model", "user"]
    part = contents[1].parts[0]
    assert part.function_call is not None
    assert part.function_call.name == "get_vehicle_status"
    assert part.thought_signature == b"opaque-signature"
    assert contents[2].parts[0].function_response is not None


def test_function_call_without_signature_has_none() -> None:
    contents = _to_contents([Message(role="model", tool_calls=[ToolCallReq(id="c", name="t")])])
    assert contents[0].parts[0].thought_signature is None
    assert isinstance(contents[0].parts[0], types.Part)
