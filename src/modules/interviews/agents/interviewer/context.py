"""Build the Interviewer Agent's context for one turn, from already-read rows.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §10 gives ``agents/interviewer/`` ownership of
"Context, adaptive routing, Thinker, Luna, arbiter, actions, guards, pacing".
This module is the context half, lifted out of ``session/chat_runtime.py``
where it sat among session persistence and turn orchestration.

Everything here is pure: it maps a session row, its turns and the current turn
into the ``session_state`` dict the engine and the turn graph read. It performs
no I/O, which is what lets it live under ``agents/`` at all -- the caller in
``session/`` does the reading. ``build_session_state_from_db`` keeps its
historical name because the Voice Lab, the runtime and several tests call it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from src.modules.interviews.core.contracts.interview import InterviewStage
from src.modules.interviews.core.conversation.turn_engine import pacing_seconds
from src.modules.interviews.core.session_policy.blueprint import (
    InterviewBlueprint,
    blueprint_from_payload,
    default_blueprint,
)
from src.modules.interviews.core.session_policy.demo_mode import is_demo_duration

__all__ = [
    "build_session_state_from_db",
    "session_blueprint",
]


def _snapshot_concept_id(snapshot: dict[str, Any] | None) -> str | None:
    """Concept id của một lượt đã đóng băng, dù planner ghi ở khoá nào."""
    snap = snapshot or {}
    for key in ("taxonomyTarget", "target"):
        concept = (snap.get(key) or {}).get("conceptId")
        if concept:
            return str(concept)
    return None

def session_blueprint(turns: list[dict[str, Any]]) -> InterviewBlueprint:
    """The blueprint the selector froze on the session's opening turn."""
    for turn in turns:
        record = (turn.get("question_snapshot") or {}).get("blueprint")
        if isinstance(record, dict):
            return blueprint_from_payload(record.get("policy"))
    return blueprint_from_payload(None)

def _format_rubric_criteria(criteria_val: Any) -> str:
    """Chuẩn hóa tiêu chí chấm điểm từ rubric snapshot sang dạng text dễ đọc cho LLM."""
    if isinstance(criteria_val, list):
        parts = []
        for c in criteria_val:
            if isinstance(c, dict):
                name = c.get("name") or c.get("stableKey") or "Tiêu chí"
                desc = c.get("description") or ""
                parts.append(f"- {name}: {desc}")
            else:
                parts.append(f"- {c}")
        return "\n".join(parts)
    elif isinstance(criteria_val, dict):
        return json.dumps(criteria_val, ensure_ascii=False)
    return str(criteria_val or "")

