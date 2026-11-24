"""Validated, immutable Admin contract snapshot used by Planner operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.interviews.core.session_policy.blueprint import InterviewBlueprint


class PlannerContractSnapshot(BaseModel):
    """The exact versioned contract Planner used for P1/P2."""

    model_config = ConfigDict(extra="forbid")

    blueprint_id: str = Field(min_length=1)
    policy_blueprint_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: InterviewBlueprint

    @model_validator(mode="after")
    def _fingerprint_matches_policy(self) -> PlannerContractSnapshot:
        canonical = json.dumps(
            self.policy.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual != self.fingerprint:
            raise ValueError("Planner contract fingerprint does not match its policy")
        return self

    @classmethod
    def from_plan_blueprint(cls, value: Any) -> PlannerContractSnapshot:
        if not isinstance(value, dict):
            raise ValueError("Planner plan is missing its frozen Admin contract")
        return cls.model_validate(
            {
                "blueprint_id": value.get("blueprintId"),
                "policy_blueprint_id": value.get("policyBlueprintId") or value.get("blueprintId"),
                "version_id": value.get("versionId"),
                "version": value.get("version"),
                "name": value.get("name"),
                "fingerprint": value.get("fingerprint"),
                "policy": value.get("policy"),
            }
        )
