"""Fixtures for the agent tests (INTERVIEW_AGENT_TEST_PLAN_V1 §2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage
from src.modules.interviews.core.conversation.turn_context import TurnContext, build_turn_context


def _question(qid: str, stage: str, competency: str = "skill-python") -> dict:
    return {
        "question_id": qid,
        "competency": competency,
        "stage": stage,
        "main_prompt": f"Câu hỏi {qid}?",
        "rubric_criteria": "điểm chính",
    }


POOL = {
    "WARM_UP": [_question("q-warm", "WARM_UP", "Khởi động")],
    "VALIDATE": [_question("q-val-1", "VALIDATE", "cv-validation"), _question("q-val-2", "VALIDATE", "cv-validation")],
    "DEEP_DIVE": [_question("q-dd-1", "DEEP_DIVE"), _question("q-dd-2", "DEEP_DIVE"), _question("q-dd-3", "DEEP_DIVE")],
    "CHALLENGE": [_question("q-ch-1", "CHALLENGE", "skill-system-design")],
    "BEHAVIORAL": [_question("q-bh-1", "BEHAVIORAL", "teamwork"), _question("q-bh-2", "BEHAVIORAL", "teamwork")],
}


@pytest.fixture
def session_state_factory():
    """Build a session state like ``build_session_state_from_db`` would, from a few knobs."""

    def make(
        *,
        stage: InterviewStage = InterviewStage.DEEP_DIVE,
        elapsed_ratio: float = 0.5,
        target_minutes: int = 25,
        asked: tuple[str, ...] = ("q-warm", "q-val-1", "q-val-2", "q-dd-1"),
        probes_used: int = 0,
        clarify_used: int = 0,
        tier: str | None = None,
        is_demo: bool = False,
        blueprint: dict | None = None,
        current_question: dict | None = None,
        extra: dict | None = None,
    ) -> dict[str, Any]:
        target_seconds = target_minutes * 60
        elapsed = int(target_seconds * elapsed_ratio)
        state: dict[str, Any] = {
            "current_stage": stage.value,
            "started_at": datetime.now(timezone.utc) - timedelta(seconds=elapsed),
            "elapsed_time": elapsed,
            "target_duration_minutes": target_minutes,
            "questions_pool": POOL,
            "asked_question_ids": list(asked),
            "current_turn_in_question": probes_used,
            "clarify_count": clarify_used,
            "current_question_context": current_question or _question("q-dd-1", stage.value),
            "practice_tier": tier,
            "is_demo": is_demo,
            "locale": "vi-VN",
        }
        if blueprint is not None:
            state["blueprint"] = blueprint
        if extra:
            state.update(extra)
        return state

    return make


class FakeToolCaller:
    """Stands in for ``ai.facade.generate_tool_calls``: scripted answers, recorded inputs."""

    def __init__(self, *answers: list[dict] | Exception):
        self.answers = list(answers)
        self.inputs: list[dict] = []

    async def __call__(self, **kwargs):
        self.inputs.append(kwargs)
        answer = self.answers.pop(0) if self.answers else []
        if isinstance(answer, Exception):
            raise answer
        return answer


def tool_call(name: str, call_id: str = "c1", **arguments) -> dict:
    arguments.setdefault("rationale", "scripted")
    arguments.setdefault("evidence_refs", ["answer:sent:1"])
    return {"name": name, "arguments": arguments, "call_id": call_id}


@pytest.fixture
def fake_tool_caller():
    return FakeToolCaller


@pytest.fixture
def ctx_factory(session_state_factory):
    def make(text: str = "Em dùng Redis làm cache và đặt TTL năm phút cho từng key.", **knobs) -> TurnContext:
        state = session_state_factory(**knobs)
        turn = CandidateTurnInput(session_id="sess-agent", turn_index=3, text_content=text)
        return build_turn_context(turn, state)

    return make


def scripted_chat_model(script: list[list[dict]], *, usage: dict | None = None, raise_with: Exception | None = None):
    """A LangChain chat model that answers each call with the next list of tool calls.

    What ``langmem``/``trustcall`` send: inserts are calls of the schema
    (``CandidateMemory``), updates ``PatchDoc(json_doc_id, patches)``, removals
    ``RemoveDoc(json_doc_id)``. ``json_doc_id`` of an existing row is
    ``uuid5(NAMESPACE_DNS, str((*namespace, key))).hex`` — see ``doc_id``.
    The model records every prompt it saw in ``.prompts``.
    """
    pytest.importorskip("langmem")
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _Scripted(BaseChatModel):
        model_name: str = "scripted-chat"
        script: list = []
        prompts: list = []
        usage: dict = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        raise_with: Any = None

        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kwargs):  # trustcall needs a RunnableBinding back
            return self.bind(tools=tools, **kwargs)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            self.prompts.append("\n".join(str(m.content) for m in messages))
            if self.raise_with is not None:
                raise self.raise_with
            calls = self.script.pop(0) if self.script else []
            message = AIMessage(content="", tool_calls=calls, usage_metadata=dict(self.usage))
            return ChatResult(generations=[ChatGeneration(message=message)])

    model = _Scripted(script=[list(step) for step in script], prompts=[], raise_with=raise_with)
    if usage is not None:
        model.usage = usage
    return model


def doc_id(namespace: tuple[str, ...], key: str) -> str:
    """The id ``langmem`` shows the model for a stored row (what PatchDoc/RemoveDoc must name)."""
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_DNS, str((*namespace, key))).hex
