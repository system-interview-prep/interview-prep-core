"""What a speculative preview must match before anything may reuse it.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §7 and the V2 correction briefs §5. A preview
is computed from a transcript the candidate had not finished saying, against a
session state that may since have moved. Reusing one is only safe when nothing
it depended on has changed, so the preview carries the identity of everything
it was computed from and that identity is compared on the way out.

The spec names the parts: session, turn, transcript revision, question,
session-state version, rubric/plan version, policy/context version. A single
mismatch discards the preview; there is no partial credit, because a decision
made against last turn's question is wrong however fresh the rest of it is.

**Who can check what.** Only the server knows the authoritative turn id,
question version, session-state, rubric and policy versions: it reads them
from the database at issuance, seals them in the token, reads them again at
redemption and compares. The LiveKit worker knows the session and the
transcript it heard, nothing more. A field a side does not know is ``None``,
and :func:`validate_preview` says which fields it could not check rather than
pretending it did. The server validates with ``strict=True``, where a missing
field is itself a rejection; the worker validates leniently and relies on the
server for the rest. Seven fields in a dataclass are not seven-field
validation -- the strict server check is.

Pure and synchronous: no I/O, no provider, nothing to mock in a test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from typing import Any, Optional

__all__ = [
    "KEY_FIELDS",
    "PreviewKey",
    "PreviewRejected",
    "preview_key",
    "transcript_revision",
    "validate_preview",
]

KEY_FIELDS: tuple[str, ...] = (
    "session_id",
    "turn_id",
    "question_id",
    "transcript_revision",
    "state_version",
    "rubric_version",
    "policy_version",
)


def _normalised(transcript: str) -> str:
    """Whitespace-insensitive transcript, so re-spacing is not a new revision."""
    return " ".join(str(transcript or "").split())


def transcript_revision(transcript: str) -> str:
    """A short stable hash of the transcript this preview assumed."""
    return hashlib.sha256(_normalised(transcript).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PreviewKey:
    """Everything a preview was computed from; ``None`` = unknown to this side.

    Frozen on purpose: a key that could be edited after the fact would let a
    stale preview be made to look fresh.
    """

    session_id: Optional[str]
    turn_id: Optional[str] = None
    question_id: Optional[str] = None
    transcript_revision: Optional[str] = None
    state_version: Optional[str] = None
    rubric_version: Optional[str] = None
    policy_version: Optional[str] = None

    def missing(self) -> list[str]:
        """Fields this side does not know."""
        return [name for name in KEY_FIELDS if not getattr(self, name)]

    def differing(self, other: "PreviewKey") -> list[str]:
        """Fields known on both sides that disagree."""
        return [
            name
            for name in KEY_FIELDS
            if getattr(self, name) and getattr(other, name) and getattr(self, name) != getattr(other, name)
        ]

    def unchecked(self, other: "PreviewKey") -> list[str]:
        """Fields that could not be compared because a side did not know them."""
        return [name for name in KEY_FIELDS if not getattr(self, name) or not getattr(other, name)]

    def as_dict(self) -> dict[str, Optional[str]]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["PreviewKey"]:
        """A key sealed in a token, or ``None`` if the payload is not one."""
        if not isinstance(raw, dict):
            return None
        return cls(**{name: (str(raw[name]) if raw.get(name) else None) for name in KEY_FIELDS})


def preview_key(
    *,
    session_id: Any,
    transcript: str,
    turn_id: Any = None,
    question_id: Any = None,
    state_version: Any = None,
    rubric_version: Any = None,
    policy_version: Any = None,
) -> PreviewKey:
    """Build the key for a transcript snapshot and whatever versions are known."""

    def opt(value: Any) -> Optional[str]:
        text = str(value).strip() if value is not None else ""
        return text or None

    return PreviewKey(
        session_id=opt(session_id),
        turn_id=opt(turn_id),
        question_id=opt(question_id),
        transcript_revision=transcript_revision(transcript),
        state_version=opt(state_version),
        rubric_version=opt(rubric_version),
        policy_version=opt(policy_version),
    )


@dataclass(frozen=True)
class PreviewRejected:
    """Why a preview could not be reused, for the wasted-call metric of §13."""

    reason: str  # no_preview | no_key | incomplete | stale
    fields: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # a rejection is always falsey as a "usable preview"
        return False


def validate_preview(
    *,
    preview: Any,
    computed_for: Optional[PreviewKey],
    final: Optional[PreviewKey],
    strict: bool = True,
) -> Optional[PreviewRejected]:
    """``None`` when the preview may be reused, otherwise why it may not.

    ``strict`` is the server's mode: every one of the seven fields must be
    known on both sides and equal. Anything missing is ``incomplete`` -- an
    old token, a turn without a question version, a session without a frozen
    contract -- and is rejected rather than waved through. The worker uses
    ``strict=False``, compares what it knows (session and transcript), and
    leaves the rest to the server.

    The transcript check is the one that matters most: §1.1 forbids committing
    anything derived from a partial transcript, so a preview whose assumed text
    is not exactly what the candidate finally said is discarded even if every
    version around it still agrees. It is never skippable: both sides always
    know the transcript.
    """
    if preview is None:
        return PreviewRejected("no_preview")
    if computed_for is None or final is None:
        return PreviewRejected("no_key")
    if not computed_for.transcript_revision or not final.transcript_revision:
        return PreviewRejected("incomplete", ("transcript_revision",))
    if strict:
        missing = sorted(set(computed_for.missing()) | set(final.missing()))
        if missing:
            return PreviewRejected("incomplete", tuple(missing))
    differing = computed_for.differing(final)
    if differing:
        return PreviewRejected("stale", tuple(differing))
    return None
