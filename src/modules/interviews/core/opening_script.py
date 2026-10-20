"""Build concise spoken openings and informative chat introductions.

Built the way structured interviews open: a short introduction, the purpose
and length of the session, the parts it will go through, and an easy first
question. Voice sessions use only a brief greeting before that question; chat
sessions can show the fuller introduction because it is not spoken aloud.
"""

from __future__ import annotations

from collections.abc import Sequence

# How each planned stage is named in the agenda (WARM_UP is the question that
# follows; CLOSING is optional and announced separately).
_STAGE_PHRASES_VI = {
    "VALIDATE": "dự án trong CV",
    "DEEP_DIVE": "kiến thức chuyên môn",
    "CHALLENGE": "một vài tình huống khó hơn",
    "BEHAVIORAL": "cách bạn làm việc nhóm",
}
_STAGE_PHRASES_EN = {
    "VALIDATE": "a project from your CV",
    "DEEP_DIVE": "technical knowledge",
    "CHALLENGE": "a few harder scenarios",
    "BEHAVIORAL": "how you work with a team",
}


def warmup_question(*, is_vi: bool, is_demo: bool) -> str:
    """The first question: easy to start, with a length and a shape to follow."""
    if is_vi:
        if is_demo:
            return "Bạn giới thiệu ngắn về mình trong khoảng ba mươi giây nhé: hiện bạn đang làm gì và vì sao quan tâm vị trí này?"
        # The CV project is asked right after (VALIDATE), so it is not asked here too.
        return (
            "Bạn giới thiệu ngắn về mình trong một, hai phút nhé: "
            "bạn đang làm gì và vì sao quan tâm vị trí này?"
        )
    if is_demo:
        return "Please introduce yourself in about thirty seconds: what you do now and why this role interests you?"
    return (
        "Please introduce yourself in a minute or two: "
        "what you do now and why this role interests you?"
    )


def _join(items: list[str], *, is_vi: bool) -> str:
    if len(items) <= 1:
        return "".join(items)
    conjunction = " và " if is_vi else " and "
    return ", ".join(items[:-1]) + conjunction + items[-1]


def _agenda(stages: Sequence[str], *, is_vi: bool) -> list[str]:
    phrases = _STAGE_PHRASES_VI if is_vi else _STAGE_PHRASES_EN
    ordered: list[str] = []
    for stage in stages:
        phrase = phrases.get(str(stage or "").upper())
        if phrase and phrase not in ordered:
            ordered.append(phrase)
    return ordered


def build_opening(
    *,
    is_vi: bool,
    mode: str,
    duration_minutes: int | None,
    job_title: str | None,
    call_name: str | None,
    planned_stages: Sequence[str],
    first_question: str,
) -> str:
    """Build the opening message, ending with the first question.

    Paragraphs are separated by a blank line. In voice/video mode, the agent
    says one short greeting and moves straight to the question. Chat keeps the
    more detailed introduction for reading.
    """
    is_voice = str(mode or "").lower() in {"voice", "video"}
    is_demo = bool(duration_minutes) and int(duration_minutes) <= 3
    if is_voice:
        # A greeting, one short sentence that sets the frame, then the question.
        # Greeting straight into the question sounded abrupt when spoken.
        greeting = f"Chào {call_name or 'bạn'}." if is_vi else f"Hi {call_name or 'there'}."
        if is_vi:
            frame = (
                "Mình hỏi nhanh vài câu thôi."
                if is_demo or not duration_minutes
                else f"Hôm nay mình luyện phỏng vấn khoảng {duration_minutes} phút."
            )
        else:
            frame = (
                "I'll ask a few quick questions."
                if is_demo or not duration_minutes
                else f"We'll practise for about {duration_minutes} minutes today."
            )
        return f"{greeting} {frame} {first_question}"

    agenda = _agenda(planned_stages, is_vi=is_vi)

    # The chat room already shows the question, the suggestions and the report
    # link, so the opening only says what the room cannot: who, what, how long.
    if is_vi:
        role = f" vị trí {job_title}" if job_title else ""
        greeting = f"Chào {call_name or 'bạn'}, mình là người phỏng vấn AI của Intervia."
        if is_demo:
            # A three-minute demo cannot spend half a minute on preliminaries.
            for_role = f" cho{role}" if role else ""
            paragraphs = [f"{greeting} Buổi demo ba phút{for_role}, mình hỏi nhanh vài câu nhé."]
        else:
            length = f", khoảng {duration_minutes} phút" if duration_minutes else ""
            paragraphs = [f"{greeting} Hôm nay mình cùng luyện phỏng vấn{role}{length}."]
            if agenda:
                paragraphs.append(f"Mình sẽ hỏi về {_join(agenda, is_vi=True)}.")
        how = "" if is_demo else "Bạn cứ suy nghĩ vài giây rồi hãy trả lời."
        start = "" if is_demo else "Mình bắt đầu nhé."
    else:
        role = f" for the {job_title} role" if job_title else ""
        greeting = f"Hi {call_name or 'there'}, I'm the AI interviewer from Intervia."
        if is_demo:
            paragraphs = [f"{greeting} This is a three-minute demo{role}, so I'll ask a few quick questions."]
        else:
            length = f", for about {duration_minutes} minutes" if duration_minutes else ""
            paragraphs = [f"{greeting} Today we'll practise an interview{role}{length}."]
            if agenda:
                paragraphs.append(f"I'll ask about {_join(agenda, is_vi=False)}.")
        how = "" if is_demo else "Take a few seconds to think before you answer."
        start = "" if is_demo else "Let's begin."

    if how:
        paragraphs.append(how)
    # "Let's begin" closes the preamble rather than standing as its own bubble.
    if start:
        paragraphs[-1] = f"{paragraphs[-1]} {start}"
    return "\n\n".join([*paragraphs, first_question])
