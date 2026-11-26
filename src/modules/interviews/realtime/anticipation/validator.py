"""What a speculative preview must match before anything may reuse it.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §7 and the V2 correction brief §5. A preview
is computed from a transcript the candidate had not finished saying, against a
session state that may since have moved. Reusing one is only safe when nothing
it depended on has changed, so the preview carries the identity of everything
it was computed from and that identity is compared on the way out.

The spec names the parts: session, turn, transcript revision, question,
session-state version, rubric/plan version, policy/context version. A single
mismatch discards the preview; there is no partial credit, because a decision
made against last turn's question is wrong however fresh the rest of it is.

Pure and synchronous: no I/O, no provider, nothing to mock in a test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

__all__ = ["PreviewKey", "PreviewRejected", "preview_key", "validate_preview"]


def _normalised(transcript: str) -> str:
    """Whitespace-insensitive transcript, so re-spacing is not a new revision."""
    return " ".join(str(transcript or "").split())


def transcript_revision(transcript: str) -> str:
    """A short stable hash of the transcript this preview assumed."""
    return hashlib.sha256(_normalised(transcript).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PreviewKey:
    """Everything a preview was computed from.

    Frozen on purpose: a key that could be edited after the fact would let a
    stale preview be made to look fresh.
    """

    session_id: str
    turn_id: str
    question_id: str
    transcript_revision: str
    state_version: str
    rubric_version: str = ""
    policy_version: str = ""

    def matches(self, other: "PreviewKey") -> list[str]:
        """The fields that differ; empty when the preview is still valid."""
        return [
            name
            for name in (
                "session_id",
                "turn_id",
                "question_id",
                "transcript_revision",
                "state_version",
                "rubric_version",
                "policy_version",
            )
            if getattr(self, name) != getattr(other, name)
        ]


def preview_key(
    *,
    session_id: Any,
    turn_id: Any,
    question_id: Any,
    transcript: str,
    state_version: Any,
    rubric_version: Any = "",
    policy_version: Any = "",
) -> PreviewKey:
    """Build the key for a transcript snapshot and the state it was read against."""
    return PreviewKey(
        session_id=str(session_id or ""),
        turn_id=str(turn_id or ""),
        question_id=str(question_id or ""),
        transcript_revision=transcript_revision(transcript),
        state_version=str(state_version or ""),
        rubric_version=str(rubric_version or ""),
        policy_version=str(policy_version or ""),
    )


@dataclass(frozen=True)
class PreviewRejected:
    """Why a preview could not be reused, for the wasted-call metric of §13."""

    reason: str
    fields: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # a rejection is always falsey as a "usable preview"
        return False


def validate_preview(
    *,
    preview: Any,
    computed_for: PreviewKey | None,
    final: PreviewKey,
) -> PreviewRejected | None:
    """``None`` when the preview may be reused, otherwise why it may not.

    The transcript check is the one that matters most: §1.1 forbids committing
    anything derived from a partial transcript, so a preview whose assumed text
    is not exactly what the candidate finally said is discarded even if every
    version around it still agrees.
    """
    if preview is None:
        return PreviewRejected("no_preview")
    if computed_for is None:
        return PreviewRejected("no_key")
    differing = computed_for.matches(final)
    if differing:
        return PreviewRejected("stale", tuple(differing))
    return None
