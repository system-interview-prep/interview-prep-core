# src/modules/interviews/core/conversation/turn_engine.py
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)
from uuid import UUID

from src.core.trace_logging import trace_event
from src.modules.interviews.core.dialogue import speech_checks
from src.modules.interviews.core.session_policy.blueprint import blueprint_from_payload
from src.modules.interviews.core.conversation.inference import (
    INFERENCE_METHODS,
    MissingInference,
    TurnInference,
)
from src.modules.interviews.core.conversation.turn_context import (
    Decision,
    TurnContext,
    build_turn_context,
    output_metadata_base,
    probe_window,
)
from src.modules.interviews.core.guardrails.input import (
    abort_request_decision,
    candidate_requests_system_data,
    system_probe_decision,
)
from src.modules.interviews.core.guardrails.input import says_already_answered
from src.modules.interviews.core.guardrails.output import (
    acknowledgement_rejection_reason,
    announces_process,
    has_lead_in,
    checked_probe,
    clarify_rejection_reason,
    enforce_final_output_guards,
    qna_rejection_reason,
    system_probe_response,
    new_terms,
)
from src.modules.interviews.core.guardrails.common import candidate_block, candidate_data_rule, canary_scope
from src.modules.interviews.core.conversation.turn_rules import (
    MAX_CLARIFIES_PER_QUESTION,
    non_advancing_exhausted,
    follow_up_repeats,
    warm_up_follow_up_repeats,
    warm_up_follow_ups_exhausted,
    DEEPEN_ASPECT_EN,
    DEEPEN_ASPECT_VI,
    EVALUATION_SCHEMA,
    MAX_QNA_QUESTIONS,
    PROHIBITED_LEAK_PATTERNS,
    QNA_BUDGET_SECONDS,
    TIME_THRESHOLDS,
    classify_candidate_intent,
    classify_candidate_question,
    format_job_context,
    is_abort_request,
    is_repeat_request,
    is_skip_request,
    is_yes_no_question,
    normalize_candidate_pronoun,
    pacing_seconds,
    probe_is_sayable,
    validate_probe_text,
)
from src.modules.interviews.core.contracts.interview import (
    CandidateTurnInput,
    InterviewerTurnOutput,
    InterviewStage,
    QuestionItem,
    SessionExitReason,
    TurnAction,
)

__all__ = [
    "InterviewCoreEngine",
    "DEEPEN_ASPECT_EN",
    "DEEPEN_ASPECT_VI",
    "EVALUATION_SCHEMA",
    "MAX_QNA_QUESTIONS",
    "PROHIBITED_LEAK_PATTERNS",
    "QNA_BUDGET_SECONDS",
    "TIME_THRESHOLDS",
    "acknowledgement_rejection_reason",
    "checked_probe",
    "clarify_rejection_reason",
    "classify_candidate_intent",
    "classify_candidate_question",
    "format_job_context",
    "is_abort_request",
    "is_repeat_request",
    "is_skip_request",
    "is_yes_no_question",
    "normalize_candidate_pronoun",
    "pacing_seconds",
    "probe_is_sayable",
    "validate_probe_text",
]
logger = logging.getLogger("InterviewCoreEngine")

# Probe continuation is controlled by answer sufficiency and interview pacing.


