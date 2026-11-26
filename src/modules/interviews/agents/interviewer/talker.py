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

**Repurposed (brief 2026-11-26): context-aware streaming backchanneling.**
:func:`backchannel_by_talker` is the same Talker -- same model, same facade,
same acknowledgement guard -- doing a different job: while the candidate is
speaking or the Thinker is deciding, it reads a compact rolling context (the
question, the last few lines, the stable transcript so far, whether the
candidate is still talking, how long since it last spoke) and *decides* to
stay silent or to say one short, contextually fitting line. Its output is
streamed as tokens and cut into complete validated units before anyone hears
it. It predicts nothing, evaluates nothing, sees no rubric, no CV or JD, no
action menu and no probe target, and mutates no state. The prediction path
(:func:`predict_by_talker`) stays for the Hybrid route, which remains off.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from src.core.config import get_settings
from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_text_stream, generate_tool_calls
from src.modules.interviews.agents.interviewer.prompts.decide import decide_input
from src.modules.interviews.agents.interviewer.tools import UNIFIED_TEXT_ACTIONS
from src.modules.interviews.core.conversation.turn_context import TurnContext
from src.modules.interviews.core.guardrails.output import acknowledgement_rejection_reason

logger = logging.getLogger("InterviewTalker")

PURPOSE = "interviews.agent.talker"
TOOL_NAME = "talker_prediction"
ToolCaller = Callable[..., Awaitable[List[Dict[str, Any]]]]

__all__ = [
    "BACKCHANNEL_KINDS",
    "BACKCHANNEL_PURPOSE",
    "BackchannelContext",
    "TalkerOutput",
    "backchannel_by_talker",
    "backchannel_enabled",
    "backchannel_input",
    "backchannel_prompt",
    "parse_kind",
    "predict_by_talker",
    "split_units",
    "talker_model",
    "TOOL_NAME",
]

BACKCHANNEL_PURPOSE = "interviews.agent.talker.backchannel"
#: What the Talker may answer on its first line. ``silence`` is a decision too.
BACKCHANNEL_KINDS = ("silence", "minimal", "reflective", "contextual", "processing")
_KIND_RE = re.compile(r"^\s*kind\s*:\s*([a-z_]+)\s*$", re.IGNORECASE)
_UNIT_END_RE = re.compile(r"(?<=[.!?…])\s+")


def backchannel_enabled() -> bool:
    """On when the setting is set *and* a Talker model is configured."""
    settings = get_settings()
    return bool(settings.interview_backchannel_enabled) and bool(talker_model())


@dataclass(frozen=True)
class BackchannelContext:
    """The compact rolling context the Talker sees -- and nothing else.

    No rubric, no CV or JD text, no action menu, no probe target, no
    evaluation: the Talker keeps the conversation's rhythm; the Thinker
    keeps the interview.
    """

    session_id: str
    question: str
    transcript: str
    phase: str  # listening | processing
    speaking: bool
    is_vi: bool = True
    recent_lines: Sequence[str] = ()
    last_backchannels: Sequence[str] = ()
    seconds_since_last: Optional[float] = None
    answer_seconds: Optional[float] = None
    fragments: int = 0


