"""How the interviewer's speech was controlled, across sessions, for the admin page.

Reads the ``turn_spoken`` pipeline events over a time window. Each one carries
what the speech guards did in that turn (``speech_checks``, written by
core/speech_checks.py since 25/10), where each part of the reply came from
(``speech_plan``), the acknowledgement source and the reply text. Answers:

* how often each guard let the AI's line through, rejected it, edited it or
  lost the model call, and for which reasons;
* where acknowledgements and questions came from (AI, scripted, bank…);
* house style on what was actually said: "nhé" per turn, the share of
  turns opening with "À/Ừ/Ừm", repeated acknowledgements;
* the latest rejected or edited drafts, linked to their session.

Turns recorded before 25/10 have no ``speech_checks``: they count for sources
and style but not for the guard table (``turns.withChecks`` says how many do).
``summarize_speech`` is pure and unit tested; ``load_speech_control`` only
runs the query and reads the switches.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import llm_tracing
from src.modules.interviews.application.pipeline_health import rate
from src.modules.interviews.core.interviewer_voice import (
    INTERJECTION_SHARE,
    INTERJECTION_WINDOW,
    MAX_NHE_PER_TURN,
    ack_mode,
    count_nhe,
    first_paragraph,
    opening_interjection,
    speech_v2_enabled,
    voice_dialect,
)
from src.modules.interviews.core.speech_checks import KINDS

ROW_LIMIT = 5000
RECENT_LIMIT = 40
TERM_LIMIT = 15
REVIEW_VERDICTS = ("correct", "wrong")


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value) if value else None


def config_view() -> dict[str, Any]:
    """The switches as this API process reads them (the voice agent reads its own .env)."""
    from src.core.config import get_settings

    return {
        "speechV2": speech_v2_enabled(),
        "ackMode": ack_mode(),
        "dialect": voice_dialect(),
        "maxNhePerTurn": MAX_NHE_PER_TURN,
        "interjectionShare": INTERJECTION_SHARE,
        "interjectionWindow": INTERJECTION_WINDOW,
        "model": get_settings().llm_model,
    }


def latest_verdicts(reviews: list[dict[str, Any]]) -> dict[tuple[int, int], str]:
    """(event id, check index) -> the admin's last verdict ("correct" | "wrong"); "clear" removes it.

    Reviews come oldest first (``speech_check_reviewed`` events).
    """
    verdicts: dict[tuple[int, int], str] = {}
    for review in reviews:
        payload = review.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        try:
            key = (int(payload["event_id"]), int(payload["check_index"]))
        except (KeyError, TypeError, ValueError):
            continue
        verdict = str(payload.get("verdict") or "")
        if verdict in REVIEW_VERDICTS:
            verdicts[key] = verdict
        elif verdict == "clear":
            verdicts.pop(key, None)
    return verdicts


def summarize_speech(
    rows: list[dict[str, Any]], verdicts: dict[tuple[int, int], str] | None = None
) -> dict[str, Any]:
    """Rows: ``turn_spoken`` events (id, session_id, turn_index, created_at, payload), newest first.

    ``verdicts``: the admins' hand checks of refused or edited lines (``latest_verdicts``).
    """
    verdicts = verdicts or {}
    reviewed: Counter[str] = Counter()
    wrong: Counter[str] = Counter()
    sessions: set[str] = set()
    with_checks = 0
    guard: dict[str, Counter[str]] = {kind: Counter() for kind in KINDS}
    guard_turns: dict[str, int] = Counter()
    reasons: Counter[tuple[str, str, str]] = Counter()
    reason_sessions: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    ack_sources: Counter[str] = Counter()
    question_sources: Counter[str] = Counter()
    terms: Counter[str] = Counter()
    recent: list[dict[str, Any]] = []
    acks = interjections = over_nhe = repeats = term_turns = 0

    for row in rows:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        session_id = str(row.get("session_id") or "")
        sessions.add(session_id)
        checks = [c for c in payload.get("speech_checks") or [] if isinstance(c, dict)]
        if checks:
            with_checks += 1
        kinds_in_turn: set[str] = set()
        event_id = row.get("id")
        for index, check in enumerate(checks):
            kind, outcome = str(check.get("kind") or ""), str(check.get("outcome") or "")
            if kind not in guard:
                continue
            guard[kind][outcome] += 1
            kinds_in_turn.add(kind)
            if outcome == "pass":
                continue
            verdict = verdicts.get((int(event_id), index)) if event_id is not None else None
            if verdict:
                reviewed[kind] += 1
                wrong[kind] += verdict == "wrong"
            key = (kind, outcome, str(check.get("reason") or "-"))
            reasons[key] += 1
            reason_sessions[key].add(session_id)
            if len(recent) < RECENT_LIMIT:
                recent.append(
                    {
                        "at": _iso(row.get("created_at")),
                        "sessionId": session_id,
                        "turnIndex": row.get("turn_index"),
                        "kind": kind,
                        "outcome": outcome,
                        "reason": check.get("reason"),
                        "draft": check.get("draft"),
                        "said": str(payload.get("interviewer") or "")[:300] or None,
                        "eventId": event_id,
                        "checkIndex": index,
                        "verdict": verdict,
                    }
                )
        for kind in kinds_in_turn:
            guard_turns[kind] += 1

        if payload.get("ack_source"):
            ack_sources[str(payload["ack_source"])] += 1
        for step in payload.get("speech_plan") or []:
            if isinstance(step, dict) and step.get("act") == "QUESTION":
                question_sources[str(step.get("source") or "-")] += 1

        said = str(payload.get("interviewer") or "")
        ack = str(payload.get("acknowledgement") or "") or (first_paragraph(said) if "\n\n" in said else "")
        if ack:
            acks += 1
            interjections += bool(opening_interjection(ack))
        over_nhe += count_nhe(said) > MAX_NHE_PER_TURN
        repeats += bool(payload.get("repeats_recent"))
        turn_terms = [str(t) for t in payload.get("new_terms") or []]
        term_turns += bool(turn_terms)
        terms.update(turn_terms)

    turns = len(rows)
    guards = []
    for kind in KINDS:
        counts = guard[kind]
        attempts = counts["pass"] + counts["reject"] + counts["error"]
        guards.append(
            {
                "kind": kind,
                "turns": guard_turns[kind],
                "pass": counts["pass"],
                "reject": counts["reject"],
                "fix": counts["fix"],
                "error": counts["error"],
                # Drafts refused out of the drafts the model actually wrote.
                "rejectRate": rate(counts["reject"], counts["pass"] + counts["reject"]),
                "errorRate": rate(counts["error"], attempts),
            }
        )
    return {
        "turns": {"count": turns, "sessions": len(sessions - {""}), "withChecks": with_checks},
        "guards": guards,
        "reasons": [
            {
                "kind": kind,
                "outcome": outcome,
                "reason": reason,
                "count": count,
                "sessions": len(reason_sessions[(kind, outcome, reason)]),
            }
            for (kind, outcome, reason), count in reasons.most_common()
        ],
        "sources": {
            "ack": [{"source": s, "count": n} for s, n in ack_sources.most_common()],
            "question": [{"source": s, "count": n} for s, n in question_sources.most_common()],
        },
        "style": {
            "interjectionOpenings": rate(interjections, acks),
            "overNhe": rate(over_nhe, turns),
            "repeatedAck": rate(repeats, turns),
            "newTermsTurns": rate(term_turns, turns),
        },
        "newTerms": [{"term": t, "count": n} for t, n in terms.most_common(TERM_LIMIT)],
        "recent": recent,
        # Lines an admin marked: a refusal of a good line is a false positive of the guard.
        "review": {
            "falseBlocks": rate(sum(wrong.values()), sum(reviewed.values())),
            "byKind": [
                {"kind": kind, "falseBlocks": rate(wrong[kind], reviewed[kind])}
                for kind in KINDS
                if reviewed[kind]
            ],
        },
    }


async def load_speech_control(db: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    result = await db.execute(
        text(
            """
            SELECT id, session_id, turn_index, created_at, payload
            FROM interview_pipeline_events
            WHERE event = 'turn_spoken' AND created_at >= :since
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"since": since, "limit": ROW_LIMIT},
    )
    rows = [dict(row) for row in result.mappings().all()]
    reviews = await db.execute(
        text(
            """
            SELECT payload FROM interview_pipeline_events
            WHERE event = 'speech_check_reviewed' AND created_at >= :since
            ORDER BY created_at, id
            """
        ),
        {"since": since},
    )
    verdicts = latest_verdicts([dict(row) for row in reviews.mappings().all()])
    return {
        "windowDays": days,
        "since": since.isoformat(),
        "truncated": len(rows) >= ROW_LIMIT,
        "config": config_view(),
        **summarize_speech(rows, verdicts),
        "generatedAt": datetime.now(UTC).isoformat(),
    }