def build_session_state_from_db(
    session_row: dict[str, Any],
    turns: list[dict[str, Any]],
    current_turn: dict[str, Any] | None,
    current_turn_probe_count: int | bool,
    *,
    session_probe_count: int = 0,
    cv_followup_used: int = 0,
    clarify_count: int = 0,
    elapsed_override_seconds: float | None = None,
) -> dict[str, Any]:
    """Trích xuất và ánh xạ dữ liệu session/turns sang session_state cho InterviewCoreEngine."""
    session_id = session_row["id"]
    snap = (current_turn.get("question_snapshot") or {}) if current_turn else {}
    current_stage = (snap.get("stage") if current_turn else None) or InterviewStage.DEEP_DIVE.value
    q_text = snap.get("questionText") or snap.get("question_text") or ""
    rubric_criteria = _format_rubric_criteria((snap.get("rubric") or {}).get("criteria", ""))

    asked_question_ids: list[str] = []
    answered_question_ids: list[str] = []
    for t in turns:
        if t.get("status") in {"ASKED", "ANSWERED", "COMPLETED"}:
            if t.get("question_version_id"):
                asked_question_ids.append(str(t["question_version_id"]))
            asked_question_ids.append(str(t["id"]))
        if t.get("status") in {"ANSWERED", "COMPLETED", "EVALUATED"}:
            if t.get("question_version_id"):
                answered_question_ids.append(str(t["question_version_id"]))
            answered_question_ids.append(str(t["id"]))
    if current_turn:
        if current_turn.get("question_version_id"):
            asked_question_ids.append(str(current_turn["question_version_id"]))
            answered_question_ids.append(str(current_turn["question_version_id"]))
        asked_question_ids.append(str(current_turn["id"]))
        answered_question_ids.append(str(current_turn["id"]))
    asked_question_ids = list(set(asked_question_ids))
    answered_question_ids = list(set(answered_question_ids))

    questions_pool: dict[str, list[dict[str, Any]]] = {}
    for t in turns:
        t_snap = t.get("question_snapshot") or {}
        t_stage = t_snap.get("stage") or InterviewStage.DEEP_DIVE.value
        if t_stage not in questions_pool:
            questions_pool[t_stage] = []
        prompt = t_snap.get("questionText") or t_snap.get("question_text") or ""
        q_id = str(t.get("question_version_id") or t["id"])
        comp = (
            (t_snap.get("taxonomyTarget") or {}).get("label")
            or (t_snap.get("target") or {}).get("conceptId")
            or "Chuyên môn"
        )
        questions_pool[t_stage].append({
            "question_id": q_id,
            "question_version_id": str(t.get("question_version_id")) if t.get("question_version_id") else None,
            "competency": comp,
            # Core gọi tên chủ đề cho ứng viên từ đây; ``competency`` là nhãn
            # nội bộ nên không nói ra được (core.dialogue.topics).
            "concept_id": _snapshot_concept_id(t_snap),
            "project_name": t_snap.get("projectName") or None,
            "stage": t_stage,
            "status": str(t.get("status") or "PLANNED"),
            "main_prompt": prompt,
            "spoken_prompt": t_snap.get("spokenText") or None,
            "rubric_criteria": _format_rubric_criteria((t_snap.get("rubric") or {}).get("criteria", "")),
        })

    metadata_json = session_row.get("metadata") or {}
    if isinstance(metadata_json, str):
        try:
            metadata_json = json.loads(metadata_json)
        except Exception:
            metadata_json = {}

    target_duration_minutes = session_row.get("duration_minutes") or 25
    session_seconds = int(target_duration_minutes) * 60
    # The stage policy frozen on the opening turn (core.session_policy.blueprint);
    # sessions planned before blueprints existed get the default.
    blueprint = session_blueprint(turns)
    blueprint_record = next(
        (
            (turn.get("question_snapshot") or {}).get("blueprint")
            for turn in turns
            if isinstance((turn.get("question_snapshot") or {}).get("blueprint"), dict)
        ),
        {},
    )
    contract_ref = {
        "blueprint_id": blueprint_record.get("blueprintId"),
        "version_id": blueprint_record.get("versionId"),
        "version": blueprint_record.get("version"),
        "name": blueprint_record.get("name") or blueprint_record.get("blueprintName"),
    }
    frozen_stage_set = {
        str((turn.get("question_snapshot") or {}).get("stage") or "")
        for turn in turns
        if str((turn.get("question_snapshot") or {}).get("stage") or "")
        not in {"", "WARM_UP", "CLOSING", "CLOSED"}
    }
    frozen_stages = [
        stage for stage in ("VALIDATE", "DEEP_DIVE", "CHALLENGE", "BEHAVIORAL")
        if stage in frozen_stage_set
    ]
    behavioral_reserve = blueprint.behavioral.reserve_seconds if blueprint.behavioral.enabled else 0
    closing_reserve_seconds = int(
        metadata_json.get("closing_reserve_seconds")
        or pacing_seconds(blueprint.closing.reserve_seconds, session_seconds)
    )
    behavioral_reserve_seconds = int(
        metadata_json.get("behavioral_reserve_seconds") or pacing_seconds(behavioral_reserve, session_seconds)
    )

    now = datetime.now(UTC)
    started_at_val = session_row.get("started_at")
    if isinstance(started_at_val, str):
        try:
            started_dt = datetime.fromisoformat(started_at_val)
        except Exception:
            started_dt = now
    elif isinstance(started_at_val, datetime):
        started_dt = started_at_val
    else:
        started_dt = now
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)

    target_sec = int(target_duration_minutes) * 60
    # `elapsed_override_seconds` is how much of the SESSION has elapsed, not how
    # long the current answer took. Those were conflated before, so any client
    # sending a per-utterance duration reset the session clock on every turn and
    # the interview never reached its closing reserve or hard timeout.
    if elapsed_override_seconds is not None and elapsed_override_seconds > 0:
        elapsed_sec = int(elapsed_override_seconds)
    else:
        elapsed_sec = max(0, int((now - started_dt).total_seconds()))
    remaining_sec = max(0, target_sec - elapsed_sec)

    working_memory = {
        "candidate_context": {"sessionId": session_id, "jobId": session_row.get("job_id")},
        "current_stage": current_stage,
        "current_competency": (
            (snap.get("taxonomyTarget") or {}).get("label")
            or (snap.get("target") or {}).get("conceptId")
            or "Chuyên môn"
        ),
        "current_question": q_text,
        "answered_questions": [t["id"] for t in turns if t.get("status") in {"ANSWERED", "COMPLETED", "EVALUATED"}],
        "evidence_collected": [t["answer_text"] for t in turns if t.get("answer_text")],
        "missing_evidence": "",
        "probe_count": session_probe_count,
        "cv_followup_used": bool(cv_followup_used > 0),
        "elapsed_time": elapsed_sec,
        "remaining_time": remaining_sec,
    }

    return {
        "session_id": session_id,
        "current_stage": current_stage,
        "consecutive_uncooperative": metadata_json.get(
            "consecutive_uncooperative", metadata_json.get("consecutive_fails", 0)
        ),
        "consecutive_fails": metadata_json.get(
            "consecutive_uncooperative", metadata_json.get("consecutive_fails", 0)
        ),
        # The core accepts a bounded count (older callers may still pass bool).
        "current_turn_in_question": int(current_turn_probe_count),
        "target_duration_minutes": target_duration_minutes,
        # Demo sessions are turn-driven (see core.session_policy.demo_mode).
        "is_demo": is_demo_duration(target_duration_minutes),
        # basic | standard | challenge, frozen on each technical turn from the
        # CV–job match (planning.entry_gate). Challenge also probes good answers.
        "practice_tier": snap.get("practiceTier") or "standard",
        "started_at": session_row.get("started_at"),
        # When the current question was put to the candidate: its time share
        # (turn_context.question_time_budget) runs from here.
        "question_started_at": current_turn.get("started_at") if current_turn else None,
        # The session clock, surfaced at the top level because that is where the
        # core engine reads it. Previously only `working_memory` carried it, so
        # the engine silently recomputed elapsed time from `turn_input`, which is
        # per-answer, not per-session.
        "elapsed_time": elapsed_sec,
        "remaining_time": remaining_sec,
        "locale": session_row.get("locale") or "vi-VN",
        "candidate_settings": session_row.get("candidate_settings") or {},
        "allow_early_exit": metadata_json.get("allow_early_exit", True),
        "asked_question_ids": asked_question_ids,
        "answered_question_ids": answered_question_ids,
        "session_probe_count": session_probe_count,
        "cv_followup_used": cv_followup_used,
        "clarify_count": clarify_count,
        "closing_reserve_seconds": closing_reserve_seconds,
        "behavioral_reserve_seconds": behavioral_reserve_seconds,
        "blueprint": blueprint.to_payload(),
        "interview_contract": {
            "reference": contract_ref,
            "objective": blueprint.contract.objective,
            "coverage_target": blueprint.contract.coverage_target,
            "planned_stages": frozen_stages,
            "snapshot_policy": blueprint.to_payload(),
        },
        "working_memory": working_memory,
        "current_question_context": {
            "question_id": str(current_turn.get("question_version_id") or current_turn["id"]) if current_turn else "",
            "main_prompt": q_text,
            "spoken_prompt": snap.get("spokenText") or None,
            "competency": working_memory["current_competency"],
            "concept_id": _snapshot_concept_id(snap),
            "project_name": snap.get("projectName") or None,
            "rubric_criteria": rubric_criteria,
            "hard_answer_seconds": (snap.get("hardAnswerSeconds") or 180),
        } if current_turn else {},
        "questions_pool": questions_pool,
    }
