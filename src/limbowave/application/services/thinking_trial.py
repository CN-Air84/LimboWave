"""An in-memory opt-in trial; never writes capability records itself."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from limbowave.application.kernel import KernelSetup


@dataclass
class ThinkingTrial:
    base_setup: KernelSetup
    model_id: str
    level: str
    previous_level: str
    id: str = field(default_factory=lambda: uuid4().hex)
    offered: bool = False

    @property
    def endpoint_id(self) -> str:
        return self.base_setup.endpoint_id

    def accept_success(self, result: dict[str, Any]) -> bool:
        if self.offered or (
            result.get("trial_id") != self.id
            or result.get("endpoint_id") != self.endpoint_id
            or result.get("model_id") != self.model_id
            or result.get("level") != self.level
            or result.get("status") != "completed"
            or result.get("request_sent") is not True
            or result.get("cancelled") is True
        ):
            return False
        self.offered = True
        return True