class CoreTurnEngine:
    """The interview's rules for one turn, with no idea how a model works.

    Headless and shared by Chat, Voice and Video. It owns the guards, the rule
    ladder, the action handlers and the stage machine — everything that decides
    *what* happens. Whenever a turn needs an answer read or a sentence written
    it calls ``self`` through :class:`TurnInference`, which
    ``agents/interviewer/response_generation.py`` implements.

    That split is the V2 brief §2 boundary: Core depends on no AI provider, no
    LangGraph, no LiveKit, no database and no LangMem, and
    ``test_import_boundaries.py`` enforces it. Build the usable engine from
    ``agents.interviewer.engine.InterviewCoreEngine``, which mixes the two
    halves together; on its own this class answers only the deterministic
    paths and raises a clear error on the rest.
    """

    # Supplied by the inference half; declared so readers (and type checkers)
    # can see what the engine expects to exist.
    _inference: TurnInference = MissingInference()  # type: ignore[assignment]

    def __getattr__(self, name: str) -> Any:
        """Route *only* the declared inference calls to the implementation.

        Reached only for names this class does not define, so a composed engine
        (which defines them itself) never gets here. The allow-list matters:
        answering every unknown name would make ``hasattr`` true for anything,
        which hides a typo'd method behind a stub that raises at await time and
        lets a removed method look like it still exists.
        """
        if name not in INFERENCE_METHODS:
            raise AttributeError(
                f"{type(self).__name__!r} has no attribute {name!r}"
            )
        return getattr(type(self)._inference, name)

    async def handle_turn(
        self,
        turn_input: CandidateTurnInput,
        session_state: dict[str, Any],
    ) -> InterviewerTurnOutput:
        """One turn, with this session's canary code in every prompt (layer 8).

        Each line the model writes is checked for the code by its own guard;
        this is the last net before the reply is shown or spoken.
        """
        with canary_scope(turn_input.session_id) as code:
            output = await self._handle_turn(turn_input, session_state)
        return await self._guards_out(output, session_state, code)

    async def _guards_out(
        self, output: InterviewerTurnOutput, session_state: dict[str, Any], canary_code: str | None
    ) -> InterviewerTurnOutput:
        """Apply the shared output boundary after every action handler."""
        return await enforce_final_output_guards(
            output,
            session_state,
            canary_code,
            generate_line=self._generate_interviewer_line,
        )

    async def _handle_turn(
        self,
        turn_input: CandidateTurnInput,
        session_state: Dict[str, Any],
    ) -> InterviewerTurnOutput:
        """One turn as three steps: guards (code) → decide (rule today) → act.

        - turn_input: dữ liệu ứng viên vừa gửi (text + metadata)
        - session_state: state lấy từ DB (PostgreSQL session + turns)

        The steps are also the nodes of the agent's turn graph
        (``agent/interviewer/interview_agent.py``); this method is the same pipeline without
        LangGraph, for tests, scripts and the Voice Lab.
        """
        ctx = build_turn_context(turn_input, session_state)
        decision = await self._guards_in(ctx)
        if decision is None:
            decision = await self._decide_by_rule(ctx)
        return await self._act(ctx, decision)

    # ----------------- BƯỚC 1: RÀO CHẮN TRƯỚC MODEL ----------------- #

    async def _guards_in(self, ctx: TurnContext) -> Optional[Decision]:
        """Code-only checks that end the turn before any model reads the answer."""
        # CHỐT CHẶN TOÀN CỤC 1: XIN DỪNG PHỎNG VẤN (CONFIRM_ABORT MODAL).
        # In CLOSING the candidate wrapping up ("em hết câu hỏi rồi, mình kết
        # thúc tại đây nhé") is the normal ending, not an abort: let the closing
        # handler complete the session instead of recording USER_ENDED.
        abort = abort_request_decision(ctx)
        if abort is not None:
            speech_checks.record("input", "reject", "abort_request")
            return abort
        ctx.apply_timing()
        # Kiểm tra Hard Timeout toàn phiên (<= 30 giây và chưa ở stage CLOSING)
        if ctx.is_hard_timeout:
            trace_event(
                "interviewer",
                "hard_timeout_triggered",
                session_id=ctx.session_id,
                time_remaining_seconds=ctx.time_remaining_seconds,
                elapsed_seconds=ctx.elapsed_seconds,
            )
            return Decision("close_interview", {"reason": "hard_timeout"}, reason="session clock",
                            evidence_refs=["clock"], path=["#2"])
        system_probe = system_probe_decision(ctx)
        if system_probe is not None:
            speech_checks.record("input", "reject", "system_probe_or_injection")
            trace_event("interviewer", "input_guardrail_triggered", session_id=ctx.session_id,
                        guard="system_probe_or_injection")
            return system_probe
        # XIN ĐỔI CÂU HỎI: a code rule in every mode. It lived only in the rule
        # ladder, so in LIVE the model answered "em có thể chuyển câu hỏi khác
        # không ạ?" with repeat_question (session 0e3c14c6). Same order as the
        # ladder had it: right after the global guards.
        if is_skip_request(ctx.candidate_text):
            ctx.next_stage, ctx.next_question = self._get_next_stage_and_question(ctx.session_state)
            if ctx.next_stage == InterviewStage.CLOSED or ctx.next_question is None:
                return Decision("close_interview", {"reason": "skip_last"}, reason="skip on the last question",
                                evidence_refs=["answer:text"], path=["#3"])
            return Decision("move_to_next_question", {"reason": "candidate_skip"}, reason="skip phrase",
                            evidence_refs=["answer:text"], path=["#3"])
        # "Em đã trình bày rồi": the candidate closes the question themselves.
        # Session 08a2ee58 answered it with a fixed decline and then the same
        # question again. Not a skip -- what they said stays their answer.
        if ctx.current_stage != InterviewStage.CLOSING and says_already_answered(ctx.candidate_text):
            ctx.next_stage, ctx.next_question = self._get_next_stage_and_question(ctx.session_state)
            if ctx.next_stage == InterviewStage.CLOSED or ctx.next_question is None:
                return Decision("close_interview", {"reason": "complete"}, reason="candidate says answered",
                                evidence_refs=["answer:text"], path=["#3b"])
            return Decision("move_to_next_question", {"reason": "candidate_answered"}, reason="candidate says answered",
                            evidence_refs=["answer:text"], path=["#3b"])
        return None

    # ----------------- BƯỚC 2: QUYẾT ĐỊNH THEO LUẬT ----------------- #

    async def _decide_by_rule(self, ctx: TurnContext) -> Decision:
        """The rule ladder: reads the answer where it must, returns one action.

        The order is policy (abort before model, regex before evaluator) and
        is kept exactly; only the output construction moved to ``act_*``.
        """
        state = ctx.session_state
        path: List[str] = []

        # CHỐT CHẶN TOÀN CỤC 2: XIN ĐỔI CÂU HỎI (SKIP_REQUEST)
        if is_skip_request(ctx.candidate_text):
            ctx.next_stage, ctx.next_question = self._get_next_stage_and_question(state)
            if ctx.next_stage == InterviewStage.CLOSED or ctx.next_question is None:
                return Decision("close_interview", {"reason": "skip_last"}, reason="skip on the last question",
                                evidence_refs=["answer:text"], path=["#3"])
            return Decision("move_to_next_question", {"reason": "candidate_skip"}, reason="skip phrase",
                            evidence_refs=["answer:text"], path=["#3"])

        # NẾU ĐANG Ở GIAI ĐOẠN CLOSING (CANDIDATE ASKS AI - REVERSE Q&A):
        if ctx.current_stage == InterviewStage.CLOSING:
            return Decision("closing_qna", reason="closing stage", path=["#4"])

        # The candidate did not hear the question: ask it again word for word.
        # Not an answer, so it is neither evaluated nor added to the answer state.
        stalled = non_advancing_exhausted(ctx)
        if ctx.active_prompt and is_repeat_request(ctx.candidate_text) and not stalled:
            return Decision("repeat_question", reason="repeat phrase", evidence_refs=["answer:text"], path=path + ["#5"])

        ctx.intent = classify_candidate_intent(ctx.candidate_text, ctx.question_prompt)

        # Questions addressed to the interviewer are conversational control,
        # not evidence that the current assessment turn has been answered.
        candidate_question_intent = classify_candidate_question(ctx.candidate_text)
        if candidate_question_intent is not None and not stalled:
            return Decision("answer_candidate_question", {"intent": candidate_question_intent},
                            reason="question to the interviewer", evidence_refs=["answer:text"], path=["#6"])

        # 2. ĐÁNH GIÁ CÂU TRẢ LỜI HIỆN TẠI (TURN EVALUATION)
        ctx.eval_result = await self._evaluate_candidate_response(
            candidate_text=ctx.candidate_text,
            current_stage=ctx.current_stage,
            current_question=ctx.current_question,
            is_vi=ctx.is_vi,
            telemetry=ctx.turn_input.telemetry,
            precomputed_evaluation=ctx.turn_input.precomputed_evaluation,
            recent_lines=ctx.recent_lines,
            seed=ctx.seed,
            memory_hits=ctx.session_state.get("candidate_memory") or (),
        )
        eval_result = ctx.eval_result
        path.append("#7")

        # BẢO HIỂM TẦNG 2: Nếu LLM nhận diện ý định xin dừng phỏng vấn -> Kích hoạt Modal xác nhận
        if eval_result.get("intent") == "CANDIDATE_ABORT":
            return Decision("confirm_abort", {"detected_by": "evaluator"}, reason="evaluator intent",
                            evidence_refs=["evaluation:intent"], path=path + ["#8"])
        # Layer 7: the evaluator recognised an attempt on the system that the
        # patterns missed. Same fixed line as the rule path, no model-written text.
        if eval_result.get("intent") == "SYSTEM_PROBE":
            trace_event("interviewer", "system_probe", source="evaluation")
            return Decision("decline_system_probe", reason="evaluator intent",
                            evidence_refs=["evaluation:intent"], path=path + ["#8"])
        # Off-topic replies are not evidence: redirect without grading them.
        if eval_result.get("intent") == "OFF_TOPIC" and not stalled:
            return Decision("redirect_off_topic", reason="evaluator intent",
                            evidence_refs=["evaluation:intent"], path=path + ["#8"])

        # Abort precedence is resolved above. Clarify is allowed only when neither
        # deterministic nor evaluator intent indicates that the candidate wants to stop.
        if ctx.intent == "CLARIFY_REQUEST" and ctx.current_stage not in (
            InterviewStage.WARM_UP,
            InterviewStage.CLOSING,
        ) and int(ctx.clarify_count or 0) < MAX_CLARIFIES_PER_QUESTION and not stalled:
            return Decision("clarify_question", reason="candidate asked for clarification",
                            evidence_refs=["answer:text"], path=path + ["#9"])

        # Gate 4 Architectural Invariant:
        # Numeric score is telemetry/evaluation-only; it has zero control-flow authority in Gate 4.
        ctx.score_telemetry = float(eval_result.get("score", 6.0))
        # Runtime control flow is driven strictly by semantic signals:
        # candidate intent, answer sufficiency, missing evidence and time budget.
        sufficiency_status = eval_result.get("sufficiency_status")
        if not sufficiency_status:
            sufficiency_status = "SUFFICIENT" if eval_result.get("is_sufficient", True) else "INSUFFICIENT"
        ctx.is_give_up = ctx.intent == "GIVE_UP"
        ctx.sufficiency_status = sufficiency_status
        ctx.is_sufficient = sufficiency_status == "SUFFICIENT"

        # Warm-up follow-ups stay conversational and use the evaluator's first
        # response -- a bounded number of times (blueprint warm_up.max_follow_ups
        # and the stage's time share): past that the interview moves on. One
        # that asks again what this warm-up already asked ends it too.
        if ctx.current_stage == InterviewStage.WARM_UP and not ctx.is_sufficient and not ctx.warm_up_repeat:
            repeat = warm_up_follow_up_repeats(
                str(eval_result.get("probe_question") or ""), ctx.warm_up_follow_ups
            )
            if repeat:
                ctx.warm_up_repeat = repeat
                trace_event("interviewer", "warm_up_follow_up_repeated", source="evaluation", repeat=repeat)
        if (
            ctx.current_stage == InterviewStage.WARM_UP
            and not ctx.is_sufficient
            and not warm_up_follow_ups_exhausted(ctx)
        ):
            return Decision("continue_warm_up", {"missing_aspect": str(eval_result.get("missing_aspect") or "")},
                            reason="warm-up answer still missing a point",
                            evidence_refs=["evaluation:missing_aspect"], path=path + ["#10"])

        # 3. PO-approved Gate 4 counter policy: only explicit GIVE_UP increments
        # the canonical counter. Scores, insufficiency, honest answers, skip and
        # clarification have no authority to increment it.
        if ctx.intent == "GIVE_UP":
            ctx.consecutive_uncooperative += 1
        else:
            ctx.consecutive_uncooperative = 0
        state["consecutive_uncooperative"] = ctx.consecutive_uncooperative

        technical_stages = {InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE}
        if (
            ctx.allow_early_exit
            and ctx.current_stage in technical_stages
            and ctx.consecutive_uncooperative >= ctx.blueprint.technical.fast_fail_after_give_ups
        ):
            return Decision("close_interview", {"reason": "repeated_give_up"}, reason="give-up threshold",
                            evidence_refs=["counter:consecutive_uncooperative"], path=path + ["#11"])

        # 4. ĐIỀU HÒA NHỊP ĐỘ (PACING OFFSET) & QUYẾT ĐỊNH PROBE
        ctx.window = probe_window(ctx)
        should_probe = ctx.window.open and not ctx.is_sufficient
        # Challenge tier (CV–job match from 70%, planning.entry_gate): a good
        # answer to a technical question gets one deeper follow-up, so a strong
        # candidate is stretched instead of moved on at once.
        is_coding_turn = bool(
            ctx.turn_input.telemetry
            and (ctx.turn_input.telemetry.get("is_coding_turn") or ctx.turn_input.telemetry.get("isCodingTurn"))
        )
        ctx.is_deepening_probe = bool(
            ctx.window.open
            and ctx.current_turn_in_question == 0
            and ctx.is_sufficient
            and state.get("practice_tier") == "challenge"
            and ctx.current_stage in (InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE)
            and not is_coding_turn
        )
        if ctx.is_deepening_probe:
            should_probe = True
        # Turn 0 (WARM_UP): Khóa tuyệt đối tính năng probe, luôn tiếp nhận chào hỏi và chuyển tiếp
        if ctx.current_stage == InterviewStage.WARM_UP:
            should_probe = False
            ctx.is_deepening_probe = False
        path.append("#12")

        # 5. XỬ LÝ CHUYỂN BƯỚC FSM
        if should_probe:
            kind = "deepen" if ctx.is_deepening_probe else "gap"
            return Decision(
                "acknowledge_and_probe",
                {"kind": kind, "missing_aspect": str(eval_result.get("missing_aspect") or "")[:300]},
                reason="answer missed a point" if kind == "gap" else "challenge tier, good answer",
                evidence_refs=["evaluation:missing_aspect" if kind == "gap" else "practice_tier"],
                path=path + ["#13a"],
            )

        # NẾU KHÔNG PROBE: CHUYỂN CÂU HỎI TIẾP THEO HOẶC ĐỔI STAGE
        ctx.next_stage, ctx.next_question = self._get_next_stage_and_question(state)
        if ctx.next_stage == InterviewStage.CLOSED or ctx.next_question is None:
            return Decision("close_interview", {"reason": "complete"}, reason="no question left",
                            evidence_refs=["queue:empty"], path=path + ["#13b"])
        if ctx.current_stage == InterviewStage.WARM_UP:
            reason = (
                "warm_up_done" if ctx.is_sufficient else "warm_up_incomplete"
            )
        elif ctx.is_sufficient:
            reason = "sufficient"
        else:
            closed = [name for name, hit in ctx.window.reasons.items() if hit]
            reason = f"budget:{closed[0]}" if closed else "no_probe"
        return Decision("move_to_next_question", {"reason": reason}, reason=reason,
                        evidence_refs=["evaluation:sufficiency"], path=path + ["#13b"])

    # ----------------- BƯỚC 3: THỰC THI ----------------- #

    async def _act(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        """Run the chosen action; the mapping is the closed set in ``turn_context.ACTIONS``."""
        handlers = {
            "confirm_abort": self.act_confirm_abort,
            "close_interview": self.act_close_interview,
            "move_to_next_question": self.act_move_to_next_question,
            "repeat_question": self.act_repeat_question,
            "answer_candidate_question": self.act_answer_candidate_question,
            "decline_system_probe": self.act_decline_system_probe,
            "redirect_off_topic": self.act_redirect_off_topic,
            "clarify_question": self.act_clarify_question,
            "continue_warm_up": self.act_continue_warm_up,
            "acknowledge_and_probe": self.act_acknowledge_and_probe,
            "closing_qna": self.act_closing_qna,
        }
        handler = handlers.get(decision.action)
        if handler is None:
            raise ValueError(f"unknown turn action: {decision.action}")
        return await handler(ctx, decision)

    async def act_confirm_abort(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        confirm_msg = await self._generate_interviewer_line(
            purpose="confirm_abort",
            context=f"Ứng viên vừa nói: {ctx.candidate_text}\nHỏi họ có muốn kết thúc buổi phỏng vấn không.",
            is_vi=ctx.is_vi,
        )
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=confirm_msg,
            action=TurnAction.CONFIRM_ABORT,
            current_stage=ctx.current_stage,
            is_session_finished=False,
            metadata={
                "requires_abort_confirmation": True,
                "candidate_abort_intent": True,
                "consecutive_uncooperative": 0,
                "consecutive_fails": 0,  # Deprecated compatibility alias.
            },
        )

    async def act_close_interview(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        reason = str(decision.args.get("reason") or "complete")
        if reason == "hard_timeout":
            timeout_msg = await self._generate_interviewer_line(
                purpose="hard_timeout_close",
                context=(f"Buổi phỏng vấn đã hết thời gian ở giai đoạn {ctx.current_stage.value}. "
                         "Thông báo ngắn gọn rằng buổi dừng tại đây."),
                is_vi=ctx.is_vi,
            )
            return self._terminate_session(
                session_id=ctx.session_id, turn_index=ctx.next_turn_index,
                reason=SessionExitReason.HARD_TIMEOUT, message=timeout_msg,
            )
        if reason == "skip_last":
            close_msg = await self._generate_interviewer_line(
                purpose="complete_after_skip",
                context="Ứng viên xin bỏ qua câu cuối. Xác nhận ngắn gọn rằng buổi phỏng vấn đã hoàn tất.",
                is_vi=ctx.is_vi,
            )
            return self._terminate_session(
                session_id=ctx.session_id, turn_index=ctx.next_turn_index,
                reason=SessionExitReason.NORMAL_COMPLETION, message=close_msg,
            )
        if reason == "repeated_give_up":
            uncoop_msg = await self._generate_interviewer_line(
                purpose="end_after_repeated_give_up",
                context="Ứng viên đã nhiều lần cho biết không thể trả lời. Thông báo tôn trọng rằng buổi sẽ kết thúc sớm.",
                is_vi=ctx.is_vi,
            )
            return self._terminate_session(
                session_id=ctx.session_id, turn_index=ctx.next_turn_index,
                reason=SessionExitReason.FAST_FAIL_TECH, message=uncoop_msg,
                metadata={
                    "consecutive_uncooperative": ctx.consecutive_uncooperative,
                    "consecutive_fails": ctx.consecutive_uncooperative,
                },
            )
        close_msg = await self._generate_interviewer_line(
            purpose="complete_interview",
            context="Ứng viên vừa hoàn thành câu cuối. Khép lại buổi phỏng vấn một cách tự nhiên, không đưa ra điểm số.",
            is_vi=ctx.is_vi,
        )
        return self._terminate_session(
            session_id=ctx.session_id, turn_index=ctx.next_turn_index,
            reason=SessionExitReason.NORMAL_COMPLETION, message=close_msg,
        )

    async def act_closing_qna(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        return await self._handle_closing_turn(
            turn_input=ctx.turn_input,
            session_state=ctx.session_state,
            is_vi=ctx.is_vi,
            time_remaining_seconds=ctx.time_remaining_seconds,
        )

    async def act_repeat_question(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=ctx.heard_prompt,
            action=TurnAction.CLARIFY,
            current_stage=ctx.current_stage,
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                "repeat_request": True,
                "turn_in_question": ctx.current_turn_in_question,
                "sufficiency_status": "NOT_ASSESSED",
            },
        )

    async def act_answer_candidate_question(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        state = ctx.session_state
        candidate_question_intent = decision.args.get("intent")
        # Unified Thinker (§6.2): already past qna_rejection_reason, which keeps
        # the answer inside the posting, when the decider supplied it.
        reply = str(decision.args.get("answer_text") or "") or await self._generate_interviewer_line(
            purpose=f"candidate_question_{candidate_question_intent}",
            context=(
                f"Ứng viên hỏi: {ctx.candidate_text}\n"
                f"Vị trí: {state.get('job_title') or 'chưa rõ'}\n"
                f"Thông tin JD: {state.get('job_description_text') or state.get('job_context') or 'không có'}\n"
                f"Câu phỏng vấn đang chờ: {ctx.active_prompt}\n"
                "Chỉ trả lời dựa trên thông tin đã biết. Nếu thiếu dữ kiện, nói rõ chưa có thông tin "
                "và nhắc lại câu phỏng vấn đang chờ."
            ),
            is_vi=ctx.is_vi,
        )
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=reply,
            action=TurnAction.CLARIFY,
            current_stage=ctx.current_stage,
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                "candidate_question_intent": candidate_question_intent,
                "turn_in_question": state.get("current_turn_in_question", 0),
                "sufficiency_status": "NOT_ASSESSED",
            },
        )

    async def act_decline_system_probe(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        reply = system_probe_response(is_vi=ctx.is_vi, active_prompt=ctx.active_prompt)
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=reply,
            action=TurnAction.CLARIFY,
            current_stage=ctx.current_stage,
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                "candidate_question_intent": "SYSTEM_PROBE",
                "system_probe_source": decision.args.get("detected_by") or "evaluation",
                "turn_in_question": ctx.current_turn_in_question,
                "sufficiency_status": "NOT_ASSESSED",
            },
        )

    async def act_redirect_off_topic(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        # Unified Thinker (§6.2): already past the acknowledgement guard in the
        # decider when present, so no second call is needed.
        reply = str(decision.args.get("redirect_text") or "") or await self._generate_interviewer_line(
            purpose="redirect_off_topic_answer",
            context=f"Ứng viên vừa nói: {ctx.candidate_text}\nNhắc nhẹ rồi quay lại câu hỏi: {ctx.active_prompt}",
            is_vi=ctx.is_vi,
        )
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=reply,
            action=TurnAction.CLARIFY,
            current_stage=ctx.current_stage,
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                "candidate_question_intent": "OFF_TOPIC",
                "turn_in_question": ctx.current_turn_in_question,
                "sufficiency_status": "NOT_ASSESSED",
            },
        )

    async def act_clarify_question(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        # Unified Thinker (§6.2): the decider may carry the line it chose to
        # say, already past clarify_rejection_reason. Otherwise generate it.
        clarify_msg = str(decision.args.get("clarify_text") or "")
        if not clarify_msg:
            try:
                clarify_msg = await self._generate_clarify_text(
                    current_question=ctx.current_question,
                    candidate_text=ctx.candidate_text,
                    is_vi=ctx.is_vi,
                    memory_hits=ctx.session_state.get("candidate_memory") or (),
                )
            except Exception as exc:  # noqa: BLE001 - a rejected line must not end the session
                # Session 2678ce7b: one clarification rejected as "evaluative"
                # raised out of the turn, the voice session closed as a
                # technical failure and no report was produced. Saying the
                # pending question again, as heard, is always safe.
                trace_event("interviewer", "clarify_fallback_repeat", error_type=type(exc).__name__,
                            error_message=str(exc)[:200], session_id=str(ctx.session_id))
                clarify_msg = ctx.heard_prompt or ctx.question_prompt
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=clarify_msg,
            action=TurnAction.CLARIFY,
            current_stage=ctx.current_stage,
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                "consecutive_uncooperative": 0,
                "consecutive_fails": 0,  # Deprecated compatibility alias.
                "turn_in_question": ctx.current_turn_in_question,
                "clarify_used": True,
                "sufficiency_status": "AMBIGUOUS",
                "generated_by": "llm",
            },
        )

    async def act_continue_warm_up(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        eval_result = ctx.eval_result
        if decision.source == "agent":
            # In live mode the decider owns both the action and the exact line
            # the candidate will hear. It has the accumulated answer and recent
            # interviewer messages in its input; do not make a recovery LLM call.
            continuation = str(decision.args.get("follow_up_text") or "").strip()
            speech_checks.record("probe", "pass", "warmup_from_agent")
            speech_plan = [{"act": "QUESTION", "source": "agent_decision"}]
        else:
            # Rule/shadow and live-error fallback can use the evaluator's
            # already-produced utterance, but must not start another LLM call.
            continuation = checked_probe(
                str(eval_result.get("probe_question") or ""),
                question=ctx.question_prompt,
                answer=ctx.candidate_text,
                rubric="",
            )
            if continuation:
                speech_checks.record("probe", "pass", "warmup_from_evaluation")
                speech_plan = [{"act": "QUESTION", "source": "llm_evaluation"}]
            else:
                # Last-resort rule response when the evaluator did not provide
                # a usable question. This is a single complete utterance, not a
                # second model-generated recovery.
                missing = str(eval_result.get("missing_aspect") or "").strip()
                if ctx.is_vi:
                    continuation = (
                        f"Cảm ơn bạn đã chia sẻ. Bạn có thể nói thêm về {missing} không?"
                        if missing
                        else "Cảm ơn bạn đã chia sẻ. Bạn muốn bổ sung thêm điều gì về phần giới thiệu của mình không?"
                    )
                else:
                    continuation = (
                        f"Thanks for sharing. Could you tell me a little more about {missing}?"
                        if missing
                        else "Thanks for sharing. Is there anything else you would like to add to your introduction?"
                    )
                speech_checks.record("probe", "pass", "warmup_rule_fallback")
                speech_plan = [{"act": "QUESTION", "source": "rule_fallback"}]
        trace_event(
            "interviewer",
            "warmup_continuation_invited",
            session_id=ctx.session_id,
            answer_chars=len(ctx.candidate_text),
            intent=ctx.intent,
            missing_aspect=str(eval_result.get("missing_aspect") or "")[:200],
            question_source=speech_plan[0]["source"],
        )
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=continuation,
            action=TurnAction.CLARIFY,
            current_stage=InterviewStage.WARM_UP,
            current_competency=ctx.current_question.get("competency") or "Giới thiệu & Khởi động",
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                "warmup_continuation": True,
                "turn_in_question": ctx.current_turn_in_question,
                "sufficiency_status": "INSUFFICIENT",
                "intent": ctx.intent,
                "ack_source": "none",
                "missing_aspect": eval_result.get("missing_aspect") or decision.args.get("missing_aspect"),
                "probe_generated_in_evaluation": speech_plan[0]["source"] == "llm_evaluation",
                "speech_plan": speech_plan,
            },
        )

    async def act_acknowledge_and_probe(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        eval_result = ctx.eval_result
        # In live agent mode the decision carries the missing point directly;
        # legacy rule/shadow mode continues to use the evaluator result.
        missing_aspect = str(
            decision.args.get("missing_aspect") or eval_result.get("missing_aspect") or ""
        )
        ctx.is_deepening_probe = decision.args.get("kind") == "deepen"
        window = ctx.window or probe_window(ctx)
        # Unified Thinker (V2 spec §6.2): a live decision may carry the probe it
        # chose to ask, already through the output guards in ``decide``. That is
        # the whole turn in one inference. It wins over the evaluator's probe
        # because it was written against the action the model actually picked.
        unified_probe = str(decision.args.get("probe_text") or "")
        # The evaluator can return the one allowed probe in the same JSON
        # response. This removes a second sequential model round-trip from
        # the latency-critical voice path while preserving the existing guard.
        # Generate a fresh probe when neither path provided a valid one.
        probe_question = unified_probe or (
            ""
            if ctx.is_deepening_probe  # the evaluator's probe targets a gap; there is none
            else checked_probe(
                str(eval_result.get("probe_question") or eval_result.get("reply_text") or ""),
                question=ctx.active_prompt,
                answer=ctx.candidate_text,
                rubric=str(ctx.current_question.get("rubric_criteria") or ""),
            )
            or ""
        )
        # Kept for the metadata below: "the probe came with the model's answer
        # rather than from a second call", whichever of the two paths supplied it.
        probe_from_evaluation = bool(probe_question)
        if unified_probe:
            speech_checks.record("probe", "pass", "from_decision")
        elif probe_from_evaluation:
            speech_checks.record("probe", "pass", "from_evaluation")
        else:
            try:
                probe_question = await self._generate_probe_question(
                    candidate_text=ctx.candidate_text,
                    current_question=ctx.current_question,
                    is_vi=ctx.is_vi,
                    current_stage=ctx.current_stage,
                    telemetry=ctx.turn_input.telemetry,
                    missing_aspect=(
                        # The decider's own point stays the subject of a deepen
                        # probe; the generic aspect is only the how.
                        (
                            f"{missing_aspect}\n{DEEPEN_ASPECT_VI if ctx.is_vi else DEEPEN_ASPECT_EN}"
                            if decision.source == "agent" and missing_aspect
                            else (DEEPEN_ASPECT_VI if ctx.is_vi else DEEPEN_ASPECT_EN)
                        )
                        if ctx.is_deepening_probe
                        else missing_aspect
                    ),
                    seed=ctx.seed,
                    recent_lines=ctx.recent_lines,
                    memory_hits=ctx.session_state.get("candidate_memory") or (),
                )
            except Exception as exc:  # noqa: BLE001 - a rejected probe must not end the session
                # No probe passed its guard: the question moves on instead of
                # failing the turn (session 2678ce7b lost its whole report to a
                # rejected clarification raised the same way).
                trace_event("interviewer", "probe_withdrawn", reason="generation_failed",
                            error_type=type(exc).__name__, session_id=str(ctx.session_id))
                return await self.act_move_to_next_question(
                    ctx, Decision("move_to_next_question", {"reason": "no_probe"}, reason="probe generation failed")
                )
        # A follow-up that asks again what an earlier follow-up of this
        # question asked is the loop of session 2678ce7b (the Redis-failure
        # question twice, reworded): the candidate already answered it, so
        # the question moves on.
        repeat = follow_up_repeats(probe_question, ctx.question_follow_ups)
        if repeat:
            trace_event("interviewer", "probe_withdrawn", reason="repeated_follow_up", repeat=repeat,
                        text=probe_question[:160], session_id=str(ctx.session_id))
            return await self.act_move_to_next_question(
                ctx, Decision("move_to_next_question", {"reason": "repeated_probe"}, reason=f"repeats {repeat}")
            )
        probe_source = "llm"
        probe_speech = probe_question
        speech_plan = [{"act": "QUESTION", "source": probe_source}]
        probe_terms = (
            new_terms(
                probe_question,
                ctx.question_prompt,
                ctx.candidate_text,
                missing_aspect,
            )
            if probe_source == "llm"
            else []
        )
        if probe_terms:
            trace_event(
                "interviewer", "speech_new_terms", kind="probe", terms=probe_terms, session_id=ctx.session_id
            )
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=probe_speech,
            action=TurnAction.PROBE,
            current_stage=ctx.current_stage,
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                **output_metadata_base(ctx),
                "turn_in_question": ctx.current_turn_in_question + 1,
                "pre_score": ctx.score_telemetry,
                "sufficiency_status": ctx.sufficiency_status,
                "hard_answer_timeout": window.is_hard_answer_timeout,
                "probe_generated_in_evaluation": probe_from_evaluation,
                # gap: the answer missed something; deepen: challenge tier, answer was sufficient.
                "probe_kind": "deepen" if ctx.is_deepening_probe else "gap",
                "intent": eval_result.get("intent"),
                # Persisted on the PROBE message: an LLM-written follow-up is
                # the one interview question that never came from the bank.
                "generated_by": "llm",
                "missing_aspect": str(eval_result.get("missing_aspect") or "")[:300],
                "acknowledgement": "",
                "speech_plan": speech_plan,
                "new_terms": probe_terms,
                "ack_source": "none",
                "ack_reason": eval_result.get("ack_reason"),
                "ack_raw": eval_result.get("ack_raw"),
            },
        )

    async def act_move_to_next_question(self, ctx: TurnContext, decision: Decision) -> InterviewerTurnOutput:
        state = ctx.session_state
        next_stage, next_question = ctx.next_stage, ctx.next_question
        if next_question is None:  # a decider that skipped the ladder
            next_stage, next_question = self._get_next_stage_and_question(state)
            ctx.next_stage, ctx.next_question = next_stage, next_question
        if next_stage == InterviewStage.CLOSED or next_question is None:
            return await self.act_close_interview(ctx, Decision("close_interview", {"reason": "complete"}))

        if decision.args.get("reason") == "candidate_skip":
            return InterviewerTurnOutput(
                session_id=ctx.session_id,
                turn_index=ctx.next_turn_index,
                message_text=next_question.spoken_prompt or next_question.main_prompt,
                action=TurnAction.NEXT_QUESTION,
                current_stage=next_stage,
                current_competency=next_question.competency,
                time_remaining_seconds=ctx.time_remaining_seconds,
                metadata={
                    "skipped_turn": True,
                    "transition": "",
                    "speech_plan": [{"act": "QUESTION", "source": "bank"}],
                    "turn_in_question": 0,
                    "sufficiency_status": "INSUFFICIENT",
                    "consecutive_uncooperative": 0,
                    "consecutive_fails": 0,  # Deprecated compatibility alias.
                },
            )

        eval_result = ctx.eval_result
        window = ctx.window or probe_window(ctx)
        interviewer_message = next_question.spoken_prompt or next_question.main_prompt
        speech_plan = [{
            "act": "QUESTION",
            "source": "pregenerated" if next_question.spoken_prompt else "bank",
        }]
        # Moving on: the decider may have written one bridging sentence in
        # the same inference (no extra call) that closes the thread it was on.
        # It goes through the acknowledgement guard, may not narrate the
        # process, is dropped when the question already opens with its own
        # lead-in, and precedes the frozen question as its own paragraph, so
        # the question is still spoken verbatim (and still plays from the
        # prefetch cache). Session 0e3c14c6: question after question with no
        # link at all, "like two different interviews".
        transition = str(decision.args.get("transition_text") or "").strip() if decision.source == "agent" else ""
        if transition:
            reason = acknowledgement_rejection_reason(
                transition, recent=ctx.recent_lines, answer=ctx.candidate_text
            ) or ("announces_process" if announces_process(transition) else None) or (
                "question_has_lead_in" if has_lead_in(interviewer_message) else None
            )
            if reason:
                speech_checks.record("ack", "reject", reason, transition)
                trace_event("interviewer", "transition_rejected", reason=reason, text=transition[:160])
                transition = ""
            else:
                speech_checks.record("ack", "pass", "transition")
        if transition:
            interviewer_message = f"{transition}\n\n{interviewer_message}"
            speech_plan = [{"act": "TRANSITION", "source": "agent_decision"}, *speech_plan]
        return InterviewerTurnOutput(
            session_id=ctx.session_id,
            turn_index=ctx.next_turn_index,
            message_text=interviewer_message,
            action=TurnAction.NEXT_QUESTION,
            current_stage=next_stage,
            current_competency=next_question.competency if next_question else None,
            time_remaining_seconds=ctx.time_remaining_seconds,
            metadata={
                **output_metadata_base(ctx),
                "turn_in_question": 0,
                "question_id": next_question.question_id if next_question else None,
                "pre_score": ctx.score_telemetry,
                "sufficiency_status": ctx.sufficiency_status,
                "hard_answer_timeout": window.is_hard_answer_timeout,
                "acknowledgement": "",
                "transition": transition,
                "speech_plan": speech_plan,
                "intent": eval_result.get("intent"),
                "ack_source": "none",
                "ack_reason": eval_result.get("ack_reason"),
                "ack_raw": eval_result.get("ack_raw"),
            },
        )

    # ----------------- CÁC HÀM BỔ TRỢ (HELPER METHODS) ----------------- #

    async def _handle_closing_turn(
        self,
        turn_input: CandidateTurnInput,
        session_state: Dict[str, Any],
        is_vi: bool,
        time_remaining_seconds: int,
    ) -> InterviewerTurnOutput:
        """Xử lý vòng Q&A ngược: ứng viên đặt câu hỏi cho phỏng vấn viên về công ty/văn hóa/dự án."""
        session_id = session_state.get("session_id", turn_input.session_id)
        candidate_text = turn_input.text_content.strip()
        job_title = session_state.get("job_title") or "Vị trí ứng tuyển"

        no_questions_patterns = [
            r"\b(không|k|hết|chưa)\b.*?\b(câu hỏi|thắc mắc|gì nữa|hỏi gì|hỏi thêm)\b",
            r"\b(cảm ơn|thank you|thanks)\b.*?\b(nhiều|bạn|anh|chị)?\b",
            r"\b(nắm rõ|hiểu rõ|đầy đủ|rõ rồi|oke|ok|dạ rồi)\b",
            r"\b(kết thúc|dừng|kết thúc tại đây)\b",
            r"\b(no|none|nothing)\b.*?\b(more\s+)?(questions?|else)\b",
            r"\b(end|finish|wrap up|stop)\b.*?\b(interview|here|now)\b",
            r"^(không|không ạ|dạ không|k ạ|hết rồi|dạ rõ rồi|ok|oke)$",
        ]
        # A message that asks something ("No worries, one question: ...?",
        # "Công ty có OT không? Cảm ơn ạ") is a question, even if it also
        # contains a polite closing phrase.
        has_no_more_q = "?" not in candidate_text and any(
            re.search(pat, candidate_text, re.IGNORECASE) for pat in no_questions_patterns
        )

        session_seconds = int(session_state.get("target_duration_minutes", 25) * 60)
        qna = session_state.get("closing_qna") or {}
        questions_asked = int(qna.get("questions_asked") or 1)
        qna_elapsed = int(qna.get("elapsed_seconds") or 0)
        # The Q&A used to run until the candidate stopped asking or the session
        # was 90s from its end, so it could take most of a session that
        # finished its assessment early. This is the last question we answer.
        is_demo = bool(session_state.get("is_demo"))
        # MAX_QNA_QUESTIONS / QNA_BUDGET_SECONDS are the default blueprint's values.
        blueprint = blueprint_from_payload(session_state.get("blueprint"))
        cutoff = blueprint.pacing.emergency_cutoff_seconds
        is_last_question = questions_asked >= blueprint.closing.max_questions or (
            not is_demo and qna_elapsed >= pacing_seconds(blueprint.closing.budget_seconds, session_seconds)
        )
        closing_seed = f"{session_id}:{turn_input.turn_index}"
        if has_no_more_q or (not is_demo and time_remaining_seconds <= pacing_seconds(cutoff, session_seconds)):
            farewell = await self._generate_interviewer_line(
                purpose="close_candidate_qna",
                context=f"Ứng viên đã kết thúc phần hỏi đáp: {candidate_text}. Hãy khép lại buổi phỏng vấn.",
                is_vi=is_vi,
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.NORMAL_COMPLETION,
                message=farewell,
            )

        job_context = format_job_context(session_state.get("job_context"), is_vi)
        company = (session_state.get("job_context") or {}).get("company_name") or (
            "nhà tuyển dụng" if is_vi else "the employer"
        )
        remaining_minutes = max(1, time_remaining_seconds // 60)
        answer_text, answer_generated_by, qna_reason = await self._answer_closing_question(
            candidate_text=candidate_text,
            job_title=job_title,
            company=company,
            job_context=job_context,
            is_vi=is_vi,
            remaining_minutes=remaining_minutes,
            is_last_question=is_last_question,
            seed=closing_seed,
        )
        if is_last_question:
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.NORMAL_COMPLETION,
                message=answer_text,
            )
        return InterviewerTurnOutput(
            session_id=session_id,
            turn_index=turn_input.turn_index + 1,
            message_text=answer_text,
            action=TurnAction.NEXT_QUESTION,
            current_stage=InterviewStage.CLOSING,
            current_competency="Hỏi đáp & Văn hóa doanh nghiệp",
            time_remaining_seconds=time_remaining_seconds,
            metadata={
                "is_qna_followup": True,
                "turn_in_question": 1,
                "generated_by": answer_generated_by,
                "qna_rejection_reason": qna_reason,
            },
        )


    def _terminate_session(
        self,
        session_id: UUID | str,
        turn_index: int,
        reason: SessionExitReason,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> InterviewerTurnOutput:
        return InterviewerTurnOutput(
            session_id=session_id,
            turn_index=turn_index,
            message_text=message,
            action=TurnAction.TERMINATE,
            current_stage=InterviewStage.CLOSED,
            is_session_finished=True,
            exit_reason=reason,
            time_remaining_seconds=0,
            metadata=metadata or {},
        )

    def _get_next_stage_and_question(
        self,
        session_state: Dict[str, Any],
    ) -> Tuple[InterviewStage, Optional[QuestionItem]]:
        """Lấy câu hỏi tiếp theo từ Question Pool theo Dynamic Time-Budgeted Pacing.

        Pacing Precedence Hierarchy:
        1. Cutoff: time_remaining_seconds <= 90 -> stop issuing new questions.
        2. Behavioral reserve: transition only when a valid frozen Behavioral turn exists.
        3. Frozen queue availability and legacy ratio heuristics.
        4. Closing is reachable only after the Behavioral stage completes with >= 180s remaining.
        """
        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
        questions_pool = session_state.get("questions_pool", {})
        asked_ids = set(str(qid) for qid in session_state.get("asked_question_ids", []))
        answered_ids = set(str(qid) for qid in session_state.get("answered_question_ids", []))
        if not answered_ids and asked_ids:
            answered_ids = set(asked_ids)
        curr_qid = str((session_state.get("current_question_context") or {}).get("question_id") or "")
        if curr_qid:
            asked_ids.add(curr_qid)
            answered_ids.add(curr_qid)

        is_vi = (session_state.get("locale") or "vi").lower().startswith("vi")

        # 1. Tính toán ngân sách thời gian thực
        target_duration_minutes = session_state.get("target_duration_minutes", 25)
        target_duration_seconds = max(60, target_duration_minutes * 60)
        now = datetime.now(timezone.utc)
        started_at = session_state.get("started_at") or now
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except Exception:
                started_at = now
        if started_at is None:
            started_at = now
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        if session_state.get("remaining_time") is not None:
            time_remaining_seconds = max(0, int(session_state["remaining_time"]))
            elapsed_seconds = max(0, target_duration_seconds - time_remaining_seconds)
        elif session_state.get("elapsed_time") is not None and session_state.get("elapsed_time") > 0:
            elapsed_seconds = int(session_state["elapsed_time"])
            time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)
        else:
            elapsed_seconds = max(0, int((now - started_at).total_seconds()))
            time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)
        elapsed_ratio = min(1.0, elapsed_seconds / target_duration_seconds)

        blueprint = blueprint_from_payload(session_state.get("blueprint"))
        behavioral_required = blueprint.behavioral_required
        closing_reserve_seconds = session_state.get(
            "closing_reserve_seconds", pacing_seconds(blueprint.closing.reserve_seconds, target_duration_seconds)
        )
        behavioral_reserve_seconds = session_state.get(
            "behavioral_reserve_seconds", pacing_seconds(blueprint.behavioral.reserve_seconds, target_duration_seconds)
        )
        emergency_cutoff_seconds = pacing_seconds(blueprint.pacing.emergency_cutoff_seconds, target_duration_seconds)
        closing_min_seconds = pacing_seconds(blueprint.closing.min_seconds_to_open, target_duration_seconds)
        stage_until_ratio = {
            InterviewStage.VALIDATE: blueprint.validate_cv.until_ratio,
            InterviewStage.DEEP_DIVE: blueprint.technical.deep_dive_until_ratio,
            InterviewStage.CHALLENGE: blueprint.technical.challenge_until_ratio,
        }

        def as_valid_frozen_question(st: InterviewStage, q: Dict[str, Any]) -> Optional[QuestionItem]:
            """Return a well-formed frozen turn only when it belongs to the requested stage."""
            qid = str(q.get("question_id") or "")
            qvid = str(q.get("question_version_id") or "")
            if not (qid or qvid) or not str(q.get("main_prompt") or "").strip():
                return None
            try:
                declared_stage = InterviewStage(q.get("stage", st.value))
                if declared_stage != st:
                    return None
                q_data = dict(q)
                q_data["stage"] = st
                return QuestionItem(**q_data)
            except (TypeError, ValueError):
                return None

        # Helper lấy câu hỏi chưa hỏi, hợp lệ trong đúng stage của frozen queue.
        def pop_from_stage(st: InterviewStage) -> Optional[QuestionItem]:
            stage_qs = list(questions_pool.get(st.value, []))
            if blueprint.contract.coverage_target == "job_must_haves":
                targets = {
                    str(target.get("conceptId") or ""): target
                    for target in session_state.get("contract_targets", []) or []
                }

                def coverage_rank(question: Dict[str, Any]) -> int:
                    target = targets.get(str(question.get("concept_id") or ""), {})
                    priorities = (target.get("rationale") or {}).get("priorities") or []
                    return 0 if any("must" in str(value).lower() for value in priorities) else 1

                stage_qs.sort(key=coverage_rank)
            for q in stage_qs:
                qid = str(q.get("question_id") or "")
                qvid = str(q.get("question_version_id") or "")
                if (qid and qid in asked_ids) or (qvid and qvid in asked_ids):
                    continue
                question = as_valid_frozen_question(st, q)
                if question:
                    return question
            return None

        def has_pending_technical() -> bool:
            return (
                pop_from_stage(InterviewStage.VALIDATE) is not None
                or pop_from_stage(InterviewStage.DEEP_DIVE) is not None
                or pop_from_stage(InterviewStage.CHALLENGE) is not None
            )

        # A demo is turn-driven: no time cutoff, reserve jump or closing minimum,
        # so it walks every frozen stage (its hard timeout is the overtime grace).
        is_demo = bool(session_state.get("is_demo"))

        def open_closing() -> Tuple[InterviewStage, Optional[QuestionItem]]:
            """The candidate's Q&A once assessment is over; the blueprint may skip it."""
            if not blueprint.closing.qna_enabled:
                return InterviewStage.CLOSED, None
            # Closing still requires closing_min_seconds (default 180, scaled down
            # for sessions shorter than 25 minutes).
            if is_demo or time_remaining_seconds >= closing_min_seconds:
                frozen_closing = pop_from_stage(InterviewStage.CLOSING)
                if frozen_closing:
                    return InterviewStage.CLOSING, frozen_closing
                # The limit is said the way an interviewer would; a "(tối đa 2 câu)"
                # aside was read aloud verbatim in the voice room. The chat room
                # states the exact limit in its stage tip.
                prompt = (
                    "Mình hỏi xong rồi. Bạn có muốn hỏi mình một, hai câu về công việc, "
                    "team hay công ty không?"
                    if is_vi
                    else "That's all from me. Would you like to ask me a question or two "
                    "about the role, the team or the company?"
                )
                return InterviewStage.CLOSING, QuestionItem(
                    question_id="closing-candidate-qna",
                    stage=InterviewStage.CLOSING,
                    competency="Hỏi đáp & Văn hóa doanh nghiệp",
                    main_prompt=prompt,
                )
            return InterviewStage.CLOSED, None

        # 2. Emergency turn cutoff: do not issue a new turn with <= 90 seconds
        # left (scaled down for sessions shorter than 25 minutes).
        if not is_demo and time_remaining_seconds <= emergency_cutoff_seconds:
            trace_event(
                "interviewer",
                "closing_pacing_triggered",
                time_remaining_seconds=time_remaining_seconds,
                current_stage=current_stage.value,
            )
            # The behavioral (STAR) question is a required part of every
            # interview. A long technical answer used to land inside the cutoff
            # and close the session without ever asking it; ask it now while the
            # session is still above its hard timeout.
            if current_stage not in (InterviewStage.BEHAVIORAL, InterviewStage.CLOSING) and (
                time_remaining_seconds > pacing_seconds(30, target_duration_seconds)
            ):
                q_beh = pop_from_stage(InterviewStage.BEHAVIORAL)
                if q_beh:
                    return InterviewStage.BEHAVIORAL, q_beh
            return InterviewStage.CLOSED, None

        # 2b. Behavioral reserve may advance only to a valid frozen Behavioral turn.
        if (
            not is_demo
            and time_remaining_seconds <= (closing_reserve_seconds + behavioral_reserve_seconds)
            and current_stage in [InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE]
        ):
            trace_event(
                "interviewer",
                "behavioral_reserve_protection_triggered",
                time_remaining_seconds=time_remaining_seconds,
                current_stage=current_stage.value,
            )
            q_beh = pop_from_stage(InterviewStage.BEHAVIORAL)
            if q_beh:
                return InterviewStage.BEHAVIORAL, q_beh
            # No Behavioral turn exists: continue the remaining assessment queue.

        # 3. Điều phối theo Tỷ lệ Thời gian Thực (Dynamic Pacing Controller)
        if current_stage == InterviewStage.WARM_UP:
            # Warm-up hoàn thành 1 lượt -> Luôn chuyển sang VALIDATE
            q = pop_from_stage(InterviewStage.VALIDATE)
            if q:
                return InterviewStage.VALIDATE, q
            q = pop_from_stage(InterviewStage.DEEP_DIVE)
            if q:
                return InterviewStage.DEEP_DIVE, q

        elif current_stage == InterviewStage.VALIDATE:
            # Nếu chưa vượt ngưỡng 30% thời gian -> Ưu tiên kiểm tra tiếp Validate nếu còn câu
            if elapsed_ratio < stage_until_ratio[InterviewStage.VALIDATE]:
                q = pop_from_stage(InterviewStage.VALIDATE)
                if q:
                    return InterviewStage.VALIDATE, q
            # Đã xong Validate hoặc chạm mốc -> Chuyển sang DEEP_DIVE
            q = pop_from_stage(InterviewStage.DEEP_DIVE)
            if q:
                return InterviewStage.DEEP_DIVE, q
            q = pop_from_stage(InterviewStage.CHALLENGE)
            if q:
                return InterviewStage.CHALLENGE, q

        elif current_stage == InterviewStage.DEEP_DIVE:
            # Dynamic Loop: Còn thời gian (< 75%) -> Tiếp tục rút từ Queue DEEP_DIVE
            if elapsed_ratio < stage_until_ratio[InterviewStage.DEEP_DIVE]:
                q = pop_from_stage(InterviewStage.DEEP_DIVE)
                if q:
                    return InterviewStage.DEEP_DIVE, q
                # Nếu đã hết câu DEEP_DIVE trong pool mà còn nhiều giờ -> Rút tiếp CHALLENGE
                q = pop_from_stage(InterviewStage.CHALLENGE)
                if q:
                    return InterviewStage.CHALLENGE, q
            else:
                # Đã chạm hoặc vượt 75% -> Ưu tiên chuyển sang CHALLENGE, sau đó nốt DEEP_DIVE
                q = pop_from_stage(InterviewStage.CHALLENGE)
                if q:
                    return InterviewStage.CHALLENGE, q
                q = pop_from_stage(InterviewStage.DEEP_DIVE)
                if q:
                    return InterviewStage.DEEP_DIVE, q

            # Chỉ chuyển sang BEHAVIORAL khi toàn bộ frozen DEEP_DIVE và CHALLENGE đã cạn
            if not has_pending_technical():
                if not behavioral_required:
                    return open_closing()
                q = pop_from_stage(InterviewStage.BEHAVIORAL)
                if q:
                    return InterviewStage.BEHAVIORAL, q

        elif current_stage == InterviewStage.CHALLENGE:
            if elapsed_ratio < stage_until_ratio[InterviewStage.CHALLENGE]:
                q = pop_from_stage(InterviewStage.CHALLENGE)
                if q:
                    return InterviewStage.CHALLENGE, q
            # Rút tiếp CHALLENGE nếu còn
            q = pop_from_stage(InterviewStage.CHALLENGE)
            if q:
                return InterviewStage.CHALLENGE, q
            # Rút tiếp các câu DEEP_DIVE còn pending từ các competency khác
            q = pop_from_stage(InterviewStage.DEEP_DIVE)
            if q:
                return InterviewStage.DEEP_DIVE, q
            # Chỉ chuyển sang BEHAVIORAL khi toàn bộ frozen DEEP_DIVE và CHALLENGE đã cạn
            if not has_pending_technical():
                if not behavioral_required:
                    return open_closing()
                q = pop_from_stage(InterviewStage.BEHAVIORAL)
                if q:
                    return InterviewStage.BEHAVIORAL, q

        elif current_stage == InterviewStage.BEHAVIORAL:
            # Behavioral must be fully drained regardless of elapsed-ratio pacing.
            # Once in BEHAVIORAL, NEVER fall back to DEEP_DIVE or CHALLENGE.
            q = pop_from_stage(InterviewStage.BEHAVIORAL)
            if q:
                return InterviewStage.BEHAVIORAL, q

            behavioral_queue = questions_pool.get(InterviewStage.BEHAVIORAL.value, [])
            has_completed_behavioral = any(
                as_valid_frozen_question(InterviewStage.BEHAVIORAL, frozen_q) is not None
                and (
                    str(frozen_q.get("question_id") or "") in answered_ids
                    or str(frozen_q.get("question_version_id") or "") in answered_ids
                )
                for frozen_q in behavioral_queue
            )
            if not has_completed_behavioral:
                if not behavioral_required:
                    return open_closing()
                return InterviewStage.CLOSED, None
            return open_closing()

        # 4. Fallback: Duyệt tuần tự qua các stage còn lại
        stage_order = [
            InterviewStage.WARM_UP,
            InterviewStage.VALIDATE,
            InterviewStage.DEEP_DIVE,
            InterviewStage.CHALLENGE,
            InterviewStage.BEHAVIORAL,
        ]
        curr_idx = stage_order.index(current_stage) if current_stage in stage_order else 0
        for next_st in stage_order[curr_idx + 1 :]:
            if next_st == InterviewStage.BEHAVIORAL and has_pending_technical():
                continue
            q = pop_from_stage(next_st)
            if q:
                return next_st, q

        # Exhausting an assessment queue is not evidence that Behavioral completed.
        # Only the BEHAVIORAL branch above may open Closing, unless the blueprint
        # has no Behavioral stage: then the last technical answer opens it.
        if not behavioral_required and current_stage != InterviewStage.WARM_UP and not has_pending_technical():
            return open_closing()
        return InterviewStage.CLOSED, None
