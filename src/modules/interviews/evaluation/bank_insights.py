"""The question bank learns from real interviews ("AI tự làm giàu question bank").

Design: DOC_AND_PLAN/plan/interview/ADVISOR_FEEDBACK_RESEARCH_2026-10-26.md, section 4.

Reads every graded answer to a bank question and turns what it sees into
proposals a person reviews; nothing here changes the bank by itself.

1. **Item statistics** (classical test theory, adapted to rubric scores 0–10):
   asked, skip rate, over-time rate, mean and spread of the score, empirical
   difficulty (mean / 10) and discrimination (Pearson correlation of the
   question's score with the session's overall score). Written to
   ``question_calibrations`` as an ``auto-YYYYMMDD`` snapshot.
2. **Rules -> suggestions** (``analyze``):

   | kind        | when                                                                  |
   |-------------|-----------------------------------------------------------------------|
   | REVISE      | skipped by > 30 %, over time for > 30 %, scores barely spread         |
   | REVISE      | discrimination < 0.10 (>= 10 graded answers)                          |
   | RETIRE      | discrimination < 0 (>= 10 graded answers): good candidates do worse   |
   | DIFFICULTY  | empirical difficulty > 0.95 (too easy) or < 0.40 (too hard) for the band |
   | RUBRIC_GAP  | one rubric point missed or only partly covered by >= 60 % of answers  |
   | VARIANT     | asked in many sessions, or met twice by the same candidate            |
   | DUPLICATE   | two questions of the same skill say nearly the same (token Jaccard)   |

   Thresholds follow item-analysis practice (p > 0.95 / < 0.40, point-biserial
   >= 0.20 good, < 0.10 revise, negative replace). With fewer than
   ``CALIBRATION_N`` answers a finding is marked ``lowData``: a hint, not a
   calibration (about 30 answers give a rough estimate).
3. **AI proposal for rubric gaps** (``propose_for_gap``): one LLM call names the
   common misconception from up to 5 short, masked quotes of real partial
   answers and drafts a follow-up probe and a level anchor. Grounding the
   proposal in real answers matters: models guess misconceptions poorly on
   their own. The probe must not copy a candidate's words.

Accepting a suggestion (question_bank/insights.py) makes a revision draft,
generates variant drafts through the existing gated generator, or retires the
question; reviewers still approve every new version.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import llm_tracing
from src.modules.ai.facade import generate_text
from src.modules.interviews.planning.question_generation import is_near_duplicate

logger = logging.getLogger(__name__)

MIN_ASKED = 5
MIN_GRADED = 5
MIN_DISCRIMINATION = 10
CALIBRATION_N = 30
SKIP_RATE = 0.30
OVERTIME_RATE = 0.30
FLAT_STDDEV = 0.5
TOO_EASY = 0.95
TOO_HARD = 0.40
WEAK_DISCRIMINATION = 0.10
GAP_RATE = 0.60
VARIANT_SESSIONS = 15
MAX_AI_PROPOSALS = 5
QUOTES_PER_GAP = 5
QUOTE_MAX_CHARS = 200
BANDS = ("foundational", "intermediate", "advanced")
_GAP_STATUSES = {"MISSING", "PARTIAL", "INCORRECT"}


@dataclass
class QuestionStats:
    question_id: str
    version_id: str
    stable_key: str
    text: str
    difficulty: str
    locale: str
    skill: str | None = None
    status: str | None = None
    asked: int = 0
    skipped: int = 0
    overtime: int = 0
    sessions: set[str] = field(default_factory=set)
    repeat_users: int = 0
    scores: list[float] = field(default_factory=list)
    pairs: list[tuple[float, float]] = field(default_factory=list)
    criteria: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    criterion_names: dict[str, str] = field(default_factory=dict)
    quotes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


@dataclass
class Suggestion:
    kind: str
    reason: str
    question_id: str | None
    version_id: str | None
    severity: str = "MEDIUM"
    criterion_key: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    proposal: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return ":".join(
            str(part) for part in (self.kind, self.reason, self.question_id, self.criterion_key or "")
        )


# --------------------------------------------------------------------------- statistics


def pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs, ys = zip(*pairs, strict=True)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in pairs) / (sx * sy)


def summarize(stats: QuestionStats) -> dict[str, Any]:
    """The numbers shown with a suggestion and stored in question_calibrations."""
    graded = len(stats.scores)
    mean = statistics.fmean(stats.scores) if stats.scores else None
    return {
        "asked": stats.asked,
        "sessions": len(stats.sessions),
        "graded": graded,
        "skipRate": round(stats.skipped / stats.asked, 4) if stats.asked else None,
        "overtimeRate": round(stats.overtime / stats.asked, 4) if stats.asked else None,
        "meanScore": round(mean, 3) if mean is not None else None,
        "scoreStdDev": round(statistics.pstdev(stats.scores), 3) if graded >= 2 else None,
        "empiricalDifficulty": round(mean / 10, 4) if mean is not None else None,
        "discrimination": (
            round(value, 4)
            if (value := pearson(stats.pairs)) is not None and len(stats.pairs) >= MIN_DISCRIMINATION
            else None
        ),
        "repeatUsers": stats.repeat_users,
        "lowData": graded < CALIBRATION_N,
    }


def _band_shift(band: str, step: int) -> str | None:
    if band not in BANDS:
        return None
    index = BANDS.index(band) + step
    return BANDS[index] if 0 <= index < len(BANDS) else None


def analyze(stats_by_question: dict[str, QuestionStats]) -> list[Suggestion]:
    """Rules over the item statistics; pure, so the thesis numbers are reproducible."""
    found: list[Suggestion] = []
    for stats in stats_by_question.values():
        summary = summarize(stats)

        def add(
            kind: str, reason: str, severity: str = "MEDIUM", stats=stats, summary=summary, **extra: Any
        ) -> None:
            found.append(
                Suggestion(
                    kind=kind,
                    reason=reason,
                    question_id=stats.question_id,
                    version_id=stats.version_id,
                    severity=severity,
                    criterion_key=extra.pop("criterion_key", None),
                    evidence={**summary, **extra.pop("evidence", {})},
                    proposal=extra.pop("proposal", {}),
                )
            )

        if stats.asked >= MIN_ASKED:
            if summary["skipRate"] is not None and summary["skipRate"] > SKIP_RATE:
                add("REVISE", "skipped_often", "HIGH")
            if summary["overtimeRate"] is not None and summary["overtimeRate"] > OVERTIME_RATE:
                add("REVISE", "over_time")
        if len(stats.scores) >= MIN_GRADED:
            if summary["scoreStdDev"] is not None and summary["scoreStdDev"] < FLAT_STDDEV:
                add("REVISE", "flat_scores", "LOW")
            difficulty = summary["empiricalDifficulty"]
            if difficulty is not None and difficulty > TOO_EASY:
                add("DIFFICULTY", "too_easy", proposal={"difficultyBand": _band_shift(stats.difficulty, 1)})
            elif difficulty is not None and difficulty < TOO_HARD:
                add("DIFFICULTY", "too_hard", proposal={"difficultyBand": _band_shift(stats.difficulty, -1)})
            for key, counts in stats.criteria.items():
                total = sum(counts.values())
                gaps = sum(counts[status] for status in _GAP_STATUSES)
                if total >= MIN_GRADED and gaps / total >= GAP_RATE:
                    add(
                        "RUBRIC_GAP",
                        "criterion_often_missed",
                        criterion_key=key,
                        evidence={
                            "criterion": stats.criterion_names.get(key, key),
                            "criterionAnswers": total,
                            "missedRate": round(gaps / total, 4),
                            "statusCounts": dict(counts),
                        },
                    )
        discrimination = summary["discrimination"]
        if discrimination is not None:
            if discrimination < 0:
                add("RETIRE", "negative_discrimination", "HIGH")
            elif discrimination < WEAK_DISCRIMINATION:
                add("REVISE", "weak_discrimination")
        if len(stats.sessions) >= VARIANT_SESSIONS or stats.repeat_users >= 2:
            add(
                "VARIANT",
                "over_exposed" if len(stats.sessions) >= VARIANT_SESSIONS else "seen_again",
                "LOW",
                proposal={"count": 2, "skill": stats.skill, "difficultyBand": stats.difficulty},
            )
    found += duplicates(stats_by_question)
    return found


def duplicates(stats_by_question: dict[str, QuestionStats]) -> list[Suggestion]:
    """Questions of the same skill that say nearly the same; the later one is the suggestion's subject."""
    by_skill: dict[str, list[QuestionStats]] = defaultdict(list)
    for stats in stats_by_question.values():
        if stats.skill:
            by_skill[stats.skill].append(stats)
    found = []
    for items in by_skill.values():
        items = sorted(items, key=lambda s: s.stable_key)
        for index, later in enumerate(items):
            twin = next(
                (earlier for earlier in items[:index] if is_near_duplicate(later.text, [earlier.text])), None
            )
            if twin is not None:
                found.append(
                    Suggestion(
                        kind="DUPLICATE",
                        reason="near_duplicate",
                        question_id=later.question_id,
                        version_id=later.version_id,
                        severity="LOW",
                        evidence={
                            "twinQuestionId": twin.question_id,
                            "twinStableKey": twin.stable_key,
                            "asked": later.asked,
                        },
                    )
                )
    return found


