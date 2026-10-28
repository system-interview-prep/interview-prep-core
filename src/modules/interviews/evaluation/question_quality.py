"""Quality signals of the questions actually asked, per stage and source.

The evaluation pipeline grades candidate answers; this reads the same stored
sessions to judge the questions themselves: were they answered, did they need
a follow-up or a re-explanation, did they fit the planned difficulty and the
time box, did any repeat, drift in language or leak grading words. Nothing
here calls a model, so the numbers are reproducible for the thesis.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.core.interview_engine import PROHIBITED_LEAK_PATTERNS

_VI_LETTER = re.compile(
    "[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE
)
_LEAK = [re.compile(pattern, re.IGNORECASE) for pattern in PROHIBITED_LEAK_PATTERNS]
_ANSWERED = {"ANSWERED", "EVALUATED"}


def _language_ok(text_value: str, locale: str) -> bool:
    marks = len(_VI_LETTER.findall(text_value or ""))
    # A Vietnamese question may keep English terms, so it only needs to read as
    # Vietnamese; an English one must carry no Vietnamese letters.
    return marks >= 3 if locale.lower().startswith("vi") else marks == 0


def _leaks(text_value: str) -> bool:
    return any(pattern.search(text_value or "") for pattern in _LEAK)


# Turns frozen before every snapshot carried questionSource: name them from
# their stable key, marked "~" so inferred labels stay distinguishable.
_LEGACY_SOURCES = {"warmup-intro": "~warmup_template", "cv-validation": "~cv_template"}


def _source(row: Mapping[str, Any]) -> str:
    if row.get("source"):
        return str(row["source"])
    if row.get("stable_key") in _LEGACY_SOURCES:
        return _LEGACY_SOURCES[str(row["stable_key"])]
    if row.get("stage") == "CLOSING":
        return "~closing_template"
    return "unlabelled"


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def summarize_question_quality(
    turns: Iterable[Mapping[str, Any]],
    assistant_messages: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Aggregate stored turns into per-(stage, source) question signals.

    ``turns`` rows: session_id, user_id, turn_id, status, stage, source,
    difficulty, plan_difficulty, question_version_id, question_text, locale,
    score, answer_seconds, hard_answer_seconds. ``assistant_messages`` rows:
    turn_id, message_type (PROBE/CLARIFY), content, generated_by.
    """

    followups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in assistant_messages:
        followups[str(message["turn_id"])].append(message)

    rows = [dict(row) for row in turns]
    exposure: Counter[tuple[str, str]] = Counter()
    seen_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row.get("question_version_id"):
            key = (str(row["user_id"]), str(row["question_version_id"]))
            seen_sessions[key].add(str(row["session_id"]))
    exposure.update({key: len(sessions) for key, sessions in seen_sessions.items()})

    within_session: Counter[tuple[str, str]] = Counter(
        (str(row["session_id"]), str(row["question_version_id"])) for row in rows if row.get("question_version_id")
    )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row.get("stage") or "UNKNOWN", _source(row))].append(row)

    report_groups = []
    for (stage, source), items in sorted(groups.items()):
        asked = [item for item in items if item.get("status") != "PLANNED"]
        answered = [item for item in asked if item.get("status") in _ANSWERED]
        scores = [float(item["score"]) for item in asked if item.get("score") is not None]
        timed = [item for item in answered if item.get("answer_seconds") is not None]
        over_time = [
            item
            for item in timed
            if item.get("hard_answer_seconds") and float(item["answer_seconds"]) > float(item["hard_answer_seconds"])
        ]
        probes = [m for item in asked for m in followups.get(str(item["turn_id"]), []) if m["message_type"] == "PROBE"]
        clarifies = [
            m for item in asked for m in followups.get(str(item["turn_id"]), []) if m["message_type"] == "CLARIFY"
        ]
        planned = [item for item in asked if item.get("plan_difficulty") and item.get("difficulty")]
        repeated = [
            item
            for item in asked
            if item.get("question_version_id")
            and exposure[(str(item["user_id"]), str(item["question_version_id"]))] > 1
        ]
        duplicated = [
            item
            for item in asked
            if item.get("question_version_id")
            and within_session[(str(item["session_id"]), str(item["question_version_id"]))] > 1
        ]
        report_groups.append(
            {
                "stage": stage,
                "source": source,
                "asked": len(asked),
                "answeredRate": _rate(len(answered), len(asked)),
                "skippedRate": _rate(sum(1 for item in asked if item.get("status") == "SKIPPED"), len(asked)),
                "meanScore": round(statistics.fmean(scores), 2) if scores else None,
                # Spread, not the mean, shows whether a question separates
                # stronger from weaker answers.
                "scoreStdDev": round(statistics.pstdev(scores), 2) if len(scores) > 1 else None,
                "meanAnswerSeconds": round(statistics.fmean(float(i["answer_seconds"]) for i in timed), 1)
                if timed
                else None,
                "overHardTimeRate": _rate(len(over_time), len(timed)),
                "probesPerTurn": _rate(len(probes), len(asked)),
                "llmProbeShare": _rate(sum(1 for m in probes if m.get("generated_by") == "llm"), len(probes)),
                "unlabelledProbeShare": _rate(sum(1 for m in probes if not m.get("generated_by")), len(probes)),
                "clarifiesPerTurn": _rate(len(clarifies), len(asked)),
                "difficultyMatchRate": _rate(
                    sum(1 for item in planned if item["difficulty"] == item["plan_difficulty"]), len(planned)
                ),
                "repeatedAcrossSessionsRate": _rate(len(repeated), len(asked)),
                "duplicateInSessionCount": len(duplicated),
                "wrongLanguageCount": sum(
                    1 for item in asked if not _language_ok(item.get("question_text") or "", item.get("locale") or "vi-VN")
                ),
                "leakCount": sum(1 for item in asked if _leaks(item.get("question_text") or ""))
                + sum(1 for m in [*probes, *clarifies] if _leaks(m.get("content") or "")),
            }
        )

    asked_all = [row for row in rows if row.get("status") != "PLANNED"]
    sources = Counter(_source(row) for row in asked_all)
    return {
        "sessions": len({str(row["session_id"]) for row in rows}),
        "askedTurns": len(asked_all),
        "sourceMix": dict(sources.most_common()),
        "planDifficulty": dict(
            Counter(
                f"{row.get('plan_difficulty') or 'none'}/{row.get('plan_difficulty_source') or 'none'}"
                for row in {str(r["session_id"]): r for r in rows}.values()
            ).most_common()
        ),
        "groups": report_groups,
    }


