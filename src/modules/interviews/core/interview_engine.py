# src/modules/interviews/core/interview_engine.py
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Optional,
    Tuple,
)
from uuid import UUID

from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_text
from src.modules.interviews.core import speech_checks
from src.modules.interviews.core.demo_mode import DEMO_OVERTIME_GRACE_SECONDS
from src.modules.interviews.core.interview_response import InterviewResponseMixin
from src.modules.interviews.core.interview_rules import (
    _FIXED_PROBES,
    DEEPEN_ASPECT_EN,
    DEEPEN_ASPECT_VI,
    EVALUATION_SCHEMA,
    MAX_QNA_QUESTIONS,
    PROHIBITED_LEAK_PATTERNS,
    QNA_BUDGET_SECONDS,
    SAFE_FALLBACK_PROBE_EN,
    SAFE_FALLBACK_PROBE_VI,
    TIME_THRESHOLDS,
    acknowledgement_pool,
    acknowledgement_rejection_reason,
    candidate_question_response,
    checked_probe,
    clarify_fallback_text,
    clarify_rejection_reason,
    classify_candidate_intent,
    classify_candidate_question,
    fallback_acknowledgement,
    fallback_probe,
    format_job_context,
    is_abort_request,
    is_repeat_request,
    is_skip_request,
    is_yes_no_question,
    normalize_candidate_pronoun,
    pacing_seconds,
    probe_is_sayable,
    safe_acknowledgement,
    validate_probe_text,
)
from src.modules.interviews.core.interview_types import (
    CandidateTurnInput,
    InterviewerTurnOutput,
    InterviewStage,
    QuestionItem,
    SessionExitReason,
    TurnAction,
)
from src.modules.interviews.core.interviewer_voice import (
    CLOSE_COMPLETED_EN,
    CLOSE_COMPLETED_VI,
    EARLY_STOP_EN,
    EARLY_STOP_VI,
    FAREWELL_EN,
    FAREWELL_VI,
    LAST_QNA_CLOSING_EN,
    LAST_QNA_CLOSING_VI,
    QNA_FALLBACK_EN,
    QNA_FALLBACK_VI,
    QNA_MORE_EN,
    QNA_MORE_VI,
    REPEAT_PREFIX_EN,
    REPEAT_PREFIX_VI,
    SKIP_ACK_EN,
    SKIP_ACK_SHORT_EN,
    SKIP_ACK_SHORT_VI,
    SKIP_ACK_VI,
    TIMEOUT_EN,
    TIMEOUT_VI,
    abort_confirmation,
    acknowledgement_for_action,
    candidate_block,
    candidate_data_rule,
    compose_turn,
    new_terms,
    pick_variant,
    qna_rejection_reason,
    speech_prompt,
    speech_v2_enabled,
)
from src.modules.interviews.core.language_policy import response_matches_locale

__all__ = [
    "InterviewCoreEngine",
    "DEEPEN_ASPECT_EN",
    "DEEPEN_ASPECT_VI",
    "EVALUATION_SCHEMA",
    "MAX_QNA_QUESTIONS",
    "PROHIBITED_LEAK_PATTERNS",
    "QNA_BUDGET_SECONDS",
    "SAFE_FALLBACK_PROBE_EN",
    "SAFE_FALLBACK_PROBE_VI",
    "TIME_THRESHOLDS",
    "_FIXED_PROBES",
    "acknowledgement_pool",
    "acknowledgement_rejection_reason",
    "candidate_question_response",
    "checked_probe",
    "clarify_fallback_text",
    "clarify_rejection_reason",
    "classify_candidate_intent",
    "classify_candidate_question",
    "fallback_acknowledgement",
    "fallback_probe",
    "format_job_context",
    "is_abort_request",
    "is_repeat_request",
    "is_skip_request",
    "is_yes_no_question",
    "normalize_candidate_pronoun",
    "pacing_seconds",
    "probe_is_sayable",
    "safe_acknowledgement",
    "validate_probe_text",
]
logger = logging.getLogger("InterviewCoreEngine")


