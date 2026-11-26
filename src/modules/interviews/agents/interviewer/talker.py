"""The Talker: Luna's neutral acknowledgment and a speculative prediction.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §6.3 and the V2 correction brief §7. The
Talker sees the same immutable context the Thinker sees and starts at the same
moment — it never waits for the Thinker. It returns two things:

* an **acknowledgment**: one neutral line that may be spoken early, before
  the Thinker has answered. It confirms nothing, hints at nothing, promises
  no topic change, and goes through the acknowledgement guard before anyone
  hears it.
* a **prediction** of what the Thinker will decide — action, target, and the
  line that would follow. The Arbiter may reuse the line only when the Thinker
  turns out to agree on every field; the Talker itself decides nothing.

It reaches Luna through the existing OpenAI facade with a per-call ``model=``
(§1.1: no second model server). ``settings.interview_talker_model`` names the
model; empty means the Talker is unavailable and the router never picks
Hybrid. It never raises: a Talker failure costs an early line, never a turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.core.config import get_settings
from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_tool_calls
from src.modules.interviews.agents.interviewer.prompts.decide import decide_input
from src.modules.interviews.agents.interviewer.tools import UNIFIED_TEXT_ACTIONS
from src.modules.interviews.core.conversation.turn_context import TurnContext
from src.modules.interviews.core.guardrails.output import acknowledgement_rejection_reason

logger = logging.getLogger("InterviewTalker")

PURPOSE = "interviews.agent.talker"
TOOL_NAME = "talker_prediction"
ToolCaller = Callable[..., Awaitable[List[Dict[str, Any]]]]

__all__ = ["TalkerOutput", "predict_by_talker", "talker_model", "TOOL_NAME"]


@dataclass(frozen=True)
class TalkerOutput:
    """What Luna said, after the acknowledgment has passed its guard.

    ``acknowledgment`` is ``None`` when the model's line was rejected, so a
    caller can tell "nothing safe to say early" from "the Talker failed".
    """

    acknowledgment: Optional[str]
    acknowledgment_rejected: Optional[str]
    predicted_action: str
    predicted_target: str
    predicted_response: str
    model: str
    raw: Dict[str, Any] = field(default_factory=dict)


def talker_model() -> str:
    """The Luna model id, or "" when the Talker is not configured."""
    return str(get_settings().interview_talker_model or "").strip()


def _talker_tool(allowed_actions: List[str], is_vi: bool) -> Dict[str, Any]:
    """One tool: the acknowledgment plus a guess at the Thinker's call.

    The action is constrained to the same menu the Thinker sees, so the
    prediction cannot name something the turn would never allow.
    """
    actions = [name for name in allowed_actions if name in UNIFIED_TEXT_ACTIONS or name == "move_to_next_question"]
    if not actions:
        actions = list(allowed_actions) or ["move_to_next_question"]
    ack_description = (
        # "ghi nhận" is banned house style (_STYLE_BANNED_RE: "mình (đã) ghi nhận"),
        # so the cue must not suggest it; both examples pass the guard.
        "Một câu ngắn, trung lập, cho ứng viên biết mình đã nghe, nói ngay khi họ dứt lời. "
        "KHÔNG xác nhận đúng hay sai, KHÔNG khen, KHÔNG gợi ý đáp án, KHÔNG hứa chuyển chủ đề, "
        "KHÔNG phải câu hỏi, KHÔNG dùng chữ 'ghi nhận'. Ví dụ phong cách: 'Mình nghe rồi.' / 'Ừ, mình hình dung được.'"
        if is_vi
        else "One neutral acknowledgment to say right after the candidate stops. "
        "Do NOT confirm right or wrong, do NOT praise, do NOT hint at the answer, "
        "do NOT promise a topic change, and do NOT ask a question."
    )
    return {
        "type": "function",
        "name": TOOL_NAME,
        "description": "Ghi nhận trung lập và dự đoán bước kế tiếp của người phỏng vấn.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "acknowledgment": {"type": "string", "description": ack_description},
                "prediction": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": actions},
                        "target": {
                            "type": "string",
                            "description": "Ý còn thiếu hoặc chủ đề mà bước kế tiếp nhắm tới; để trống nếu không có.",
                        },
                        "response": {
                            "type": "string",
                            "description": "Lời thoại dự đoán người phỏng vấn sẽ nói cho bước đó, nguyên văn.",
                        },
                    },
                    "required": ["action", "target", "response"],
                    "additionalProperties": False,
                },
            },
            "required": ["acknowledgment", "prediction"],
            "additionalProperties": False,
        },
    }


def _talker_prompt(is_vi: bool) -> str:
    if is_vi:
        return (
            "# Vai trò\n"
            "Bạn là người đồng hành của người phỏng vấn Intervia. Ứng viên vừa dứt lời.\n\n"
            "# Nhiệm vụ\n"
            "1. Viết MỘT câu ngắn, trung lập cho ứng viên biết mình đã nghe, để nói ngay trong lúc người phỏng vấn chính đang cân nhắc.\n"
            "2. Dự đoán người phỏng vấn chính sẽ chọn hành động nào trong menu và sẽ nói gì.\n\n"
            "# Không bao giờ\n"
            "- Không chấm điểm, không nhận xét đúng/sai, không nhắc tiêu chí đánh giá.\n"
            "- Không tiết lộ đáp án, không hứa sẽ chuyển câu.\n"
            "- Câu nói ngay đó không phải câu hỏi và không dùng chữ 'ghi nhận'.\n"
            "- Không bịa chi tiết ứng viên chưa nói."
        )
    return (
        "# Role\n"
        "You accompany Intervia's interviewer. The candidate has just finished speaking.\n\n"
        "# Task\n"
        "1. Write ONE neutral acknowledgment to say now, while the main interviewer is deciding.\n"
        "2. Predict which menu action the main interviewer will choose and what they will say.\n\n"
        "# Never\n"
        "- No grading, no right/wrong, no mention of assessment criteria.\n"
        "- No giving the answer away, no promising to move on.\n"
        "- The acknowledgment is not a question.\n"
        "- No details the candidate did not say."
    )


async def predict_by_talker(
    ctx: TurnContext,
    *,
    allowed_actions: List[str],
    pacing: Dict[str, Any],
    generate: Optional[ToolCaller] = None,
    model: Optional[str] = None,
) -> Optional[TalkerOutput]:
    """Ask Luna for an acknowledgment and a prediction. Returns ``None`` on any failure.

    Same ``decide_input`` as the Thinker, so the two reason over one immutable
    snapshot of the turn; neither can see something the other did not.
    """
    model = (model or talker_model()).strip()
    if not model:
        return None
    generate = generate or generate_tool_calls
    items = decide_input(
        question=ctx.active_prompt or ctx.question_prompt,
        candidate_text=ctx.candidate_text,
        stage=ctx.current_stage.value,
        evaluation=None,
        assessment_criteria=str(ctx.current_question.get("rubric_criteria") or ""),
        recent_lines=ctx.recent_lines,
        memory_hits=[dict(h) for h in (ctx.session_state.get("candidate_memory") or ()) if isinstance(h, dict)],
        pacing=pacing,
        allowed_actions=allowed_actions,
        interview_contract=ctx.session_state.get("interview_contract"),
        is_vi=ctx.is_vi,
    )
    try:
        calls = await generate(
            instructions=_talker_prompt(ctx.is_vi),
            input_items=items,
            tools=[_talker_tool(allowed_actions, ctx.is_vi)],
            tool_choice={"type": "function", "name": TOOL_NAME},
            purpose=PURPOSE,
            reasoning_effort="none",
            max_output_tokens=300,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 - the Talker must never cost the candidate a turn
        trace_event("interviewer", "talker_error", session_id=str(ctx.session_id), error_type=type(exc).__name__)
        logger.warning("talker failed: %s", exc)
        return None

    call = next((c for c in calls if c.get("name") == TOOL_NAME), None)
    args = dict((call or {}).get("arguments") or {})
    if not call or "_raw" in args:
        trace_event("interviewer", "talker_rejected", session_id=str(ctx.session_id), reason="parse")
        return None

    ack = str(args.get("acknowledgment") or "").strip()
    rejected = acknowledgement_rejection_reason(ack, recent=ctx.recent_lines, answer=ctx.candidate_text) if ack else "empty"
    if rejected:
        trace_event("interviewer", "talker_ack_rejected", session_id=str(ctx.session_id), reason=rejected)
    prediction = args.get("prediction") if isinstance(args.get("prediction"), dict) else {}
    return TalkerOutput(
        acknowledgment=None if rejected else ack,
        acknowledgment_rejected=rejected,
        predicted_action=str(prediction.get("action") or ""),
        predicted_target=str(prediction.get("target") or "").strip(),
        predicted_response=str(prediction.get("response") or "").strip(),
        model=model,
        raw=args,
    )
