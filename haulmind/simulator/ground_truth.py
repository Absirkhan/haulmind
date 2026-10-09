"""Ground-truth labels written alongside each run for evaluation."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from haulmind.events.schemas import Domain


class GroundTruthIncident(BaseModel):
    id: str
    type: str
    domain: Domain
    vehicle_id: str | None = None
    driver_id: str | None = None
    start_ts: datetime
    end_ts: datetime
    benign: bool = False
    expected_actions: list[str] = Field(default_factory=list)
    notes: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class GroundTruth(BaseModel):
    run_id: str
    seed: int
    config: dict[str, Any]
    incidents: list[GroundTruthIncident]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> GroundTruth:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @property
    def positives(self) -> list[GroundTruthIncident]:
        return [i for i in self.incidents if not i.benign]

    @property
    def benigns(self) -> list[GroundTruthIncident]:
        return [i for i in self.incidents if i.benign]