async def load_question_quality_rows(
    db: AsyncSession,
    *,
    since: datetime | None = None,
    include_demo: bool = True,
    exclude_email_pattern: str | None = None,
    session_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the turns and assistant follow-ups that summarize_question_quality needs."""

    filters = ["1 = 1"]
    params: dict[str, Any] = {}
    if since is not None:
        filters.append("s.started_at >= :since")
        params["since"] = since
    if not include_demo:
        filters.append("COALESCE(s.duration_minutes, 25) > 3")
    if exclude_email_pattern:
        filters.append("u.email NOT ILIKE :email_pattern")
        params["email_pattern"] = exclude_email_pattern
    if session_id:
        filters.append("s.id = :session_id")
        params["session_id"] = session_id
    where = " AND ".join(filters)
    turns = (
        await db.execute(
            text(
                f"""
                SELECT t.session_id, s.user_id, t.id AS turn_id, t.turn_index, t.status,
                       t.question_snapshot->>'stage' AS stage,
                       NULLIF(t.question_snapshot->>'questionSource', '') AS source,
                       t.question_snapshot->>'stableKey' AS stable_key,
                       t.question_snapshot->>'difficulty' AS difficulty,
                       p.plan_payload->'difficulty'->>'level' AS plan_difficulty,
                       p.plan_payload->'difficulty'->>'source' AS plan_difficulty_source,
                       t.question_version_id, t.question_snapshot->>'questionText' AS question_text,
                       COALESCE(t.question_snapshot->>'locale', s.locale, 'vi-VN') AS locale,
                       e.score,
                       EXTRACT(EPOCH FROM (t.completed_at - t.started_at)) AS answer_seconds,
                       NULLIF(t.question_snapshot->>'hardAnswerSeconds', '')::numeric AS hard_answer_seconds
                FROM interview_turns t
                JOIN interview_sessions s ON s.id = t.session_id
                JOIN users u ON u.id = s.user_id
                LEFT JOIN interview_session_plans p ON p.session_id = t.session_id
                LEFT JOIN LATERAL (
                    SELECT CASE WHEN te.outcome = 'NOT_GRADABLE' THEN NULL ELSE te.score END AS score
                    FROM interview_turn_evaluations te
                    WHERE te.turn_id = t.id ORDER BY te.created_at DESC LIMIT 1
                ) e ON TRUE
                WHERE {where}
                ORDER BY t.session_id, t.turn_index
                """
            ),
            params,
        )
    ).mappings().all()
    messages = (
        await db.execute(
            text(
                f"""
                SELECT m.turn_id, m.message_type, m.content, m.metadata->>'generatedBy' AS generated_by
                FROM interview_chat_messages m
                JOIN interview_sessions s ON s.id = m.session_id
                JOIN users u ON u.id = s.user_id
                WHERE m.role = 'assistant' AND m.message_type IN ('PROBE', 'CLARIFY') AND {where}
                """
            ),
            params,
        )
    ).mappings().all()
    return [dict(row) for row in turns], [dict(row) for row in messages]


async def question_quality_report(db: AsyncSession, **filters: Any) -> dict[str, Any]:
    turns, messages = await load_question_quality_rows(db, **filters)
    return summarize_question_quality(turns, messages)


async def session_plan_audit(db: AsyncSession, session_id: str) -> dict[str, Any] | None:
    """One session as planned and as asked, for the admin plan view.

    Shows what the planner decided (duration budget, difficulty and why,
    targets) and, per turn, where the question came from, its rubric and
    every follow-up with who wrote it.
    """

    session = (
        await db.execute(
            text(
                """
                SELECT s.id, s.status, s.mode, s.duration_minutes, s.locale, s.started_at, s.ended_at,
                       s.end_reason, j.title AS job_title, j.company_name,
                       u.email AS candidate_email, u.name AS candidate_name,
                       p.status AS plan_status, p.plan_payload
                FROM interview_sessions s
                LEFT JOIN job_descriptions j ON j.id = s.job_id
                LEFT JOIN users u ON u.id = s.user_id
                LEFT JOIN interview_session_plans p ON p.session_id = s.id
                WHERE s.id = :sid
                """
            ),
            {"sid": session_id},
        )
    ).mappings().one_or_none()
    if session is None:
        return None
    payload = session["plan_payload"] or {}
    selection = payload.get("questionSelection") or {}

    turns, messages = await load_question_quality_rows(db, session_id=session_id)
    snapshots = {
        str(row["id"]): row["question_snapshot"] or {}
        for row in (
            await db.execute(
                text("SELECT id, question_snapshot FROM interview_turns WHERE session_id = :sid"),
                {"sid": session_id},
            )
        ).mappings()
    }
    followups = (
        await db.execute(
            text(
                """
                SELECT turn_id, message_type, content, metadata, created_at
                FROM interview_chat_messages
                WHERE session_id = :sid AND role = 'assistant'
                  AND (message_type IN ('PROBE', 'CLARIFY') OR metadata->>'is_qna_answer' = 'true')
                ORDER BY sequence
                """
            ),
            {"sid": session_id},
        )
    ).mappings().all()
    by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in followups:
        metadata = message["metadata"] or {}
        by_turn[str(message["turn_id"])].append(
            {
                "type": "QNA_ANSWER" if metadata.get("is_qna_answer") else message["message_type"],
                "content": message["content"],
                "generatedBy": metadata.get("generatedBy"),
                "missingAspect": metadata.get("missingAspect"),
            }
        )

    turn_views = []
    for row in turns:
        snap = snapshots.get(str(row["turn_id"]), {})
        rubric = snap.get("rubric") or {}
        criteria = rubric.get("criteria") if isinstance(rubric, dict) else None
        turn_views.append(
            {
                "turnId": row["turn_id"],
                "turnIndex": row["turn_index"],
                "status": row["status"],
                "stage": row["stage"],
                "source": _source(row),
                "difficulty": row["difficulty"],
                "questionVersionId": str(row["question_version_id"]) if row["question_version_id"] else None,
                "questionText": row["question_text"],
                "objective": snap.get("objective"),
                "target": (snap.get("taxonomyTarget") or {}).get("label"),
                "expectedPoints": [
                    point.get("text") or point.get("description") or str(point)
                    for point in (snap.get("expectedPoints") or [])
                    if point
                ],
                "rubricCriteria": [
                    c.get("name") or c.get("key") for c in (criteria or []) if isinstance(c, dict)
                ],
                "softAnswerSeconds": snap.get("softAnswerSeconds"),
                "hardAnswerSeconds": snap.get("hardAnswerSeconds"),
                "answerSeconds": round(float(row["answer_seconds"]), 1) if row["answer_seconds"] is not None else None,
                "score": float(row["score"]) if row["score"] is not None else None,
                "languageOk": _language_ok(row["question_text"] or "", row["locale"] or "vi-VN"),
                "followups": by_turn.get(str(row["turn_id"]), []),
            }
        )

    return {
        "session": {
            "id": session["id"],
            "status": session["status"],
            "mode": session["mode"],
            "durationMinutes": session["duration_minutes"],
            "locale": session["locale"],
            "startedAt": session["started_at"].isoformat() if session["started_at"] else None,
            "endedAt": session["ended_at"].isoformat() if session["ended_at"] else None,
            "endReason": session["end_reason"],
            "jobTitle": session["job_title"],
            "companyName": session["company_name"],
            "candidateEmail": session["candidate_email"],
            "candidateName": session["candidate_name"],
        },
        "plan": {
            "status": session["plan_status"],
            "policyVersion": payload.get("policyVersion"),
            "questionBudget": payload.get("questionBudget"),
            "difficulty": payload.get("difficulty"),
            "sections": payload.get("sections") or [],
            "targets": [
                {
                    "label": target.get("label"),
                    "importance": target.get("importance"),
                    "questionCount": target.get("targetQuestionCount"),
                    "matchStatuses": (target.get("rationale") or {}).get("matchStatuses") or [],
                    "priorities": (target.get("rationale") or {}).get("priorities") or [],
                }
                for target in payload.get("targets") or []
            ],
            "questionSources": selection.get("questionSources") or {},
            "generatedQuestionCount": selection.get("generatedQuestionCount"),
            "uncoveredTargets": selection.get("uncoveredTargets") or [],
        },
        "turns": turn_views,
        "quality": summarize_question_quality(turns, messages),
    }
