"""Fast, Unified and Hybrid: the execution paths inside LIVE (V2 spec §6.1).

These are not RULE / LIVE / SHADOW. Those are rollout modes and the tests
here assert they are untouched. The router is deterministic — it must never
spend a call to decide how many calls to spend — and the Arbiter never lets
the Talker decide anything.
"""

from __future__ import annotations

import asyncio

import pytest

from src.modules.interviews.agents.interviewer import graph as interview_agent
from src.modules.interviews.agents.interviewer.arbiter import arbitrate
from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from src.modules.interviews.agents.interviewer.router import FAST, HYBRID, UNIFIED, choose_path
from src.modules.interviews.agents.interviewer.talker import TalkerOutput, predict_by_talker
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage
from src.modules.interviews.core.conversation.turn_context import Decision
from tests.modules.interviews.agent.test_call_budget import CountingLLM


# ----------------------------------------------------------------- router


def test_the_router_is_a_cost_ladder_and_never_calls_a_model() -> None:
    guard = Decision("confirm_abort", {}, reason="abort phrase")
    preview = {"action": "acknowledge_and_probe", "args": {"probe_text": "…?"}}

    assert choose_path(early_decision=guard, preview_decision=None, modality="VOICE",
                       hybrid_enabled=True, talker_model="gpt-5.6-luna").path == FAST
    assert choose_path(early_decision=None, preview_decision=preview, modality="VOICE",
                       hybrid_enabled=True, talker_model="gpt-5.6-luna").path == FAST
    assert choose_path(early_decision=None, preview_decision=None, modality="VOICE",
                       hybrid_enabled=True, talker_model="gpt-5.6-luna").path == HYBRID
    # Chat gets no early speech, so no Talker.
    assert choose_path(early_decision=None, preview_decision=None, modality="CHAT",
                       hybrid_enabled=True, talker_model="gpt-5.6-luna").path == UNIFIED
    # Hybrid without a model configured is Unified, and says why.
    chosen = choose_path(early_decision=None, preview_decision=None, modality="VOICE",
                         hybrid_enabled=True, talker_model="")
    assert chosen.path == UNIFIED and "no_talker_model" in chosen.reason
    assert choose_path(early_decision=None, preview_decision=None, modality="VOICE",
                       hybrid_enabled=False, talker_model="gpt-5.6-luna").path == UNIFIED


def test_an_evaluation_only_preview_is_not_a_fast_path() -> None:
    """A preview that sped up the rule path carries no action to execute."""
    chosen = choose_path(early_decision=None, preview_decision={"evaluation": {"score": 7}},
                         modality="VOICE", hybrid_enabled=False, talker_model="")
    assert chosen.path == UNIFIED


# ---------------------------------------------------------------- arbiter


def _thinker(action="acknowledge_and_probe", **args) -> Decision:
    return Decision(action, {"kind": "gap", "missing_aspect": "cache invalidation", **args},
                    reason="thinker", evidence_refs=["answer:sent:1"], source="agent", path=["model"])


def _talker(action="acknowledge_and_probe", target="cache invalidation",
            response="Khi dữ liệu trong database đổi, em xoá key cache theo cách nào?",
            ack="Mình nghe rồi.") -> TalkerOutput:
    return TalkerOutput(acknowledgment=ack, acknowledgment_rejected=None, predicted_action=action,
                        predicted_target=target, predicted_response=response, model="gpt-5.6-luna")


MENU = ["acknowledge_and_probe", "clarify_question", "move_to_next_question"]


def test_the_arbiter_reuses_the_talker_only_on_a_proven_match(ctx_factory) -> None:
    ctx = ctx_factory()
    verdict = arbitrate(ctx, thinker=_thinker(), talker=_talker(), allowed_actions=MENU)

    assert verdict.talker_reused and verdict.talker_outcome == "reused"
    assert verdict.decision.action == "acknowledge_and_probe"
    assert verdict.decision.args["probe_text"].startswith("Khi dữ liệu")
    assert "talker_reused" in verdict.decision.path
    assert verdict.acknowledgment == "Mình nghe rồi."