async def review_speech_check(
    db: AsyncSession, *, event_id: int, check_index: int, verdict: str, admin_id: str | None
) -> dict[str, Any] | None:
    """Store an admin's verdict on one refused or edited line; None when there is no such line.

    The verdict is one more pipeline event (``speech_check_reviewed``) on the
    same session, so it follows the same retention as the line it judges.
    """
    found = (
        await db.execute(
            text(
                "SELECT session_id, turn_index, payload FROM interview_pipeline_events "
                "WHERE id = :id AND event = 'turn_spoken'"
            ),
            {"id": event_id},
        )
    ).mappings().first()
    if not found:
        return None
    payload = found["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    checks = [c for c in (payload or {}).get("speech_checks") or [] if isinstance(c, dict)]
    if not 0 <= check_index < len(checks) or checks[check_index].get("outcome") == "pass":
        return None
    check = checks[check_index]
    await db.execute(
        text(
            "INSERT INTO interview_pipeline_events (session_id, stage, event, status, turn_index, payload) "
            "VALUES (:sid, 'interview', 'speech_check_reviewed', 'info', :turn, CAST(:payload AS jsonb))"
        ),
        {
            "sid": found["session_id"],
            "turn": found["turn_index"],
            "payload": json.dumps(
                {
                    "event_id": event_id,
                    "check_index": check_index,
                    "verdict": verdict,
                    "kind": check.get("kind"),
                    "reason": check.get("reason"),
                    "admin_id": admin_id,
                },
                ensure_ascii=False,
            ),
        },
    )
    await db.commit()
    if verdict != "clear":
        # The same verdict on the turn's Langfuse trace, next to the judge's scores.
        llm_tracing.score_trace(
            (payload or {}).get("langfuse_trace_id"),
            "human.guard_correct",
            1 if verdict == "correct" else 0,
            comment=f"{check.get('kind')}:{check.get('reason')}",
        )
    saved = None if verdict == "clear" else verdict
    return {"eventId": event_id, "checkIndex": check_index, "verdict": saved}