# --------------------------------------------------------------------------- loading


_TURNS_SQL = """
SELECT t.session_id, s.user_id, t.status, t.question_version_id::text AS version_id,
       COALESCE(q.current_approved_version_id, qv.id)::text AS current_version_id,
       qv.question_id::text AS question_id, q.stable_key, qv.canonical_text, qv.difficulty_band,
       qv.canonical_locale, qv.status AS version_status,
       EXTRACT(EPOCH FROM (t.completed_at - t.started_at)) AS answer_seconds,
       qv.hard_answer_seconds, te.score, te.outcome, te.rubric_points, ev.overall_score
FROM interview_turns t
JOIN interview_sessions s ON s.id = t.session_id
JOIN interview_question_versions qv ON qv.id = t.question_version_id
JOIN interview_questions q ON q.id = qv.question_id
LEFT JOIN LATERAL (
    SELECT score, outcome, rubric_points FROM interview_turn_evaluations
    WHERE turn_id = t.id ORDER BY created_at DESC LIMIT 1
) te ON TRUE
LEFT JOIN interview_evaluations ev ON ev.session_id = t.session_id
WHERE q.retired_at IS NULL
  AND COALESCE(s.duration_minutes, 25) > 3
  AND UPPER(COALESCE(t.status, '')) NOT IN ('PLANNED', 'ASKED')
"""