@pytest.mark.parametrize(
    "talker,outcome",
    [
        (None, "no_talker"),
        (_talker(action="clarify_question"), "action_mismatch"),
        (_talker(target="xử lý khi Redis lỗi"), "target_mismatch"),
        (_talker(response=""), "no_response"),
        # Hands the candidate a rubric term the conversation never used.
        (_talker(response="Em có dùng write-through cache để tránh dữ liệu cũ không?"), "guard:probe"),
    ],
)
def test_the_thinker_stands_when_equivalence_cannot_be_shown(ctx_factory, talker, outcome) -> None:
    """§6.4: same action alone is not enough; the Thinker's response is official."""
    ctx = ctx_factory()
    thinker = _thinker()
    verdict = arbitrate(ctx, thinker=thinker, talker=talker, allowed_actions=MENU)

    assert not verdict.talker_reused
    assert verdict.talker_outcome == outcome
    assert verdict.decision is thinker
    assert "probe_text" not in verdict.decision.args


def test_the_thinker_own_line_always_wins_over_the_talker(ctx_factory) -> None:
    """When the single inference already wrote the line, Luna's is discarded."""
    ctx = ctx_factory()
    thinker = _thinker(probe_text="Khi database đổi, em xử lý cache thế nào?")
    verdict = arbitrate(ctx, thinker=thinker, talker=_talker(), allowed_actions=MENU)

    assert verdict.talker_outcome == "thinker_has_text"
    assert verdict.decision.args["probe_text"] == "Khi database đổi, em xử lý cache thế nào?"


def test_the_talker_can_never_promote_its_prediction_to_a_decision(ctx_factory) -> None:
    """The Thinker chose to move on; a Talker that predicted a probe changes nothing."""
    ctx = ctx_factory()
    thinker = Decision("move_to_next_question", {"reason": "sufficient"}, source="agent")
    verdict = arbitrate(ctx, thinker=thinker, talker=_talker(), allowed_actions=MENU)

    assert verdict.decision.action == "move_to_next_question"
    assert not verdict.talker_reused


# ----------------------------------------------------------------- talker


@pytest.mark.asyncio
async def test_the_talker_guards_its_acknowledgment_and_never_raises(ctx_factory) -> None:
    ctx = ctx_factory()

    async def praising(**_kw):
        return [{"name": "talker_prediction", "call_id": "t1", "arguments": {
            "acknowledgment": "Rất tốt, bạn nắm vấn đề khá chắc.",   # praise: must not be said early
            "prediction": {"action": "acknowledge_and_probe", "target": "cache invalidation",
                           "response": "Khi dữ liệu đổi, em xoá cache thế nào?"},
        }}]

    out = await predict_by_talker(ctx, allowed_actions=MENU, pacing={}, generate=praising, model="gpt-5.6-luna")
    assert out is not None
    assert out.acknowledgment is None and out.acknowledgment_rejected
    assert out.predicted_action == "acknowledge_and_probe"

    async def boom(**_kw):
        raise RuntimeError("luna down")

    assert await predict_by_talker(ctx, allowed_actions=MENU, pacing={}, generate=boom, model="gpt-5.6-luna") is None
    # No model configured means no Talker, not a crash.
    assert await predict_by_talker(ctx, allowed_actions=MENU, pacing={}, generate=praising, model="") is None


@pytest.mark.asyncio
async def test_the_talker_calls_the_configured_model_through_the_facade(ctx_factory) -> None:
    """§1.1: Luna via the existing provider's per-call model override, nothing else."""
    ctx = ctx_factory()
    seen: dict = {}

    async def capture(**kw):
        seen.update(kw)
        return []

    await predict_by_talker(ctx, allowed_actions=MENU, pacing={}, generate=capture, model="gpt-5.6-luna")
    assert seen["model"] == "gpt-5.6-luna"
    assert seen["purpose"] == "interviews.agent.talker"


# ------------------------------------------------------------ live paths


def _turn(modality: str = "VOICE", **extra) -> CandidateTurnInput:
    return CandidateTurnInput(session_id="sess-paths", turn_index=3, modality=modality,
                              text_content="Em dùng Redis làm cache và đặt TTL năm phút cho từng key.", **extra)