def backchannel_prompt(is_vi: bool) -> str:
    if is_vi:
        return (
            "# Vai trò\n"
            "Bạn là giọng đồng hành của người phỏng vấn Intervia trong một buổi phỏng vấn bằng giọng nói. "
            "Người phỏng vấn chính đang lắng nghe hoặc đang cân nhắc; việc của bạn chỉ là giữ nhịp trò chuyện "
            "tự nhiên như một người đang chăm chú nghe.\n\n"
            "# Nhiệm vụ\n"
            "Đọc câu hỏi đang hỏi, vài câu gần đây, phần ứng viên đã nói tới giờ (bản ghi còn dang dở, có thể sai vài chữ), "
            "trạng thái và các tín hiệu, rồi QUYẾT ĐỊNH: im lặng, hoặc nói MỘT câu rất ngắn (tối đa 12 từ) "
            "cho thấy bạn đang theo dõi đúng điều họ vừa nói.\n\n"
            "# Định dạng trả lời, bắt buộc\n"
            "Dòng 1: `kind: silence` hoặc `kind: minimal` hoặc `kind: reflective` hoặc `kind: contextual` hoặc `kind: processing`.\n"
            "Dòng 2 (chỉ khi không im lặng): câu nói, nguyên văn, một câu, không ngoặc kép.\n"
            "- minimal: một tiếng ngắn cho biết đang nghe. reflective: nhắc tới điều họ vừa nêu bằng lời của bạn. "
            "contextual: bám vào ý cụ thể đang nói. processing: khi phase là processing, cho biết bạn đang xem xét điều họ vừa nói.\n\n"
            "# Khi nào im lặng\n"
            "- Ứng viên đang nói dở một ý, chưa có điểm dừng tự nhiên, hoặc `speaking` là true.\n"
            "- Vừa có một câu đệm cách đây chưa lâu (xem `seconds_since_last_backchannel`).\n"
            "- Bản ghi chưa đủ để hiểu họ đang nói về gì.\n"
            "Im lặng là lựa chọn đúng trong phần lớn trường hợp.\n\n"
            "# Không bao giờ\n"
            "- Không nhận xét đúng/sai, không khen, không chê, không chấm điểm, không gợi ý đáp án, không nêu ý còn thiếu.\n"
            "- Không đặt câu hỏi, không chuyển chủ đề, không hứa chuyển câu, không nhắc tiêu chí đánh giá.\n"
            "- Không nhắc lại nguyên văn lời ứng viên, không bịa chi tiết họ chưa nói, không dùng chữ 'ghi nhận'.\n"
            "- Không lặp lại câu đệm đã nói (xem `last_backchannels`)."
        )
    return (
        "# Role\n"
        "You are the companion voice of Intervia's interviewer in a spoken interview. The main interviewer is "
        "listening or deciding; your only job is to keep the conversation's rhythm, as an attentive listener would.\n\n"
        "# Task\n"
        "Read the current question, the last few lines, what the candidate has said so far (a partial transcript, "
        "possibly with recognition errors), the status and the signals, then DECIDE: stay silent, or say ONE very "
        "short line (at most 12 words) that shows you are following what they just said.\n\n"
        "# Required format\n"
        "Line 1: `kind: silence` | `kind: minimal` | `kind: reflective` | `kind: contextual` | `kind: processing`.\n"
        "Line 2 (only when not silent): the line itself, one sentence, no quotes.\n\n"
        "# When to stay silent\n"
        "- The candidate is mid-thought, there is no natural pause, or `speaking` is true.\n"
        "- A backchannel was said a moment ago (see `seconds_since_last_backchannel`).\n"
        "- The transcript is not enough to tell what they are talking about.\n"
        "Silence is the right choice most of the time.\n\n"
        "# Never\n"
        "- No right/wrong, no praise, no criticism, no grading, no hints, no naming what is missing.\n"
        "- No questions, no topic change, no promise to move on, no mention of assessment criteria.\n"
        "- Do not quote the candidate verbatim, invent nothing they did not say.\n"
        "- Do not repeat an earlier backchannel (see `last_backchannels`)."
    )