class InterviewCoreEngine(InterviewResponseMixin):
    """
    Headless Interview Engine dùng chung 100% cho cả ChatAdapter và VoiceAdapter.
    """

    def _llm_options(
        self, *, speech: bool = False, schema: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Extra options for the shared OpenAI facade (speech v2); none for test generators.

        The model in use ignores temperature (``ai/facade.py``), so wording was
        steered by prompt and guards only. Lines spoken to the candidate ask for
        no reasoning (latency); the evaluation call gets a JSON schema with
        enums and length limits instead of free JSON parsed afterwards.
        """
        if self._generate_fn is not generate_text or not speech_v2_enabled():
            return {}
        options: Dict[str, Any] = {}
        if speech:
            options["reasoning_effort"] = "none"
        if schema:
            options["json_schema"] = schema
        return options

    def __init__(self, llm_client=None, ai_generator=None):
        """
        llm_client: Client gọi LLM hoặc wrapper hỗ trợ generate_json / generate_text.
        ai_generator: Hàm generate async có chữ ký (instructions, input_text, ...).
        Nếu None, mặc định sử dụng module AI facade của dự án.
        """
        self.llm = llm_client
        self._generate_fn = ai_generator or generate_text

    async def handle_turn(
        self,
        turn_input: CandidateTurnInput,
        session_state: Dict[str, Any],
    ) -> InterviewerTurnOutput:
        """
        Hàm xử lý lượt phỏng vấn trung tâm.
        - turn_input: dữ liệu ứng viên vừa gửi (text + metadata)
        - session_state: state lấy từ DB (PostgreSQL session + turns)
        """
        session_id = turn_input.session_id
        candidate_text = turn_input.text_content.strip()
        locale = session_state.get("locale", "vi-VN")
        is_vi = str(locale).lower().startswith("vi")

        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
        # Lines the interviewer said most recently, and a per-turn seed: fixed
        # lines are picked from pools so the same sentence is not repeated.
        recent_lines = [str(x) for x in (session_state.get("recent_interviewer_lines") or []) if x]
        seed = f"{session_id}:{turn_input.turn_index}"

        # =========================================================================
        # CHỐT CHẶN TOÀN CỤC 1: XIN DỪNG PHỎNG VẤN (CONFIRM_ABORT MODAL)
        # Bất kể đang ở Warm-up, Validate hay bất kỳ stage nào:
        # Nếu ứng viên có dấu hiệu xin dừng -> Kích hoạt Modal xác nhận cho ứng viên tự quyết
        # =========================================================================
        # In CLOSING the candidate wrapping up ("em hết câu hỏi rồi, mình kết
        # thúc tại đây nhé") is the normal ending, not an abort: let the closing
        # handler complete the session instead of recording USER_ENDED.
        if current_stage != InterviewStage.CLOSING and is_abort_request(candidate_text):
            confirm_msg = abort_confirmation(is_vi)
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=confirm_msg,
                action=TurnAction.CONFIRM_ABORT,
                current_stage=current_stage,
                is_session_finished=False,
                metadata={
                    "requires_abort_confirmation": True,
                    "candidate_abort_intent": True,
                    "consecutive_uncooperative": 0,
                    "consecutive_fails": 0,  # Deprecated compatibility alias.
                },
            )

        now = datetime.now(timezone.utc)

        # 1. TÍNH TOÁN THỜI LƯỢNG & PACING
        # Session elapsed time comes from `session_state`, never from
        # `turn_input.duration_seconds` -- that field is the length of THIS
        # answer and is used below only for the per-question hard-answer
        # timeout. Treating it as session elapsed restarted the session clock on
        # every turn for any caller that supplied it.
        started_at = session_state.get("started_at") or now
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except Exception:
                started_at = now
        if started_at is None:
            started_at = now
        if session_state.get("elapsed_time") is not None and session_state.get("elapsed_time") > 0:
            elapsed_seconds = int(session_state["elapsed_time"])
        else:
            elapsed_seconds = max(0, int((now - started_at).total_seconds()))
        target_duration_seconds = session_state.get("target_duration_minutes", 25) * 60
        time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)
        session_state["elapsed_time"] = elapsed_seconds
        session_state["remaining_time"] = time_remaining_seconds

        # Kiểm tra Hard Timeout toàn phiên (<= 30 giây và chưa ở stage CLOSING)
        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
        # A demo follows its turns, not the clock: it only times out once it is
        # past its nominal duration by the overtime grace.
        if session_state.get("is_demo"):
            is_hard_timeout = elapsed_seconds > target_duration_seconds + DEMO_OVERTIME_GRACE_SECONDS
        else:
            is_hard_timeout = (
                time_remaining_seconds <= pacing_seconds(30, target_duration_seconds)
                and current_stage != InterviewStage.CLOSING
            )
        if is_hard_timeout:
            trace_event(
                "interviewer",
                "hard_timeout_triggered",
                session_id=session_id,
                time_remaining_seconds=time_remaining_seconds,
                elapsed_seconds=elapsed_seconds,
            )
            timeout_msg = pick_variant(TIMEOUT_VI if is_vi else TIMEOUT_EN, f"{seed}:timeout", recent_lines)
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.HARD_TIMEOUT,
                message=timeout_msg,
            )

        # =========================================================================
        # CHỐT CHẶN TOÀN CỤC 2: XIN ĐỔI CÂU HỎI (SKIP_REQUEST)
        # =========================================================================
        if is_skip_request(candidate_text):
            skip_ack = pick_variant(SKIP_ACK_VI if is_vi else SKIP_ACK_EN, f"{seed}:skip", recent_lines)
            next_stage, next_question = self._get_next_stage_and_question(session_state)
            if next_stage == InterviewStage.CLOSED or next_question is None:
                close_msg = pick_variant(
                    CLOSE_COMPLETED_VI if is_vi else CLOSE_COMPLETED_EN, f"{seed}:close", recent_lines
                )
                return self._terminate_session(
                    session_id=session_id,
                    turn_index=turn_input.turn_index + 1,
                    reason=SessionExitReason.NORMAL_COMPLETION,
                    message=close_msg,
                )
            skip_transition = self._transition_for(
                session_state,
                current_stage,
                next_stage,
                next_question,
                is_vi,
                seed,
                after_struggle=True,
            )
            if skip_transition and speech_v2_enabled():
                # The transition already says where we go next ("Mình chuyển sang…").
                skip_ack = pick_variant(
                    SKIP_ACK_SHORT_VI if is_vi else SKIP_ACK_SHORT_EN, f"{seed}:skip", recent_lines
                )
            speech, speech_plan = compose_turn(
                skip_ack,
                next_question.spoken_prompt or next_question.main_prompt,
                skip_transition,
                recent=recent_lines if speech_v2_enabled() else (),
                ack_source="script",
                question_source="pregenerated" if next_question.spoken_prompt else "bank",
            )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=speech,
                action=TurnAction.NEXT_QUESTION,
                current_stage=next_stage,
                current_competency=next_question.competency,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "skipped_turn": True,
                    "transition": skip_transition,
                    "speech_plan": speech_plan,
                    "turn_in_question": 0,
                    "sufficiency_status": "INSUFFICIENT",
                    "consecutive_uncooperative": 0,
                    "consecutive_fails": 0,  # Deprecated compatibility alias.
                },
            )

        consecutive_uncooperative = session_state.get(
            "consecutive_uncooperative",
            session_state.get("consecutive_fails", 0),
        )
        current_turn_in_question = session_state.get(
            "current_turn_in_question", 0
        )  # 0: Câu chính, 1: Câu probe
        allow_early_exit = session_state.get("allow_early_exit", True)
        # NẾU ĐANG Ở GIAI ĐOẠN CLOSING (CANDIDATE ASKS AI - REVERSE Q&A):
        if current_stage == InterviewStage.CLOSING:
            return await self._handle_closing_turn(
                turn_input=turn_input,
                session_state=session_state,
                is_vi=is_vi,
                time_remaining_seconds=time_remaining_seconds,
            )

        current_question = session_state.get("current_question_context", {})
        question_prompt = current_question.get("main_prompt", "")
        # After a probe the candidate is looking at the probe, so "repeat",
        # "explain" and "back to the question" refer to it, not the main question.
        active_prompt = current_question.get("follow_up_prompt") or question_prompt
        # What the candidate actually heard: the spoken version when there is one.
        heard_prompt = (
            current_question.get("follow_up_prompt")
            or current_question.get("spoken_prompt")
            or question_prompt
        )

        # The candidate did not hear the question: ask it again word for word.
        # Not an answer, so it is neither evaluated nor counted against the
        # one clarification each question allows.
        if active_prompt and is_repeat_request(candidate_text):
            repeat_prefix = pick_variant(
                REPEAT_PREFIX_VI if is_vi else REPEAT_PREFIX_EN, f"{seed}:repeat", ()
            )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=f"{repeat_prefix} {heard_prompt}",
                action=TurnAction.CLARIFY,
                current_stage=current_stage,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "repeat_request": True,
                    "turn_in_question": current_turn_in_question,
                    "sufficiency_status": "NOT_ASSESSED",
                },
            )

        intent = classify_candidate_intent(candidate_text, question_prompt)

        # Questions addressed to the interviewer are conversational control,
        # not evidence that the current assessment turn has been answered.
        # Reply deterministically, disclose no rubric details, and keep the
        # current turn ASKED by returning CLARIFY.
        candidate_question_intent = classify_candidate_question(candidate_text)
        if candidate_question_intent is not None:
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=candidate_question_response(
                    candidate_question_intent,
                    current_question=active_prompt,
                    is_vi=is_vi,
                    duration_minutes=int(session_state.get("target_duration_minutes", 25)),
                ),
                action=TurnAction.CLARIFY,
                current_stage=current_stage,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "candidate_question_intent": candidate_question_intent,
                    "turn_in_question": session_state.get("current_turn_in_question", 0),
                    "sufficiency_status": "NOT_ASSESSED",
                },
            )

        # 2. ĐÁNH GIÁ CÂU TRẢ LỜI HIỆN TẠI (TURN EVALUATION)
        eval_result = await self._evaluate_candidate_response(
            candidate_text=turn_input.text_content,
            current_stage=current_stage,
            current_question=current_question,
            is_vi=is_vi,
            telemetry=turn_input.telemetry,
            precomputed_evaluation=turn_input.precomputed_evaluation,
            recent_lines=recent_lines,
            seed=seed,
        )

        # BẢO HIỂM TẦNG 2: Nếu LLM nhận diện ý định xin dừng phỏng vấn -> Kích hoạt Modal xác nhận
        if eval_result.get("intent") == "CANDIDATE_ABORT":
            confirm_msg = abort_confirmation(is_vi)
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=confirm_msg,
                action=TurnAction.CONFIRM_ABORT,
                current_stage=current_stage,
                is_session_finished=False,
                metadata={
                    "requires_abort_confirmation": True,
                    "candidate_abort_intent": True,
                    "consecutive_uncooperative": 0,
                    "consecutive_fails": 0,  # Deprecated compatibility alias.
                },
            )

        # Off-topic replies are not evidence: redirect without grading them.
        if eval_result.get("intent") == "OFF_TOPIC":
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=candidate_question_response(
                    "OFF_TOPIC_REPLY",
                    current_question=active_prompt,
                    is_vi=is_vi,
                    duration_minutes=int(session_state.get("target_duration_minutes", 25)),
                ),
                action=TurnAction.CLARIFY,
                current_stage=current_stage,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "candidate_question_intent": "OFF_TOPIC",
                    "turn_in_question": current_turn_in_question,
                    "sufficiency_status": "NOT_ASSESSED",
                },
            )

        # Abort precedence is resolved above. Clarify is allowed only when neither
        # deterministic nor evaluator intent indicates that the candidate wants to stop.
        clarify_count = session_state.get("clarify_count", 0)
        if intent == "CLARIFY_REQUEST" and current_stage not in (
            InterviewStage.WARM_UP,
            InterviewStage.CLOSING,
        ):
            if clarify_count < 1:
                clarify_msg = await self._generate_clarify_text(
                    current_question=current_question,
                    candidate_text=candidate_text,
                    is_vi=is_vi,
                )
                return InterviewerTurnOutput(
                    session_id=session_id,
                    turn_index=turn_input.turn_index + 1,
                    message_text=clarify_msg,
                    action=TurnAction.CLARIFY,
                    current_stage=current_stage,
                    time_remaining_seconds=time_remaining_seconds,
                    metadata={
                        "consecutive_uncooperative": 0,
                        "consecutive_fails": 0,  # Deprecated compatibility alias.
                        "turn_in_question": current_turn_in_question,
                        "clarify_used": True,
                        "sufficiency_status": "AMBIGUOUS",
                        "generated_by": "fixed_fallback"
                        if clarify_msg
                        == clarify_fallback_text(current_question.get("main_prompt", ""), is_vi=is_vi)
                        else "llm",
                    },
                )

        # Gate 4 Architectural Invariant:
        # Numeric score is telemetry/evaluation-only; it has zero control-flow authority in Gate 4.
        score_telemetry = float(eval_result.get("score", 6.0))

        # Runtime control flow is driven strictly by semantic signals:
        # candidate intent, answer sufficiency, missing evidence, probe budget, time budget.
        sufficiency_status = eval_result.get("sufficiency_status")
        if not sufficiency_status:
            sufficiency_status = "SUFFICIENT" if eval_result.get("is_sufficient", True) else "INSUFFICIENT"
        is_sufficient = sufficiency_status == "SUFFICIENT"

        is_give_up = intent == "GIVE_UP"

        # Adaptive Probing: Nếu ứng viên trả lời quá ngắn (< 15 từ hoặc < 70 ký tự)
        # trong khi câu hỏi không phải là Yes/No và chưa từng probe ở câu này:
        words = candidate_text.split()
        is_too_brief = (
            len(words) < 15
            and len(candidate_text) < 70
            and not is_yes_no_question(question_prompt)
            and not is_give_up
        )
        if is_too_brief:
            is_sufficient = False
            sufficiency_status = "INSUFFICIENT"

        # 3. PO-approved Gate 4 counter policy: only explicit GIVE_UP increments
        # the canonical counter. Scores, insufficiency, honest answers, skip and
        # clarification have no authority to increment it.
        if intent == "GIVE_UP":
            consecutive_uncooperative += 1
        else:
            consecutive_uncooperative = 0
        session_state["consecutive_uncooperative"] = consecutive_uncooperative

        technical_stages = {InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE}
        if allow_early_exit and current_stage in technical_stages and consecutive_uncooperative >= 2:
            uncoop_msg = pick_variant(
                EARLY_STOP_VI if is_vi else EARLY_STOP_EN, f"{seed}:early", recent_lines
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.FAST_FAIL_TECH,
                message=uncoop_msg,
                metadata={
                    "consecutive_uncooperative": consecutive_uncooperative,
                    "consecutive_fails": consecutive_uncooperative,
                },
            )

        # 4. ĐIỀU HÒA NHỊP ĐỘ (PACING OFFSET) & QUYẾT ĐỊNH PROBE
        # Probe Budget Guardrails
        closing_reserve_seconds = session_state.get(
            "closing_reserve_seconds", pacing_seconds(60, target_duration_seconds)
        )
        behavioral_reserve_seconds = session_state.get(
            "behavioral_reserve_seconds", pacing_seconds(180, target_duration_seconds)
        )
        hard_answer_seconds = current_question.get("hard_answer_seconds", 180)

        is_hard_answer_timeout = (
            turn_input.duration_seconds > hard_answer_seconds
            if turn_input.duration_seconds and turn_input.duration_seconds > 0
            else False
        )
        is_near_closing_reserve = time_remaining_seconds <= closing_reserve_seconds
        is_near_behavioral_reserve = time_remaining_seconds <= (
            closing_reserve_seconds + behavioral_reserve_seconds
        ) and current_stage in [InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE]
        asked_ids = set(str(qid) for qid in session_state.get("asked_question_ids", []))
        behavioral_queue = session_state.get("questions_pool", {}).get(InterviewStage.BEHAVIORAL.value, [])
        has_pending_behavioral = any(
            str(q.get("question_id") or q.get("question_version_id") or "") not in asked_ids
            for q in behavioral_queue
        )

        # Nếu đang bị trễ giờ hơn 80% thời gian -> cấm probe, ép chuyển câu
        is_behind_schedule = (elapsed_seconds > (target_duration_seconds * 0.8)) and current_stage in [
            InterviewStage.VALIDATE,
            InterviewStage.DEEP_DIVE,
        ]

        probe_window_open = (
            # A demo asks each frozen question once so every stage fits.
            not session_state.get("is_demo")
            and not is_give_up
            and current_turn_in_question == 0
            and not is_behind_schedule
            and not is_hard_answer_timeout
            and not (is_near_behavioral_reserve and has_pending_behavioral)
            and not is_near_closing_reserve
            and current_stage
            in [
                InterviewStage.VALIDATE,
                InterviewStage.DEEP_DIVE,
                InterviewStage.CHALLENGE,
            ]
        )
        should_probe = probe_window_open and not is_sufficient
        # Challenge tier (CV–job match from 70%, planning.entry_gate): a good
        # answer to a technical question gets one deeper follow-up, so a strong
        # candidate is stretched instead of moved on at once.
        is_coding_turn = bool(
            turn_input.telemetry
            and (turn_input.telemetry.get("is_coding_turn") or turn_input.telemetry.get("isCodingTurn"))
        )
        is_deepening_probe = bool(
            probe_window_open
            and is_sufficient
            and session_state.get("practice_tier") == "challenge"
            and current_stage in (InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE)
            and not is_coding_turn
        )
        if is_deepening_probe:
            should_probe = True
        # Turn 0 (WARM_UP): Khóa tuyệt đối tính năng probe, luôn tiếp nhận chào hỏi và chuyển tiếp
        if current_stage == InterviewStage.WARM_UP:
            should_probe = False
            is_deepening_probe = False

        # 5. XỬ LÝ CHUYỂN BƯỚC FSM
        if should_probe:
            # The evaluator can return the one allowed probe in the same JSON
            # response. This removes a second sequential model round-trip from
            # the latency-critical voice path while preserving the existing
            # guarded generator as a fallback for legacy/invalid responses.
            probe_question = (
                ""
                if is_deepening_probe  # the evaluator's probe targets a gap; there is none
                else checked_probe(
                    str(eval_result.get("probe_question") or eval_result.get("reply_text") or ""),
                    is_vi=is_vi,
                    question=question_prompt,
                    answer=candidate_text,
                    rubric=str(current_question.get("rubric_criteria") or ""),
                )
                or ""
            )
            probe_from_evaluation = bool(probe_question)
            if probe_from_evaluation:
                speech_checks.record("probe", "pass", "from_evaluation")
            else:
                probe_question = await self._generate_probe_question(
                    candidate_text=turn_input.text_content,
                    current_question=current_question,
                    is_vi=is_vi,
                    telemetry=turn_input.telemetry,
                    missing_aspect=(
                        (DEEPEN_ASPECT_VI if is_vi else DEEPEN_ASPECT_EN)
                        if is_deepening_probe
                        else eval_result.get("missing_aspect", "")
                    ),
                    seed=seed,
                    recent_lines=recent_lines,
                )
            # A real interviewer acknowledges before following up, so the
            # candidate is not hit with a new question the instant they stop.
            # The probe stays the last paragraph (chat reads it back from there).
            probe_ack = str(eval_result.get("acknowledgement") or "").strip()
            probe_ack, ack_changed = acknowledgement_for_action(
                probe_ack,
                action="PROBE",
                is_vi=is_vi,
                seed=seed,
                recent=recent_lines,
                answer=candidate_text,
                stage=current_stage.value,
            )
            if ack_changed:
                eval_result = {
                    **eval_result,
                    "ack_source": "action",
                    "ack_reason": ack_changed,
                    "ack_raw": eval_result.get("acknowledgement"),
                }
            probe_source = "script" if probe_question in _FIXED_PROBES else "llm"
            probe_speech, speech_plan = compose_turn(
                probe_ack,
                probe_question,
                recent=recent_lines if speech_v2_enabled() else (),
                ack_source=str(eval_result.get("ack_source") or "-"),
                question_source=probe_source,
            )
            probe_terms = (
                new_terms(
                    probe_question,
                    question_prompt,
                    candidate_text,
                    str(eval_result.get("missing_aspect") or ""),
                )
                if speech_v2_enabled() and probe_source == "llm"
                else []
            )
            if probe_terms:
                trace_event(
                    "interviewer", "speech_new_terms", kind="probe", terms=probe_terms, session_id=session_id
                )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=probe_speech,
                action=TurnAction.PROBE,
                current_stage=current_stage,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "consecutive_uncooperative": consecutive_uncooperative,
                    "consecutive_fails": consecutive_uncooperative,  # Deprecated compatibility alias.
                    "turn_in_question": 1,
                    "pre_score": score_telemetry,
                    "sufficiency_status": sufficiency_status,
                    "hard_answer_timeout": is_hard_answer_timeout,
                    "probe_generated_in_evaluation": probe_from_evaluation,
                    # gap: the answer missed something; deepen: challenge tier, answer was sufficient.
                    "probe_kind": "deepen" if is_deepening_probe else "gap",
                    "intent": eval_result.get("intent"),
                    # Persisted on the PROBE message: an LLM-written follow-up is
                    # the one interview question that never came from the bank.
                    "generated_by": "fixed_fallback" if probe_question in _FIXED_PROBES else "llm",
                    "missing_aspect": str(eval_result.get("missing_aspect") or "")[:300],
                    "acknowledgement": probe_ack,
                    "speech_plan": speech_plan,
                    "new_terms": probe_terms,
                    "ack_source": eval_result.get("ack_source"),
                    "ack_reason": eval_result.get("ack_reason"),
                    "ack_raw": eval_result.get("ack_raw"),
                },
            )

        # NẾU KHÔNG PROBE: CHUYỂN CÂU HỎI TIẾP THEO HOẶC ĐỔI STAGE
        next_stage, next_question = self._get_next_stage_and_question(session_state)

        if next_stage == InterviewStage.CLOSED or next_question is None:
            close_msg = pick_variant(
                CLOSE_COMPLETED_VI if is_vi else CLOSE_COMPLETED_EN, f"{seed}:close", recent_lines
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.NORMAL_COMPLETION,
                message=close_msg,
            )

        default_ack = fallback_acknowledgement(
            is_vi, seed, recent_lines, acknowledgement_pool(is_vi, current_stage)
        )
        next_ack, ack_changed = acknowledgement_for_action(
            eval_result.get("acknowledgement") or default_ack,
            action="NEXT_QUESTION",
            is_vi=is_vi,
            seed=seed,
            recent=recent_lines,
            answer=candidate_text,
            stage=current_stage.value,
        )
        if ack_changed:
            eval_result = {
                **eval_result,
                "acknowledgement": next_ack,
                "ack_source": "action",
                "ack_reason": ack_changed,
                "ack_raw": eval_result.get("acknowledgement"),
            }
        next_transition = self._transition_for(
            session_state,
            current_stage,
            next_stage,
            next_question,
            is_vi,
            seed,
            after_struggle=str(eval_result.get("intent") or "") in {"GIVE_UP", "SKIP_QUESTION"},
        )
        interviewer_message, speech_plan = compose_turn(
            next_ack,
            next_question.spoken_prompt or next_question.main_prompt,
            next_transition,
            recent=recent_lines if speech_v2_enabled() else (),
            ack_source=(
                str(eval_result.get("ack_source") or "fallback")
                if eval_result.get("acknowledgement")
                else "fallback"
            ),
            question_source="pregenerated" if next_question.spoken_prompt else "bank",
        )

        return InterviewerTurnOutput(
            session_id=session_id,
            turn_index=turn_input.turn_index + 1,
            message_text=interviewer_message,
            action=TurnAction.NEXT_QUESTION,
            current_stage=next_stage,
            current_competency=next_question.competency if next_question else None,
            time_remaining_seconds=time_remaining_seconds,
            metadata={
                "consecutive_uncooperative": consecutive_uncooperative,
                "consecutive_fails": consecutive_uncooperative,  # Deprecated compatibility alias.
                "turn_in_question": 0,
                "question_id": next_question.question_id if next_question else None,
                "pre_score": score_telemetry,
                "sufficiency_status": sufficiency_status,
                "hard_answer_timeout": is_hard_answer_timeout,
                "acknowledgement": next_ack,
                "transition": next_transition,
                "speech_plan": speech_plan,
                "intent": eval_result.get("intent"),
                "ack_source": eval_result.get("ack_source")
                if eval_result.get("acknowledgement")
                else "fallback",
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
        is_last_question = questions_asked >= MAX_QNA_QUESTIONS or (
            not is_demo and qna_elapsed >= pacing_seconds(QNA_BUDGET_SECONDS, session_seconds)
        )
        closing_seed = f"{session_id}:{turn_input.turn_index}"
        if has_no_more_q or (not is_demo and time_remaining_seconds <= pacing_seconds(90, session_seconds)):
            farewell = pick_variant(FAREWELL_VI if is_vi else FAREWELL_EN, f"{closing_seed}:farewell")
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
        language = "tiếng Việt" if is_vi else "English"
        follow_up = (
            "Không hỏi thêm câu nào; chỉ trả lời câu hỏi, hệ thống sẽ tự chào kết thúc."
            if is_last_question
            else "Sau khi trả lời, hỏi ngắn gọn ứng viên còn câu hỏi nào khác không."
        )
        system_prompt = (
            f"Bạn là người phỏng vấn đại diện cho {company}, vị trí {job_title}.\n"
            f"Đây là phần ứng viên hỏi ngược; buổi phỏng vấn còn khoảng {remaining_minutes} phút.\n"
            "CHỈ được dùng thông tin trong phần THÔNG TIN TUYỂN DỤNG dưới đây. Nếu câu hỏi nằm ngoài "
            "các thông tin này (ví dụ OT, quy trình nội bộ, stack chưa nêu, phúc lợi chưa nêu), nói rõ "
            "rằng mô tả công việc chưa đề cập và bộ phận tuyển dụng sẽ trao đổi thêm. Tuyệt đối không bịa "
            "thông tin về công ty.\n"
            f"Trả lời bằng {language}, tối đa 4 câu. {follow_up}\n"
            "Tuyệt đối không chấm điểm hay nhận xét điểm số.\n\n"
            f"THÔNG TIN TUYỂN DỤNG:\n{job_context}"
        )
        system_prompt += "\n\n" + candidate_data_rule(is_vi)
        user_content = "Câu hỏi của ứng viên:\n" + candidate_block(candidate_text)
        if speech_v2_enabled():
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
                closing_line = pick_variant(
                    LAST_QNA_CLOSING_VI if is_vi else LAST_QNA_CLOSING_EN, f"{closing_seed}:last"
                )
                return self._terminate_session(
                    session_id=session_id,
                    turn_index=turn_input.turn_index + 1,
                    reason=SessionExitReason.NORMAL_COMPLETION,
                    message=f"{answer_text}\n\n{closing_line}",
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

        try:
            if hasattr(self.llm, "generate_text"):
                answer = await self.llm.generate_text(system_prompt=system_prompt, user_content=user_content)
            else:
                answer = await self._generate_fn(
                    instructions=system_prompt,
                    input_text=user_content,
                    max_output_tokens=800,
                    temperature=0.4,
                )
            answer_text = normalize_candidate_pronoun(str(answer).strip(), is_vi)
            if not response_matches_locale(answer_text, is_vi=is_vi):
                raise ValueError("closing response language does not match session locale")
            answer_generated_by = "llm"
        except Exception as e:
            answer_generated_by = "fixed_fallback"
            logger.warning(f"Error generating Q&A response in CLOSING stage: {e}")
            answer_text = (
                "Cảm ơn câu hỏi của bạn. Hiện mình chưa thể trả lời chi tiết nội dung này; "
                "bộ phận tuyển dụng sẽ trao đổi thêm với bạn."
                if is_vi
                else "Thank you for the question. I can't answer that in detail right now; "
                "the recruiting team will follow up with you."
            )
            if not is_last_question:
                answer_text += (
                    "\n\nBạn còn câu hỏi nào khác không?" if is_vi else "\n\nDo you have any other questions?"
                )

        if is_last_question:
            closing_line = pick_variant(
                LAST_QNA_CLOSING_VI if is_vi else LAST_QNA_CLOSING_EN, f"{closing_seed}:last"
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.NORMAL_COMPLETION,
                message=f"{answer_text}\n\n{closing_line}",
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
            },
        )

    async def _answer_closing_question(
        self,
        *,
        candidate_text: str,
        job_title: str,
        company: str,
        job_context: str,
        is_vi: bool,
        remaining_minutes: int,
        is_last_question: bool,
        seed: str,
    ) -> tuple[str, str, Optional[str]]:
        """The answer to the candidate's own question (speech v2): (text, generated_by, rejection reason).

        Same interviewer and style as the rest of the session, at most three
        sentences, checked by ``qna_rejection_reason``. "Còn câu hỏi nào khác
        không?" is added by code so the model never writes it twice.
        """
        language = "tiếng Việt" if is_vi else "English"
        system_prompt = speech_prompt(
            is_vi=is_vi,
            role=(
                "Bạn là người phỏng vấn của Intervia, đang ở phần ứng viên hỏi ngược "
                f"về vị trí {job_title} tại {company}. Buổi phỏng vấn còn khoảng {remaining_minutes} phút."
            ),
            task=(
                "Trả lời câu hỏi của ứng viên CHỈ bằng THÔNG TIN TUYỂN DỤNG bên dưới. Nếu câu hỏi nằm ngoài "
                "các thông tin này (ví dụ OT, quy trình nội bộ, stack chưa nêu, phúc lợi chưa nêu), nói rõ "
                "rằng mô tả công việc chưa đề cập và bộ phận tuyển dụng sẽ trao đổi thêm."
            ),
            rules=(
                f"Trả lời bằng {language}, một đoạn, tối đa 3 câu.",
                "Không hỏi lại ứng viên, không khen câu hỏi; hệ thống tự hỏi ứng viên còn câu hỏi nào không.",
                "Không đưa con số, tên công ty hay công nghệ không có trong thông tin tuyển dụng.",
            ),
            output=f"Chỉ câu trả lời.\n\nTHÔNG TIN TUYỂN DỤNG:\n{job_context}",
        )
        system_prompt += "\n\n" + candidate_data_rule(is_vi)
        user_content = "Câu hỏi của ứng viên:\n" + candidate_block(candidate_text)
        reason: Optional[str] = None
        try:
            if hasattr(self.llm, "generate_text"):
                answer = await self.llm.generate_text(system_prompt=system_prompt, user_content=user_content)
            else:
                answer = await self._generate_fn(
                    instructions=system_prompt,
                    input_text=user_content,
                    max_output_tokens=800,
                    **self._llm_options(speech=True),
                )
            answer_text = normalize_candidate_pronoun(" ".join(str(answer).split()), is_vi)
            reason = qna_rejection_reason(
                answer_text,
                job_context=job_context,
                question=candidate_text,
                rubric_leak=lambda value: not validate_probe_text(value),
            ) or (None if response_matches_locale(answer_text, is_vi=is_vi) else "wrong_language")
            if reason:
                speech_checks.record("qna", "reject", reason, answer_text)
                raise ValueError(f"closing answer rejected: {reason}")
            generated_by = "llm"
            speech_checks.record("qna", "pass")
        except Exception as exc:
            logger.warning(f"Closing Q&A answer replaced: {exc}")
            if not reason:
                speech_checks.record("qna", "error", type(exc).__name__)
            trace_event("interviewer", "qna_answer_replaced", reason=reason or type(exc).__name__)
            answer_text = pick_variant(QNA_FALLBACK_VI if is_vi else QNA_FALLBACK_EN, f"{seed}:qna_fallback")
            generated_by = "fixed_fallback"
        if not is_last_question:
            more = pick_variant(QNA_MORE_VI if is_vi else QNA_MORE_EN, f"{seed}:qna_more")
            answer_text = f"{answer_text}\n\n{more}"
        return answer_text, generated_by, reason

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

        closing_reserve_seconds = session_state.get(
            "closing_reserve_seconds", pacing_seconds(60, target_duration_seconds)
        )
        behavioral_reserve_seconds = session_state.get(
            "behavioral_reserve_seconds", pacing_seconds(180, target_duration_seconds)
        )
        emergency_cutoff_seconds = pacing_seconds(90, target_duration_seconds)
        closing_min_seconds = pacing_seconds(180, target_duration_seconds)

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
            stage_qs = questions_pool.get(st.value, [])
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
            if elapsed_ratio < TIME_THRESHOLDS[InterviewStage.VALIDATE]:
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
            if elapsed_ratio < TIME_THRESHOLDS[InterviewStage.DEEP_DIVE]:
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
                q = pop_from_stage(InterviewStage.BEHAVIORAL)
                if q:
                    return InterviewStage.BEHAVIORAL, q

        elif current_stage == InterviewStage.CHALLENGE:
            if elapsed_ratio < TIME_THRESHOLDS[InterviewStage.CHALLENGE]:
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
                return InterviewStage.CLOSED, None

            # Behavioral is complete. Closing still requires at least 180 seconds
            # (scaled down for sessions shorter than 25 minutes).
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
        # Only the BEHAVIORAL branch above may open Closing.
        return InterviewStage.CLOSED, None