@pytest.mark.asyncio
async def test_rule_and_shadow_never_route(session_state_factory, monkeypatch) -> None:
    """Rollout modes are not execution paths and must not be routed."""
    monkeypatch.setattr("src.modules.interviews.agents.interviewer.graph.get_settings",
                        lambda: type("S", (), {"interview_talker_enabled": True})())
    monkeypatch.setattr("src.modules.interviews.agents.interviewer.graph.talker_model", lambda: "gpt-5.6-luna")
    for mode in (interview_agent.MODE_RULE, interview_agent.MODE_SHADOW):
        out = await interview_agent.run_turn(
            InterviewCoreEngine(llm_client=CountingLLM()), _turn(),
            session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=mode,
        )
        rec = out.metadata["decision"]
        assert rec["execution_path"] == mode and rec["route_reason"] == "rollout_mode"
        assert rec["arbiter"] is None


@pytest.mark.asyncio
async def test_fast_path_executes_a_validated_preview_with_zero_new_calls(session_state_factory, monkeypatch) -> None:
    """A preview the server already matched to the final transcript is the decision."""
    probe = "Khi dữ liệu trong database đổi, em xoá key cache theo cách nào?"
    preview = {"action": "acknowledge_and_probe",
               "args": {"kind": "gap", "missing_aspect": "cache invalidation", "probe_text": probe},
               "reason": "từ preview", "evidence_refs": ["answer:sent:1"], "path": ["model"]}
    decider_calls: list[int] = []

    async def must_not_run(*_a, **_k):
        decider_calls.append(1)

    monkeypatch.setattr(interview_agent, "decide_by_model", must_not_run)
    llm = CountingLLM()
    out = await interview_agent.run_turn(
        InterviewCoreEngine(llm_client=llm), _turn(precomputed_decision=preview),
        session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=interview_agent.MODE_LIVE,
    )
    rec = out.metadata["decision"]
    assert rec["execution_path"] == FAST and rec["route_reason"] == "validated_preview"
    assert rec["executed"] == "agent" and rec["agent"]["path"][0] == "preview"
    assert decider_calls == [] and llm.calls == []
    assert out.message_text == probe


@pytest.mark.asyncio
async def test_a_preview_whose_action_left_the_menu_falls_through_to_unified(session_state_factory, monkeypatch) -> None:
    """Menu validation still applies: a preview is checked like any live decision."""
    preview = {"action": "closing_qna", "args": {}, "reason": "stale", "evidence_refs": [], "path": []}
    asked: list[int] = []

    async def thinker(ctx, **_kw):
        asked.append(1)
        return Decision("move_to_next_question", {"reason": "sufficient"}, reason="r", source="agent")

    monkeypatch.setattr(interview_agent, "decide_by_model", thinker)
    out = await interview_agent.run_turn(
        InterviewCoreEngine(llm_client=CountingLLM()), _turn(precomputed_decision=preview),
        session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=interview_agent.MODE_LIVE,
    )
    rec = out.metadata["decision"]
    # closing_qna is not in a DEEP_DIVE menu, so the preview was rejected and
    # the rule ladder decided; the preview never executed.
    assert rec["execution_path"] == FAST
    assert rec["executed"] == "rule"
    assert rec["agent"]["action"] == "closing_qna" and rec["in_menu"] is True


