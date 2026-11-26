"""Which execution path a LIVE turn takes: Fast, Unified or Hybrid.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §6.1 and the V2 correction brief §6. These
are *execution paths*, chosen per turn from what the turn already knows. They
are not RULE / LIVE / SHADOW, which are *rollout modes* chosen per deployment;
the router only runs inside LIVE, and SHADOW and RULE never see it.

The router is deterministic and costs nothing: the brief forbids spending an
LLM call on routing, so every input here is a value the turn already holds.

    Fast     an input guard decided, or a validated preview already carries
             the decision — 0 new calls
    Unified  one Thinker inference (the §6.2 single call) — 1 call
    Hybrid   Thinker and the Luna Talker concurrently — 2 calls in parallel,
             only when voice/video, the Talker is on, and a model is set
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["FAST", "UNIFIED", "HYBRID", "ExecutionPath", "choose_path"]

FAST = "fast"
UNIFIED = "unified"
HYBRID = "hybrid"

#: Modalities on which an early spoken acknowledgment is worth anything.
_SPOKEN_MODALITIES = frozenset({"VOICE", "VIDEO"})


@dataclass(frozen=True)
class ExecutionPath:
    """The chosen path and the one reason it was chosen, for the record."""

    path: str
    reason: str

    @property
    def is_fast(self) -> bool:
        return self.path == FAST

    @property
    def is_hybrid(self) -> bool:
        return self.path == HYBRID


def choose_path(
    *,
    early_decision: Any,
    preview_decision: Mapping[str, Any] | None,
    modality: str,
    hybrid_enabled: bool,
    talker_model: str,
) -> ExecutionPath:
    """Pick the path from values the turn already has. Never calls a model.

    Order matters and is a cost ladder: anything that needs no new call wins,
    then the single call, then the parallel pair. A preview counts only when
    it carries a *decision* — an evaluation-only preview speeds the rule path
    but is not a decision the Fast path may execute.
    """
    if early_decision is not None:
        return ExecutionPath(FAST, "input_guard")
    if preview_decision and preview_decision.get("action"):
        return ExecutionPath(FAST, "validated_preview")
    if hybrid_enabled and talker_model and str(modality or "").upper() in _SPOKEN_MODALITIES:
        return ExecutionPath(HYBRID, "spoken_modality_with_talker")
    if hybrid_enabled and not talker_model:
        return ExecutionPath(UNIFIED, "hybrid_enabled_but_no_talker_model")
    return ExecutionPath(UNIFIED, "default")