def collect(
    rows: list[dict[str, Any]], criteria: list[dict[str, Any]], skills: dict[str, str]
) -> dict[str, QuestionStats]:
    """Fold turn rows into per-question statistics (pure; the loader feeds it)."""
    by_question: dict[str, QuestionStats] = {}
    seen: Counter = Counter()
    names = {(str(c["version_id"]), c["stable_key"]): c["name"] for c in criteria}
    for row in rows:
        qid = row["question_id"]
        stats = by_question.get(qid)
        if stats is None:
            stats = by_question[qid] = QuestionStats(
                question_id=qid,
                version_id=row.get("current_version_id") or row["version_id"],
                stable_key=row["stable_key"],
                text=row["canonical_text"] or "",
                difficulty=row["difficulty_band"] or "",
                locale=row["canonical_locale"] or "vi-VN",
                skill=skills.get(row["version_id"]),
                status=row.get("version_status"),
            )
        stats.asked += 1
        stats.sessions.add(str(row["session_id"]))
        seen[(row["user_id"], qid)] += 1
        outcome = str(row.get("outcome") or "").upper()
        if outcome == "SKIPPED" or str(row.get("status") or "").upper() == "SKIPPED":
            stats.skipped += 1
        hard = row.get("hard_answer_seconds")
        if hard and row.get("answer_seconds") and float(row["answer_seconds"]) > float(hard):
            stats.overtime += 1
        if row.get("score") is not None and outcome not in {"NOT_GRADABLE", "SKIPPED"}:
            score = float(row["score"])
            stats.scores.append(score)
            if row.get("overall_score") is not None:
                stats.pairs.append((score, float(row["overall_score"])))
            for point in row.get("rubric_points") or []:
                if not isinstance(point, dict) or not point.get("stable_key"):
                    continue
                key, status = str(point["stable_key"]), str(point.get("status") or "").upper()
                stats.criteria[key][status] += 1
                stats.criterion_names.setdefault(key, names.get((row["version_id"], key), key))
                quote = str(point.get("quote") or "").strip()
                if status in {"PARTIAL", "INCORRECT"} and quote and len(stats.quotes[key]) < QUOTES_PER_GAP:
                    stats.quotes[key].append(quote[:QUOTE_MAX_CHARS])
    for (_, qid), count in seen.items():
        if count > 1:
            by_question[qid].repeat_users += 1
    return by_question


async def load_stats(db: AsyncSession) -> dict[str, QuestionStats]:
    rows = [dict(r) for r in (await db.execute(text(_TURNS_SQL))).mappings().all()]
    for row in rows:
        if isinstance(row.get("rubric_points"), str):
            row["rubric_points"] = json.loads(row["rubric_points"])
    version_ids = sorted({row["version_id"] for row in rows})
    criteria: list[dict[str, Any]] = []
    skills: dict[str, str] = {}
    if version_ids:
        criteria = [
            dict(r)
            for r in (
                await db.execute(
                    text(
                        "SELECT qvr.question_version_id::text AS version_id, rc.stable_key, rc.name "
                        "FROM question_version_rubrics qvr JOIN rubric_criteria rc "
                        "ON rc.rubric_version_id = qvr.rubric_version_id "
                        "WHERE qvr.question_version_id::text = ANY(:ids)"
                    ),
                    {"ids": version_ids},
                )
            )
            .mappings()
            .all()
        ]
        for r in (
            await db.execute(
                text(
                    "SELECT question_version_id::text AS version_id, concept_id "
                    "FROM question_version_taxonomy_concepts "
                    "WHERE purpose = 'TARGET_SKILL' AND question_version_id::text = ANY(:ids)"
                ),
                {"ids": version_ids},
            )
        ).mappings():
            skills.setdefault(r["version_id"], r["concept_id"])
    return collect(rows, criteria, skills)