@pytest.mark.asyncio
async def test_hybrid_runs_thinker_and_talker_concurrently_and_speaks_early(session_state_factory, monkeypatch) -> None:
    """AC-06: the two calls overlap; the ack is delivered before the Thinker returns."""
    monkeypatch.setattr("src.modules.interviews.agents.interviewer.graph.get_settings",
                        lambda: type("S", (), {"interview_talker_enabled": True})())
    monkeypatch.setattr(interview_agent, "talker_model", lambda: "gpt-5.6-luna")
    timeline: list[str] = []
    thinker_gate = asyncio.Event()

    async def slow_thinker(ctx, **_kw):
        timeline.append("thinker:start")
        await thinker_gate.wait()
        timeline.append("thinker:end")
        return Decision("acknowledge_and_probe", {"kind": "gap", "missing_aspect": "cache invalidation"},
                        reason="r", evidence_refs=["answer:sent:1"], source="agent", path=["model"])

    async def fast_talker(ctx, **_kw):
        timeline.append("talker:start")
        await asyncio.sleep(0.01)
        timeline.append("talker:end")
        return _talker()

    def early_speech(line: str) -> None:
        spoken.append(line)
        timeline.append("early_speech")
        # Only now may the Thinker return: if early speech had waited for
        # fan-in, this would deadlock and the test would time out.
        thinker_gate.set()

    monkeypatch.setattr(interview_agent, "decide_by_model", slow_thinker)
    monkeypatch.setattr(interview_agent, "predict_by_talker", fast_talker)
    spoken: list[str] = []

    out = await asyncio.wait_for(
        interview_agent.run_turn(
            InterviewCoreEngine(llm_client=CountingLLM()), _turn(),
            session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=interview_agent.MODE_LIVE,
            on_early_speech=early_speech,
        ),
        timeout=5.0,
    )

    assert timeline.index("talker:start") < timeline.index("thinker:end"), "the calls did not overlap"
    assert timeline.index("early_speech") < timeline.index("thinker:end"), "early speech waited for fan-in"
    assert spoken == ["Mình nghe rồi."]
    rec = out.metadata["decision"]
    assert rec["execution_path"] == HYBRID
    assert rec["arbiter"]["talker_reused"] is True and rec["arbiter"]["outcome"] == "reused"
    assert out.message_text.startswith("Khi dữ liệu")


@pytest.mark.asyncio
async def test_hybrid_does_not_wait_for_a_slow_talker_once_the_thinker_has_answered(session_state_factory, monkeypatch) -> None:
    """§6.4: no waiting on Luna at fan-in. AC-07: a slow Talker changes nothing."""
    monkeypatch.setattr("src.modules.interviews.agents.interviewer.graph.get_settings",
                        lambda: type("S", (), {"interview_talker_enabled": True})())
    monkeypatch.setattr(interview_agent, "talker_model", lambda: "gpt-5.6-luna")
    cancelled = asyncio.Event()

    async def thinker(ctx, **_kw):
        return Decision("acknowledge_and_probe", {"kind": "gap", "missing_aspect": "cache invalidation",
                                                  "probe_text": "Khi database đổi, em xử lý cache thế nào?"},
                        reason="r", evidence_refs=["answer:sent:1"], source="agent", path=["model"])

    async def stuck_talker(ctx, **_kw):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(interview_agent, "decide_by_model", thinker)
    monkeypatch.setattr(interview_agent, "predict_by_talker", stuck_talker)
    spoken: list[str] = []

    loop = asyncio.get_running_loop()
    started = loop.time()
    out = await interview_agent.run_turn(
        InterviewCoreEngine(llm_client=CountingLLM()), _turn(),
        session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=interview_agent.MODE_LIVE,
        on_early_speech=spoken.append,
    )
    await asyncio.sleep(0)  # let the cancellation propagate

    assert loop.time() - started < 2.0, "the turn waited for the Talker"
    assert cancelled.is_set()
    assert spoken == []
    rec = out.metadata["decision"]
    assert rec["executed"] == "agent" and rec["arbiter"]["outcome"] == "no_talker"
    assert out.message_text == "Khi database đổi, em xử lý cache thế nào?"


