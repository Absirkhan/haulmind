"""Structured LLM outputs. Pydantic models double as the JSON schemas sent to the model."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TriageOutput(BaseModel):
    decision: Literal["act", "watch", "dismiss"]
    domain: Literal["safety", "maintenance", "compliance", "spend"]
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    incident_type: str = Field(description="short snake_case label, e.g. hos_violation_risk")
    reason: str = Field(description="one sentence")


class ActionProposal(BaseModel):
    tool: str = Field(description="exact write-tool name")
    args: dict[str, Any] = Field(description="arguments matching the tool schema")
    title: str = Field(description="imperative, under 80 chars, for the manager inbox")
    reasoning: str = Field(description="two sentences linking evidence to the action")
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, description="evidence ids like E1")


class PlanOutput(BaseModel):
    summary: str = Field(description="one or two sentences for the manager")
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    actions: list[ActionProposal] = Field(default_factory=list)


class VerifyItem(BaseModel):
    index: int
    approved: bool
    reason: str


class VerifyOutput(BaseModel):
    results: list[VerifyItem]


class InvestigateOutput(BaseModel):
    findings: str
    key_facts: list[str] = Field(default_factory=list)


class SingleAgentOutput(BaseModel):
    decision: Literal["act", "watch", "dismiss"]
    incident_type: str = ""
    reason: str = ""
    summary: str = ""
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    actions: list[ActionProposal] = Field(default_factory=list)


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for Gemini's response_json_schema (titles removed to save tokens)."""
    schema: dict[str, Any] = _strip_titles(model.model_json_schema())
    return schema


def _strip_titles(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_titles(v) for k, v in obj.items() if k != "title"}
    if isinstance(obj, list):
        return [_strip_titles(x) for x in obj]
    return obj
