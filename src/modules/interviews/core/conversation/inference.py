"""What the deterministic turn engine needs from a model, as a contract.

Core owns the interview's rules: the guards, the action menu, the stage
transitions, what a turn is allowed to do. It does not own *how* a sentence
gets written or an answer gets read — that is inference, and it belongs to
``agents/interviewer/``.

This module is the seam between them. ``TurnInference`` declares the calls the
engine makes; ``agents/interviewer/response_generation.py`` implements them
against the shared OpenAI facade. Core imports nothing but ``typing`` here, so
the rule that keeps providers, LangGraph, LiveKit, the database and LangMem out
of Core holds by construction rather than by convention.

The method names are the historical ones on purpose. ``caller_purpose`` builds
every ``ai_call_logs.purpose`` from the calling function's name, and
``langfuse.tracing.STEP_BY_CALLER`` maps those same names to the step labels a
reviewer reads. Renaming them would silently split the measurement history that
§13 of INTERVIA_INTERVIEW_AGENT_V2_SPEC depends on.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, Sequence, runtime_checkable

#: Exactly the names ``CoreTurnEngine`` may delegate to an implementation.
#: Anything else is a typo and must raise, not resolve to a stub.
INFERENCE_METHODS: frozenset[str] = frozenset(
    {
        "_candidate_tone_instruction",
        "_generate_interviewer_line",
        "_evaluate_candidate_response",
        "_generate_probe_question",
        "_generate_clarify_text",
        "_answer_closing_question",
        "_llm_options",
        "llm",
        "_generate_fn",
        "candidate_tone",
    }
)


@runtime_checkable
class TurnInference(Protocol):
    """The model-backed calls a turn may make.

    An implementation never decides what happens next: it reads an answer or
    writes a line, and the engine decides. Every text it returns still passes
    the Core output guards afterwards.
    """

    candidate_tone: str

    def _candidate_tone_instruction(self, *, is_vi: bool) -> str:
        """The tone clause appended to a spoken prompt."""
        ...

    async def _generate_interviewer_line(
        self,
        *,
        purpose: str,
        context: str,
        is_vi: bool,
        max_words: int = 35,
        reject: Optional[Any] = None,
        fallback: str = "",
        speech_kind: Optional[str] = None,
    ) -> str:
        """One short line for the candidate, for ``purpose``."""
        ...

    async def _evaluate_candidate_response(
        self,
        *,
        candidate_text: str,
        current_stage: Any,
        current_question: Dict[str, Any],
        is_vi: bool = True,
        telemetry: Optional[Dict[str, Any]] = None,
        precomputed_evaluation: Optional[Dict[str, Any]] = None,
        recent_lines: Sequence[str] = (),
        seed: str = "",
        memory_hits: Any = (),
    ) -> Dict[str, Any]:
        """Read the answer: intent, sufficiency, the missing point."""
        ...

    async def _generate_probe_question(
        self,
        *,
        candidate_text: str,
        current_question: Dict[str, Any],
        is_vi: bool = True,
        current_stage: Any = None,
        telemetry: Optional[Dict[str, Any]] = None,
        missing_aspect: str = "",
        seed: str = "",
        recent_lines: Sequence[str] = (),
        memory_hits: Any = (),
    ) -> str:
        """The one follow-up question aimed at the missing point."""
        ...

    async def _generate_clarify_text(
        self,
        *,
        current_question: Dict[str, Any],
        candidate_text: str,
        is_vi: bool,
        memory_hits: Any = (),
    ) -> str:
        """The pending question said another way."""
        ...

    async def _answer_closing_question(
        self,
        *,
        candidate_text: str,
        job_context: str,
        is_vi: bool,
        recent_lines: Sequence[str] = (),
    ) -> str:
        """An answer to the candidate's own question, from the posting only."""
        ...


class MissingInference:
    """Raises on any model call, so a misbuilt engine fails where it is built.

    ``CoreTurnEngine`` is usable on its own for the deterministic paths — the
    guards, the menu, the stage machine. The moment one of those paths needs a
    sentence written, an engine assembled without an inference implementation
    should say so plainly rather than fail later with ``AttributeError`` deep in
    an action handler.
    """

    candidate_tone: str = "standard"

    def _candidate_tone_instruction(self, *, is_vi: bool) -> str:
        return ""

    def __getattr__(self, name: str) -> Any:
        async def _missing(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(
                f"{name} needs a model: build the engine through "
                "agents.interviewer.engine.InterviewCoreEngine, which supplies one."
            )

        return _missing
