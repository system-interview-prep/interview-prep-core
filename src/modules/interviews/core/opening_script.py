"""Build the opening of an interview: one script for chat, voice and video.

Built the way structured interviews open: a short introduction, the length of
the session, the parts it will go through, and an easy first question. Every
room says the same words (wording in ``interviewer_script.py``); the voice
agent only cleans it up for speech.
"""

from __future__ import annotations

from collections.abc import Sequence

from src.modules.interviews.core.interviewer_script import (
    OPENING_AGENDA_EN,
    OPENING_AGENDA_VI,
    OPENING_DEMO_EN,
    OPENING_DEMO_VI,
    OPENING_FRAME_EN,
    OPENING_FRAME_VI,
    OPENING_GREETING_EN,
    OPENING_GREETING_VI,
    OPENING_LENGTH_EN,
    OPENING_LENGTH_VI,
    OPENING_ROLE_EN,
    OPENING_ROLE_VI,
    OPENING_START_EN,
    OPENING_START_VI,
    STAGE_PHRASES_EN,
    STAGE_PHRASES_VI,
    WARMUP_QUESTION_DEMO_EN,
    WARMUP_QUESTION_DEMO_VI,
    WARMUP_QUESTION_EN,
    WARMUP_QUESTION_VI,
)


def warmup_question(*, is_vi: bool, is_demo: bool) -> str:
    """The first question: easy to start, with a length and a shape to follow."""
    if is_vi:
        return WARMUP_QUESTION_DEMO_VI if is_demo else WARMUP_QUESTION_VI
    return WARMUP_QUESTION_DEMO_EN if is_demo else WARMUP_QUESTION_EN


def _join(items: list[str], *, is_vi: bool) -> str:
    if len(items) <= 1:
        return "".join(items)
    conjunction = " và " if is_vi else " and "
    return ", ".join(items[:-1]) + conjunction + items[-1]


def _agenda(stages: Sequence[str], *, is_vi: bool) -> list[str]:
    phrases = STAGE_PHRASES_VI if is_vi else STAGE_PHRASES_EN
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

    Two paragraphs separated by a blank line: the introduction, then the
    question. ``mode`` no longer changes the words; it is kept for callers.
    """
    del mode
    is_demo = bool(duration_minutes) and int(duration_minutes) <= 3
    name = call_name or ("bạn" if is_vi else "there")
    greeting = (OPENING_GREETING_VI if is_vi else OPENING_GREETING_EN).format(name=name)
    if is_demo:
        # A three-minute demo cannot spend half a minute on preliminaries.
        intro = f"{greeting} {OPENING_DEMO_VI if is_vi else OPENING_DEMO_EN}"
        return f"{intro}\n\n{first_question}"

    role = (OPENING_ROLE_VI if is_vi else OPENING_ROLE_EN).format(job_title=job_title) if job_title else ""
    length = (
        (OPENING_LENGTH_VI if is_vi else OPENING_LENGTH_EN).format(minutes=duration_minutes)
        if duration_minutes
        else ""
    )
    sentences = [greeting, (OPENING_FRAME_VI if is_vi else OPENING_FRAME_EN).format(role=role, length=length)]
    agenda = _agenda(planned_stages, is_vi=is_vi)
    if agenda:
        sentences.append((OPENING_AGENDA_VI if is_vi else OPENING_AGENDA_EN).format(agenda=_join(agenda, is_vi=is_vi)))
    sentences.append(OPENING_START_VI if is_vi else OPENING_START_EN)
    return f"{' '.join(sentences)}\n\n{first_question}"