# --------------------------------------------------------------------------- AI proposal for a rubric gap


_GAP_INSTRUCTIONS = """You help maintain a structured interview question bank.
You get one interview question, one rubric point that most candidates miss or only partly cover,
and a few short (masked) excerpts of real partial answers.
Return ONLY JSON: {"misconception": str, "probe": str, "anchor_partial": str, "anchor_full": str}
- misconception: one sentence naming what candidates typically get wrong or leave out.
- probe: one neutral follow-up question the interviewer can ask to let a candidate cover this point.
  It must not reveal the answer, must not quote the candidates, one idea only, speakable aloud.
- anchor_partial: what a partly correct answer to this point usually says (a rubric anchor).
- anchor_full: what a complete answer to this point says.
Write in the language of the question. Never include names, emails or phone numbers."""


def copies_candidate(text_value: str, quotes: list[str], words: int = 6) -> bool:
    """True when the text repeats ``words`` consecutive words of a candidate quote."""
    tokens = re.findall(r"\w+", text_value.lower())
    grams = {" ".join(tokens[i : i + words]) for i in range(len(tokens) - words + 1)}
    for quote in quotes:
        q = re.findall(r"\w+", quote.lower())
        if any(" ".join(q[i : i + words]) in grams for i in range(len(q) - words + 1)):
            return True
    return False


async def propose_for_gap(stats: QuestionStats, suggestion: Suggestion) -> dict[str, Any] | None:
    """LLM proposal (misconception, probe, anchors) for a RUBRIC_GAP; None when unusable."""
    key = suggestion.criterion_key or ""
    quotes = [llm_tracing.mask(data=q) for q in stats.quotes.get(key, [])]
    raw = await generate_text(
        instructions=_GAP_INSTRUCTIONS,
        input_text=json.dumps(
            {
                "question": stats.text,
                "language": "Vietnamese" if stats.locale.lower().startswith("vi") else "English",
                "rubricPoint": stats.criterion_names.get(key, key),
                "missedRate": suggestion.evidence.get("missedRate"),
                "partialAnswerExcerpts": quotes,
            },
            ensure_ascii=False,
        ),
        max_output_tokens=600,
        temperature=0.2,
        purpose="question_bank.insights.rubric_gap",
    )
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(match.group(0) if match else raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not str(data.get("probe") or "").strip():
        return None
    proposal = {
        k: str(data.get(k) or "").strip()[:500]
        for k in ("misconception", "probe", "anchor_partial", "anchor_full")
    }
    if copies_candidate(" ".join(proposal.values()), quotes):
        return None
    proposal["basedOnAnswers"] = len(quotes)
    return proposal


# --------------------------------------------------------------------------- run


async def run_insights(db: AsyncSession, *, with_ai: bool = True) -> dict[str, Any]:
    """Statistics, suggestions and (optionally) AI proposals; returns what was found.

    Persisting is the question bank's job (question_bank/insights.py), called by the caller.
    """
    stats = await load_stats(db)
    suggestions = analyze(stats)
    ai_done = ai_failed = 0
    if with_ai:
        gaps = sorted(
            (s for s in suggestions if s.kind == "RUBRIC_GAP"),
            key=lambda s: -(s.evidence.get("missedRate") or 0),
        )[:MAX_AI_PROPOSALS]
        for suggestion in gaps:
            try:
                proposal = await propose_for_gap(stats[suggestion.question_id], suggestion)
            except Exception as exc:  # noqa: BLE001 - one failed call must not stop the run
                logger.warning("Rubric-gap proposal failed: %s", exc)
                proposal = None
            if proposal:
                suggestion.proposal.update(proposal)
                ai_done += 1
            else:
                ai_failed += 1
    return {
        "stats": {qid: summarize(s) for qid, s in stats.items()},
        "versions": {qid: s.version_id for qid, s in stats.items()},
        "suggestions": suggestions,
        "aiProposals": ai_done,
        "aiFailed": ai_failed,
    }
