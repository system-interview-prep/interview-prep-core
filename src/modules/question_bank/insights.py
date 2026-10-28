# ruff: noqa: E501
"""Suggestions from real interviews: stored, listed, accepted or dismissed.

The analysis lives in the interviews module (``interviews/evaluation/bank_insights.py``,
reached through its facade) because it reads turns and grades. This module
owns what the bank does with it:

* ``start_run`` runs the analysis in the background (it makes up to five LLM
  calls) and ``save_run`` stores the result: item statistics as an
  ``auto-YYYYMMDD`` row of ``question_calibrations``, findings as
  ``question_bank_suggestions`` (one OPEN row per finding; a finding that no
  longer holds is closed as ``auto_resolved``; one a person dismissed stays
  quiet for 30 days).
* ``decide`` applies a person's decision:

  | kind                          | ACCEPT does                                         | role            |
  |-------------------------------|-----------------------------------------------------|-----------------|
  | REVISE, DIFFICULTY, RUBRIC_GAP| new revision draft by the person, change summary = the finding (DIFFICULTY also sets the band) | author |
  | VARIANT                       | 2 gated AI drafts for the same skill (IN_REVIEW)    | bank admin      |
  | RETIRE, DUPLICATE             | retire the question (``retired_at``)                | bank admin      |

  Dismissing needs a reviewer or bank admin. Every new version still goes
  through review and approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.roles import ADMIN, QUESTION_AUTHOR
from src.core.trace_logging import trace_event
from src.infrastructure.database import SessionFactory
from src.modules.question_bank.permissions import REVISABLE_STATUSES
from src.modules.question_bank.service import QuestionBankService

logger = logging.getLogger(__name__)

DISMISS_QUIET_DAYS = 30
AUTHOR_KINDS = frozenset({"REVISE", "DIFFICULTY", "RUBRIC_GAP"})
ADMIN_KINDS = frozenset({"VARIANT", "RETIRE", "DUPLICATE"})

_run: dict[str, Any] = {"running": False}
_task: asyncio.Task[None] | None = None


def _held(actor: dict[str, Any]) -> set[str]:
    return {str(role).upper() for role in actor.get("roles") or []}


def can_decide(kind: str, decision: str, roles: set[str]) -> bool:
    # ADMIN decides everything (reviewer and bank-manager duties since 28/10);
    # an author may only accept the kinds that become a revision for them.
    if ADMIN in roles:
        return True
    return decision == "ACCEPT" and kind in AUTHOR_KINDS and QUESTION_AUTHOR in roles


# --------------------------------------------------------------------------- store a run


def calibration_row(version_id: str, summary: dict[str, Any], version: str) -> dict[str, Any] | None:
    """A question_calibrations row (rates 0-1, scores on 0-1); None without a graded answer."""
    if not summary.get("graded"):
        return None
    mean = summary.get("meanScore")
    spread = summary.get("scoreStdDev")
    return {
        "id": uuid4(),
        "version_id": UUID(version_id),
        "calibration_version": version,
        "sample_size": int(summary["graded"]),
        "mean_score": round(float(mean) / 10, 5) if mean is not None else 0,
        "score_stddev": round(float(spread) / 10, 5) if spread is not None else 0,
        "mean_answer_seconds": 0,
        "timeout_rate": summary.get("overtimeRate") or 0,
        "skip_rate": summary.get("skipRate") or 0,
        "clarification_rate": 0,
        "empirical_difficulty": summary.get("empiricalDifficulty"),
        "discrimination": summary.get("discrimination"),
        "slice_metrics": {
            "asked": summary.get("asked"),
            "sessions": summary.get("sessions"),
            "lowData": summary.get("lowData"),
            "method": "classical item analysis on rubric scores (bank_insights)",
        },
    }


async def save_run(db: AsyncSession, result: dict[str, Any], *, run_id: str) -> dict[str, int]:
    version = f"auto-{datetime.now(UTC):%Y%m%d}"
    calibrated = 0
    for qid, summary in result["stats"].items():
        row = calibration_row(result["versions"][qid], summary, version)
        if row is None:
            continue
        await db.execute(
            text(
                "INSERT INTO question_calibrations (id, question_version_id, calibration_version, sample_size, "
                "mean_score, score_stddev, mean_answer_seconds, timeout_rate, skip_rate, clarification_rate, "
                "empirical_difficulty, discrimination, slice_metrics) VALUES (:id, :version_id, "
                ":calibration_version, :sample_size, :mean_score, :score_stddev, :mean_answer_seconds, "
                ":timeout_rate, :skip_rate, :clarification_rate, :empirical_difficulty, :discrimination, "
                "CAST(:slice_metrics AS jsonb)) ON CONFLICT (question_version_id, calibration_version) DO UPDATE SET "
                "sample_size = EXCLUDED.sample_size, mean_score = EXCLUDED.mean_score, "
                "score_stddev = EXCLUDED.score_stddev, timeout_rate = EXCLUDED.timeout_rate, "
                "skip_rate = EXCLUDED.skip_rate, empirical_difficulty = EXCLUDED.empirical_difficulty, "
                "discrimination = EXCLUDED.discrimination, slice_metrics = EXCLUDED.slice_metrics, "
                "calculated_at = now()"
            ),
            {**row, "slice_metrics": json.dumps(row["slice_metrics"])},
        )
        calibrated += 1

    created = refreshed = quiet = 0
    keys = []
    for suggestion in result["suggestions"]:
        key = suggestion.dedupe_key[:255]
        keys.append(key)
        dismissed = await db.scalar(
            text(
                "SELECT 1 FROM question_bank_suggestions WHERE dedupe_key = :key AND status = 'DISMISSED' "
                "AND COALESCE(note, '') <> 'auto_resolved' "
                "AND decided_at > now() - make_interval(days => :days) LIMIT 1"
            ),
            {"key": key, "days": DISMISS_QUIET_DAYS},
        )
        if dismissed:
            quiet += 1
            continue
        params = {
            "id": uuid4(),
            "kind": suggestion.kind,
            "reason": suggestion.reason,
            "question_id": UUID(suggestion.question_id) if suggestion.question_id else None,
            "version_id": UUID(suggestion.version_id) if suggestion.version_id else None,
            "criterion_key": suggestion.criterion_key,
            "key": key,
            "severity": suggestion.severity,
            "evidence": json.dumps(suggestion.evidence, ensure_ascii=False, default=str),
            "proposal": json.dumps(suggestion.proposal, ensure_ascii=False, default=str),
            "run_id": run_id,
        }
        existing = await db.scalar(
            text("SELECT id FROM question_bank_suggestions WHERE dedupe_key = :key AND status = 'OPEN'"),
            {"key": key},
        )
        if existing:
            # Keep an AI proposal from an earlier run when this run made none.
            await db.execute(
                text(
                    "UPDATE question_bank_suggestions SET evidence = CAST(:evidence AS jsonb), "
                    "proposal = CASE WHEN CAST(:proposal AS jsonb) = '{}'::jsonb THEN proposal "
                    "ELSE proposal || CAST(:proposal AS jsonb) END, severity = :severity, "
                    "question_version_id = :version_id, run_id = :run_id, updated_at = now() WHERE id = :existing"
                ),
                {**params, "existing": existing},
            )
            refreshed += 1
        else:
            await db.execute(
                text(
                    "INSERT INTO question_bank_suggestions (id, kind, reason, question_id, question_version_id, "
                    "criterion_key, dedupe_key, severity, evidence, proposal, run_id) VALUES (:id, :kind, :reason, "
                    ":question_id, :version_id, :criterion_key, :key, :severity, CAST(:evidence AS jsonb), "
                    "CAST(:proposal AS jsonb), :run_id)"
                ),
                params,
            )
            created += 1
    # Findings that no longer hold (the question improved or was retired) close themselves.
    closed = await db.execute(
        text(
            "UPDATE question_bank_suggestions SET status = 'DISMISSED', note = 'auto_resolved', "
            "decided_at = now(), updated_at = now() "
            "WHERE status = 'OPEN' AND NOT (dedupe_key = ANY(:keys))"
        ),
        {"keys": keys or [""]},
    )
    await db.commit()
    return {
        "calibrated": calibrated,
        "created": created,
        "refreshed": refreshed,
        "quiet": quiet,
        "autoResolved": closed.rowcount or 0,
    }


async def _run_job(run_id: str, with_ai: bool) -> None:
    from src.modules.interviews.facade import run_question_bank_insights

    try:
        async with SessionFactory() as db:
            result = await run_question_bank_insights(db, with_ai=with_ai)
            saved = await save_run(db, result, run_id=run_id)
        _run.update(
            {
                **saved,
                "questions": len(result["stats"]),
                "suggestions": len(result["suggestions"]),
                "aiProposals": result["aiProposals"],
                "aiFailed": result["aiFailed"],
                "error": None,
            }
        )
        trace_event("question_bank", "bank_insights_completed", run_id=run_id, **saved)
    except Exception as exc:  # noqa: BLE001 - reported on the page
        logger.exception("Bank insights run failed")
        _run["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        _run["running"] = False
        _run["finishedAt"] = datetime.now(UTC).isoformat()


def run_state() -> dict[str, Any]:
    return dict(_run)


def start_run(*, with_ai: bool) -> dict[str, Any]:
    global _task
    if _run.get("running"):
        raise HTTPException(409, "insights_running")
    run_id = str(uuid4())
    _run.clear()
    _run.update(
        {"running": True, "runId": run_id, "withAi": with_ai, "startedAt": datetime.now(UTC).isoformat()}
    )
    _task = asyncio.create_task(_run_job(run_id, with_ai), name=f"bank-insights-{run_id}")
    return run_state()


# --------------------------------------------------------------------------- list and decide


def suggestion_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "reason": row["reason"],
        "status": row["status"],
        "severity": row["severity"],
        "questionId": str(row["question_id"]) if row.get("question_id") else None,
        "questionVersionId": str(row["question_version_id"]) if row.get("question_version_id") else None,
        "stableKey": row.get("stable_key"),
        "questionText": row.get("canonical_text"),
        "difficultyBand": row.get("difficulty_band"),
        "criterionKey": row.get("criterion_key"),
        "evidence": row.get("evidence") or {},
        "proposal": row.get("proposal") or {},
        "result": row.get("result") or {},
        "note": row.get("note"),
        "decidedAt": row["decided_at"].isoformat() if row.get("decided_at") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


_LIST_SQL = """
SELECT s.*, q.stable_key, qv.canonical_text, qv.difficulty_band
FROM question_bank_suggestions s
LEFT JOIN interview_questions q ON q.id = s.question_id
LEFT JOIN interview_question_versions qv ON qv.id = s.question_version_id
WHERE {where}
ORDER BY CASE s.severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, s.updated_at DESC
LIMIT :limit
"""


async def list_suggestions(
    db: AsyncSession,
    *,
    status: str = "OPEN",
    question_id: UUID | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    filters, params = ["s.status = :status"], {"status": status, "limit": limit}
    if question_id:
        filters.append("s.question_id = :qid")
        params["qid"] = question_id
    if kind:
        filters.append("s.kind = :kind")
        params["kind"] = kind
    rows = await db.execute(text(_LIST_SQL.format(where=" AND ".join(filters))), params)
    return [suggestion_view(dict(r)) for r in rows.mappings().all()]


async def counts(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(
        text("SELECT kind, count(*) AS n FROM question_bank_suggestions WHERE status = 'OPEN' GROUP BY kind")
    )
    return {r["kind"]: int(r["n"]) for r in rows.mappings().all()}


_CHANGE_SUMMARY = {
    "skipped_often": "Nhiều ứng viên bỏ qua câu này: viết lại cho rõ hoặc giảm độ khó.",
    "over_time": "Nhiều câu trả lời quá giờ: thu hẹp phạm vi câu hỏi hoặc tăng thời gian.",
    "flat_scores": "Điểm gần như giống nhau ở mọi ứng viên: câu hỏi không phân loại được.",
    "weak_discrimination": "Điểm câu này ít liên quan tới điểm cả buổi: xem lại câu hỏi và barem.",
    "too_easy": "Gần như ai cũng đạt điểm cao: tăng độ khó.",
    "too_hard": "Phần lớn ứng viên đạt điểm thấp: giảm độ khó hoặc làm rõ câu hỏi.",
    "criterion_often_missed": "Một ý barem thường bị thiếu: thêm câu hỏi thêm hoặc mốc chấm cho ý này.",
}


async def _revision_from(db: AsyncSession, row: dict[str, Any], actor_id: str) -> dict[str, Any]:
    status = await db.scalar(
        text("SELECT status FROM interview_question_versions WHERE id = :id"),
        {"id": row["question_version_id"]},
    )
    if str(status or "").upper() not in REVISABLE_STATUSES:
        raise HTTPException(409, "not_revisable")
    service = QuestionBankService(db)
    version = await service.revise(row["question_version_id"], actor_id)
    proposal = row.get("proposal") or {}
    summary = _CHANGE_SUMMARY.get(row["reason"], row["reason"])
    if row["kind"] == "RUBRIC_GAP" and proposal.get("probe"):
        summary += f" Gợi ý hỏi thêm: {proposal['probe']}"
    version.change_summary = summary[:1000]
    if row["kind"] == "DIFFICULTY" and proposal.get("difficultyBand"):
        version.difficulty_band = proposal["difficultyBand"]
    await db.flush()
    return {"revisionVersionId": str(version.id)}


async def decide(
    db: AsyncSession, suggestion_id: UUID, actor: dict[str, Any], decision: str, note: str | None
) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text("SELECT * FROM question_bank_suggestions WHERE id = :id"), {"id": suggestion_id}
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "suggestion_not_found")
    row = dict(row)
    if row["status"] != "OPEN":
        raise HTTPException(409, "already_decided")
    if not can_decide(row["kind"], decision, _held(actor)):
        raise HTTPException(403, "not_your_role")
    result: dict[str, Any] = {}
    if decision == "ACCEPT":
        if row["kind"] in AUTHOR_KINDS:
            result = await _revision_from(db, row, str(actor["sub"]))
        elif row["kind"] in {"RETIRE", "DUPLICATE"}:
            await db.execute(
                text(
                    "UPDATE interview_questions SET retired_at = now() WHERE id = :id AND retired_at IS NULL"
                ),
                {"id": row["question_id"]},
            )
            result = {"retired": True}
        elif row["kind"] == "VARIANT":
            result = await _variants(db, row)
    await db.execute(
        text(
            "UPDATE question_bank_suggestions SET status = :status, decided_by = :by, decided_at = now(), "
            "updated_at = now(), note = :note, result = CAST(:result AS jsonb) WHERE id = :id"
        ),
        {
            "status": "ACCEPTED" if decision == "ACCEPT" else "DISMISSED",
            "by": str(actor["sub"])[:36],
            "note": (note or None),
            "result": json.dumps(result),
            "id": suggestion_id,
        },
    )
    await db.commit()
    return {"status": "ACCEPTED" if decision == "ACCEPT" else "DISMISSED", "result": result}


async def _variants(db: AsyncSession, row: dict[str, Any]) -> dict[str, Any]:
    from src.modules.interviews.facade import generate_skill_questions

    meta = (
        (
            await db.execute(
                text(
                    "SELECT qv.difficulty_band, qv.canonical_locale, "
                    "(SELECT concept_id FROM question_version_taxonomy_concepts m WHERE m.question_version_id = qv.id "
                    "AND m.purpose = 'TARGET_SKILL' LIMIT 1) AS skill "
                    "FROM interview_question_versions qv WHERE qv.id = :id"
                ),
                {"id": row["question_version_id"]},
            )
        )
        .mappings()
        .one_or_none()
    )
    if meta is None or not meta["skill"]:
        raise HTTPException(409, "no_skill_mapping")
    filed = await generate_skill_questions(
        db,
        concept_id=meta["skill"],
        job_role=None,
        difficulty=meta["difficulty_band"],
        locale=meta["canonical_locale"],
        count=2,
        change_summary=f"Biến thể thay cho câu bị dùng lặp ({row['reason']}), đề xuất từ dữ liệu phỏng vấn thật.",
    )
    if not filed:
        raise HTTPException(503, "generation_failed")
    return {"generated": filed}