@pytest.mark.asyncio
async def test_a_talker_error_never_costs_the_turn_or_changes_the_action(session_state_factory, monkeypatch) -> None:
    """AC-07: Luna error → Unified result, official response intact."""
    monkeypatch.setattr("src.modules.interviews.agents.interviewer.graph.get_settings",
                        lambda: type("S", (), {"interview_talker_enabled": True})())
    monkeypatch.setattr(interview_agent, "talker_model", lambda: "gpt-5.6-luna")

    async def thinker(ctx, **_kw):
        return Decision("move_to_next_question", {"reason": "sufficient"}, reason="r",
                        evidence_refs=["answer:sent:1"], source="agent", path=["model"])

    async def broken_talker(ctx, **_kw):
        return None   # predict_by_talker swallows its own errors and returns None

    monkeypatch.setattr(interview_agent, "decide_by_model", thinker)
    monkeypatch.setattr(interview_agent, "predict_by_talker", broken_talker)

    out = await interview_agent.run_turn(
        InterviewCoreEngine(llm_client=CountingLLM()), _turn(),
        session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=interview_agent.MODE_LIVE,
    )
    rec = out.metadata["decision"]
    assert rec["execution_path"] == HYBRID and rec["executed"] == "agent"
    assert rec["agent"]["action"] == "move_to_next_question"
    assert rec["arbiter"]["outcome"] == "no_talker"


@pytest.mark.asyncio
async def test_early_speech_fires_when_two_timers_race_and_the_talker_is_ahead(session_state_factory, monkeypatch) -> None:
    """Regression: completion order must be recorded, not inferred.

    The first implementation read "who finished first" off asyncio.wait's
    return set. When both tasks landed in the same loop tick — routine on
    Windows, whose timers resolve at ~15 ms — the set held both, the Thinker
    looked "done first", and a guarded acknowledgment that nothing had yet
    been spoken over was silently dropped. The 4-config harness exposed it;
    the earlier test could not, because it gated the Thinker on an event
    instead of racing two timers. This one races them.
    """
    monkeypatch.setattr("src.modules.interviews.agents.interviewer.graph.get_settings",
                        lambda: type("S", (), {"interview_talker_enabled": True})())
    monkeypatch.setattr(interview_agent, "talker_model", lambda: "gpt-5.6-luna")

    async def thinker(ctx, **_kw):
        await asyncio.sleep(0.03)
        return Decision("acknowledge_and_probe", {"kind": "gap", "missing_aspect": "cache invalidation"},
                        reason="r", evidence_refs=["answer:sent:1"], source="agent", path=["model"])

    async def talker(ctx, **_kw):
        await asyncio.sleep(0.005)
        return _talker()

    monkeypatch.setattr(interview_agent, "decide_by_model", thinker)
    monkeypatch.setattr(interview_agent, "predict_by_talker", talker)
    spoken: list[str] = []

    out = await interview_agent.run_turn(
        InterviewCoreEngine(llm_client=CountingLLM()), _turn(),
        session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=interview_agent.MODE_LIVE,
        on_early_speech=spoken.append,
    )

    assert spoken == ["Mình nghe rồi."], "the ack was available before anything was spoken and must be said"
    assert out.metadata["decision"]["arbiter"]["early_acknowledgment"] == "Mình nghe rồi."


@pytest.mark.asyncio
async def test_cancelling_a_hybrid_turn_from_outside_leaves_no_child_task_running(session_state_factory, monkeypatch) -> None:
    """V2 review: structured cancellation.

    If the turn is cancelled while Thinker and Talker are both in flight,
    neither may outlive it -- a leaked Talker task keeps paying Luna for a
    decision nobody will read.
    """
    monkeypatch.setattr("src.modules.interviews.agents.interviewer.graph.get_settings",
                        lambda: type("S", (), {"interview_talker_enabled": True})())
    monkeypatch.setattr(interview_agent, "talker_model", lambda: "gpt-5.6-luna")
    cancelled: list[str] = []

    def stuck(name):
        async def run(ctx, **_kw):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(name)
                raise
        return run

    monkeypatch.setattr(interview_agent, "decide_by_model", stuck("thinker"))
    monkeypatch.setattr(interview_agent, "predict_by_talker", stuck("talker"))

    turn = asyncio.create_task(interview_agent.run_turn(
        InterviewCoreEngine(llm_client=CountingLLM()), _turn(),
        session_state_factory(stage=InterviewStage.DEEP_DIVE), mode=interview_agent.MODE_LIVE,
    ))
    await asyncio.sleep(0.05)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    await asyncio.sleep(0.01)

    assert sorted(cancelled) == ["talker", "thinker"]