def backchannel_input(ctx: BackchannelContext) -> str:
    """The rolling context, as the model reads it. Compact on purpose."""
    payload = {
        "question": ctx.question,
        "recent_interviewer_lines": list(ctx.recent_lines)[-4:],
        "candidate_so_far": ctx.transcript,
        "phase": ctx.phase,
        "speaking": bool(ctx.speaking),
        "signals": {
            "seconds_since_last_backchannel": ctx.seconds_since_last,
            "answer_seconds": ctx.answer_seconds,
            "fragments": ctx.fragments,
            "words_so_far": len(ctx.transcript.split()),
        },
        "last_backchannels": list(ctx.last_backchannels)[-3:],
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_kind(line: str) -> Optional[str]:
    match = _KIND_RE.match(line or "")
    if not match:
        return None
    kind = match.group(1).lower()
    return kind if kind in BACKCHANNEL_KINDS else None


def split_units(text: str) -> tuple[list[str], str]:
    """Complete sentences so far, and the unfinished tail.

    A unit is a sentence that ended (``.``, ``!``, ``?``, ``…``) followed by
    whitespace; the tail is kept until more text arrives or the stream ends.
    """
    parts = _UNIT_END_RE.split(text)
    if not parts:
        return [], ""
    *done, tail = parts
    return [p.strip() for p in done if p.strip()], tail


StreamGenerator = Callable[..., AsyncIterator[str]]


async def backchannel_by_talker(
    ctx: BackchannelContext,
    *,
    generate_stream: Optional[StreamGenerator] = None,
    model: Optional[str] = None,
    max_words: Optional[int] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Stream the Talker's backchannel as validated units, then one ``done`` record.

    Yields ``{"type": "unit", "text", "kind", "index", "ttft_ms", "at_ms"}``
    for each complete sentence the guard accepted, in order, and finally
    ``{"type": "done", "kind", "ttft_ms", "total_ms", "units", "rejected",
    "reason"}``. ``kind == "silence"`` ends the call with no unit: the model
    chose not to speak, and the stream is closed as soon as that line is
    read, so the choice costs the fewest tokens it can.

    Never raises: a failure is a ``done`` with ``reason``. The guard is the
    Arbiter's :func:`arbitrate_backchannel` (the acknowledgement guard plus a
    length cap), applied to every unit before it leaves here.
    """
    from src.modules.interviews.agents.interviewer.arbiter import arbitrate_backchannel

    model = (model or talker_model()).strip()
    limit = max_words or int(get_settings().interview_backchannel_max_words)
    if not model:
        yield {"type": "done", "kind": None, "ttft_ms": None, "total_ms": 0, "units": 0, "rejected": [], "reason": "no_model"}
        return
    generate_stream = generate_stream or generate_text_stream
    started = perf_counter()
    ttft_ms: Optional[int] = None
    kind: Optional[str] = None
    buffer = ""
    body = ""
    units = 0
    rejected: list[str] = []
    reason: Optional[str] = None

    def ms() -> int:
        return round((perf_counter() - started) * 1000)

    async def emit(text: str) -> Optional[Dict[str, Any]]:
        nonlocal units
        why = arbitrate_backchannel(text, recent=ctx.recent_lines, answer=ctx.transcript, max_words=limit,
                                    last_backchannels=ctx.last_backchannels)
        if why:
            rejected.append(why)
            trace_event("interviewer", "talker_backchannel_rejected", session_id=ctx.session_id, reason=why,
                        text=text[:120])
            return None
        units += 1
        return {"type": "unit", "text": text, "kind": kind, "index": units, "ttft_ms": ttft_ms, "at_ms": ms()}

    try:
        stream = generate_stream(
            instructions=backchannel_prompt(ctx.is_vi),
            input_text=backchannel_input(ctx),
            max_output_tokens=80,
            purpose=BACKCHANNEL_PURPOSE,
            reasoning_effort="none",
            model=model,
        )
        async for delta in stream:
            if ttft_ms is None:
                ttft_ms = ms()
            if kind is None:
                buffer += delta
                if "\n" not in buffer:
                    continue
                first, rest = buffer.split("\n", 1)
                kind = parse_kind(first)
                if kind is None:
                    reason = "bad_format"
                    break
                if kind == "silence":
                    break
                body = rest
            else:
                body += delta
            done, tail = split_units(body)
            for sentence in done:
                unit = await emit(sentence)
                if unit is not None:
                    yield unit
            body = tail
        if kind is None and reason is None:
            # The stream ended on one line: no newline means no body.
            kind = parse_kind(buffer)
            reason = None if kind == "silence" else ("bad_format" if kind is None else None)
            if kind not in (None, "silence"):
                body = ""
        if kind not in (None, "silence") and body.strip():
            unit = await emit(body.strip())
            if unit is not None:
                yield unit
    except Exception as exc:  # noqa: BLE001 - the Talker must never cost the candidate anything
        reason = f"error:{type(exc).__name__}"
        trace_event("interviewer", "talker_error", session_id=ctx.session_id, error_type=type(exc).__name__)
        logger.warning("talker backchannel failed: %s", exc)
    if kind and kind != "silence" and units == 0 and reason is None:
        reason = "all_rejected" if rejected else "empty"
    yield {"type": "done", "kind": kind, "ttft_ms": ttft_ms, "total_ms": ms(), "units": units,
           "rejected": rejected, "reason": reason}


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
