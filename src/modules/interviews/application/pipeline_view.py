"""What the AI did for one interview, stage by stage, for the admin view.

Combines the durable state (session, plan, frozen turns, messages, grading
job, evaluations) with the persisted pipeline events and AI call logs
(src/core/pipeline_events.py) into one read model:

    created -> entry check -> plan -> questions -> interview -> grading -> report

Each stage carries a status (done | running | error | pending | skipped), its
time, and the data behind the decision. Reasons are returned as structured
fields (codes, thresholds, numbers) so the frontend can phrase them in either
language. ``assemble_pipeline`` is pure and unit tested; ``load_pipeline``
only runs the queries.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.evaluation.question_quality import session_plan_audit
from src.modules.interviews.planning.entry_gate import entry_policy

STAGE_KEYS = ("created", "entry", "plan", "questions", "interview", "evaluation", "report")
_EVENT_LIMIT = 3000
# Voice-room events shown on the question they happened in (voice_events.py).
_TURN_VOICE_EVENTS = frozenset(
    {
        "voice_not_heard",
        "voice_unclear_transcript",
        "voice_silence_reprompt",
        "voice_question_replayed",
        "voice_interruption",
        "voice_processing_filler",
        "voice_core_failed",
        "voice_candidate_left",
        "voice_candidate_returned",
        "voice_provider_degraded",
        "voice_tts_fallback",
    }
)
# A voice state older than this, with no newer voice event, is not trusted as
# "now" (the agent may have crashed); the view falls back to stored messages.
_VOICE_STATE_STALE_SECONDS = 180
_AI_CALL_LIMIT = 300


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value) if value else None


def _ms_between(start: Any, end: Any) -> int | None:
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, int((end - start).total_seconds() * 1000))


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return int(ordered[index])


def _last(events: list[dict[str, Any]], *names: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event["event"] in names:
            return event
    return None


def _stage(key: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "key": key,
        "status": status,
        "startedAt": extra.pop("started_at", None),
        "endedAt": extra.pop("ended_at", None),
        "durationMs": extra.pop("duration_ms", None),
        "note": extra.pop("note", None),
    }


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "at": _iso(event.get("created_at")),
        "stage": event["stage"],
        "event": event["event"],
        "status": event["status"],
        "turnIndex": event.get("turn_index"),
        "durationMs": event.get("duration_ms"),
        "payload": event.get("payload") or {},
    }


def summarize_ai_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [int(c["duration_ms"]) for c in calls if c.get("duration_ms") is not None]
    by_purpose: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        by_purpose[str(call.get("purpose") or "unknown")].append(call)
    purposes = []
    for purpose, rows in by_purpose.items():
        ms = [int(r["duration_ms"]) for r in rows if r.get("duration_ms") is not None]
        purposes.append(
            {
                "purpose": purpose,
                "count": len(rows),
                "errors": sum(1 for r in rows if r.get("status") == "error"),
                "avgMs": round(sum(ms) / len(ms)) if ms else None,
                "p95Ms": _percentile(ms, 0.95),
                "inputTokens": sum(int(r.get("input_tokens") or 0) for r in rows),
                "outputTokens": sum(int(r.get("output_tokens") or 0) for r in rows),
            }
        )
    purposes.sort(key=lambda p: (-p["count"], p["purpose"]))
    return {
        "total": len(calls),
        "errors": sum(1 for c in calls if c.get("status") == "error"),
        "totalMs": sum(durations),
        "p50Ms": _percentile(durations, 0.5),
        "p95Ms": _percentile(durations, 0.95),
        "inputTokens": sum(int(c.get("input_tokens") or 0) for c in calls),
        "outputTokens": sum(int(c.get("output_tokens") or 0) for c in calls),
        "byPurpose": purposes,
    }


def ai_call_view(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call.get("id"),
        "at": _iso(call.get("created_at")),
        "sessionId": call.get("session_id"),
        "purpose": call.get("purpose"),
        "provider": call.get("provider"),
        "model": call.get("model"),
        "status": call.get("status"),
        "durationMs": call.get("duration_ms"),
        "inputTokens": call.get("input_tokens"),
        "outputTokens": call.get("output_tokens"),
        "inputChars": call.get("input_chars"),
        "outputChars": call.get("output_chars"),
        "errorType": call.get("error_type"),
        "errorMessage": call.get("error_message"),
        "inputPreview": call.get("input_preview"),
        "outputPreview": call.get("output_preview"),
    }


def _entry_section(
    context: dict[str, Any], plan: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    policy = entry_policy()
    blocked = _last(events, "plan_blocked_by_match_threshold")
    return {
        "matchPercent": context.get("matchPercent"),
        "fitBand": context.get("fitBand"),
        "eligibility": context.get("eligibility"),
        "matchingDecision": context.get("matchingDecision"),
        "warnings": list(context.get("entryWarnings") or []),
        "practiceTier": context.get("practiceTier") or (plan.get("difficulty") or {}).get("practiceTier"),
        "blocked": bool(blocked),
        "blockedReason": (blocked or {}).get("payload", {}).get("reason") if blocked else None,
        "policy": {
            "minMatchPercent": policy.min_match_percent,
            "lowFitPercent": policy.low_fit_percent,
            "challengePercent": policy.challenge_percent,
            "blockNotEligible": policy.block_not_eligible,
        },
    }


def _questions_section(audit: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    plan = audit.get("plan") or {}
    spoken = _last(events, "spoken_questions_saved")
    rejected_ids = set((spoken or {}).get("payload", {}).get("rejected_turn_ids") or [])
    generated = [e for e in events if e["event"] == "question_generated"]
    selection = _last(events, "question_selection_completed")
    sources: dict[str, int] = defaultdict(int)
    for turn in audit.get("turns") or []:
        sources[str(turn.get("source") or "unlabelled")] += 1
    return {
        "turnsCount": len(audit.get("turns") or []),
        "sources": dict(sources),
        "fallbackCount": (selection or {}).get("payload", {}).get("fallback_count"),
        "generatedQuestionCount": plan.get("generatedQuestionCount"),
        "generation": [
            {
                "conceptId": e["payload"].get("concept_id"),
                "requested": e["payload"].get("requested"),
                "filed": e["payload"].get("filed"),
                "rejected": e["payload"].get("rejected"),
            }
            for e in generated
        ],
        "spoken": (
            {
                "saved": spoken["payload"].get("saved"),
                "rejected": spoken["payload"].get("rejected"),
                "total": spoken["payload"].get("total"),
                "rejectedTurnIds": sorted(rejected_ids),
            }
            if spoken
            else None
        ),
        "uncoveredTargets": plan.get("uncoveredTargets") or [],
    }


def _exchange_view(event: dict[str, Any]) -> dict[str, Any]:
    p = event.get("payload") or {}
    return {
        "at": _iso(event.get("created_at")),
        "action": p.get("action"),
        "stage": p.get("stage"),
        "intent": p.get("intent"),
        "sufficiency": p.get("sufficiency"),
        "missingAspect": p.get("missing_aspect"),
        "probeKind": p.get("probe_kind"),
        "preScore": p.get("pre_score"),
        "ackSource": p.get("ack_source"),
        "ackReason": p.get("ack_reason"),
        "generatedBy": p.get("generated_by"),
        "repeatsRecent": p.get("repeats_recent"),
        "hardAnswerTimeout": p.get("hard_answer_timeout"),
        "answerChars": p.get("answer_chars"),
        "interviewer": p.get("interviewer"),
        "coreMs": event.get("duration_ms"),
        "modality": p.get("modality"),
    }


def _voice_incident_view(event: dict[str, Any]) -> dict[str, Any]:
    p = event.get("payload") or {}
    keep = (
        "reason",
        "action",
        "after_s",
        "confidence",
        "words",
        "error_type",
        "component",
        "provider",
        "available",
    )
    return {
        "at": _iso(event.get("created_at")),
        "event": event["event"],
        "status": event["status"],
        **{key: p.get(key) for key in keep if p.get(key) is not None},
    }


def _turns_section(
    audit: dict[str, Any],
    events: list[dict[str, Any]],
    turn_evals: list[dict[str, Any]],
    turn_times: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    turn_times = turn_times or {}
    exchanges: dict[int, list[dict[str, Any]]] = defaultdict(list)
    incidents: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("turn_index") is None:
            continue
        if event["event"] == "turn_spoken":
            exchanges[int(event["turn_index"])].append(_exchange_view(event))
        elif event["event"] in _TURN_VOICE_EVENTS:
            incidents[int(event["turn_index"])].append(_voice_incident_view(event))
    grades = {str(row["turn_id"]): row for row in turn_evals}
    spoken = _last(events, "spoken_questions_saved")
    rejected_ids = set((spoken or {}).get("payload", {}).get("rejected_turn_ids") or [])
    views = []
    for turn in audit.get("turns") or []:
        grade = grades.get(str(turn.get("turnId")))
        index = int(turn.get("turnIndex") or 0)
        times = turn_times.get(str(turn.get("turnId"))) or {}
        views.append(
            {
                "turnId": turn.get("turnId"),
                "turnIndex": index,
                "status": turn.get("status"),
                "stage": turn.get("stage"),
                "source": turn.get("source"),
                "difficulty": turn.get("difficulty"),
                "target": turn.get("target"),
                "questionText": turn.get("questionText"),
                "spokenRejected": str(turn.get("turnId")) in rejected_ids,
                "answerSeconds": turn.get("answerSeconds"),
                "followups": turn.get("followups") or [],
                "askedAt": _iso(times.get("asked_at")),
                "lastAnswerAt": _iso(times.get("last_answer_at")),
                "answerCount": int(times.get("answers") or 0),
                "exchanges": exchanges.get(index, []),
                "voiceIncidents": incidents.get(index, [])[-30:],
                "grade": (
                    {
                        "score": float(grade["score"]) if grade.get("score") is not None else None,
                        "outcome": grade.get("outcome"),
                        "rubricPoints": grade.get("rubric_points") or [],
                        "feedback": grade.get("feedback"),
                    }
                    if grade
                    else None
                ),
            }
        )
    return views


def _evaluation_section(
    events: list[dict[str, Any]],
    job: dict[str, Any] | None,
    evaluation: dict[str, Any] | None,
) -> dict[str, Any]:
    claimed = _last(events, "evaluation_job_claimed")
    graded_since = []
    if claimed is not None:
        seen = False
        for event in events:
            if event is claimed:
                seen = True
                continue
            if seen and event["event"] == "turn_graded":
                graded_since.append(event)
    total = (claimed or {}).get("payload", {}).get("turns_total")
    partial = _last(events, "evaluation_partial_coverage")
    capped = _last(events, "evaluation_decision_capped")
    return {
        "job": (
            {
                "status": job.get("status"),
                "attemptCount": job.get("attempt_count"),
                "lastError": job.get("last_error"),
                "leaseUntil": _iso(job.get("lease_until")),
                "updatedAt": _iso(job.get("updated_at")),
            }
            if job
            else None
        ),
        "progress": {"graded": len(graded_since), "total": total} if claimed else None,
        "gradedTurns": [
            {
                "turnIndex": e.get("turn_index"),
                "score": e["payload"].get("score"),
                "outcome": e["payload"].get("outcome"),
                "graded": e["payload"].get("graded"),
                "method": e["payload"].get("method"),
                "at": _iso(e.get("created_at")),
            }
            for e in graded_since
        ],
        "coverage": (
            {
                "ratio": partial["payload"].get("coverage_ratio"),
                "uncovered": partial["payload"].get("uncovered") or [],
            }
            if partial
            else None
        ),
        "decisionCapped": (
            {
                "originalDecision": capped["payload"].get("original_decision"),
                "coverageRatio": capped["payload"].get("coverage_ratio"),
            }
            if capped
            else None
        ),
        "overallScore": float(evaluation["overall_score"]) if evaluation else None,
        "decision": evaluation.get("decision_recommendation") if evaluation else None,
        "evaluatedAt": _iso(evaluation.get("evaluated_at")) if evaluation else None,
    }


def _stages(
    session: dict[str, Any],
    plan: dict[str, Any],
    context: dict[str, Any],
    turns: list[dict[str, Any]],
    events: list[dict[str, Any]],
    job: dict[str, Any] | None,
    evaluation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    stages = []
    created = _last(events, "session_created")
    stages.append(
        _stage(
            "created",
            "done",
            started_at=_iso((created or {}).get("created_at")),
            duration_ms=(created or {}).get("duration_ms"),
        )
    )

    blocked = _last(events, "plan_blocked_by_match_threshold")
    if blocked:
        stages.append(_stage("entry", "error", started_at=_iso(blocked.get("created_at")), note="blocked"))
    elif context.get("matchPercent") is not None or context.get("matchingDecision"):
        stages.append(_stage("entry", "done", note=context.get("practiceTier")))
    else:
        stages.append(_stage("entry", "pending"))

    plan_started = _last(events, "plan_started")
    plan_done = _last(events, "plan_completed")
    plan_failed = _last(events, "plan_failed")
    plan_status = str(plan.get("status") or "").upper()
    if plan_status in {"READY", "LOCKED"}:
        status = "done"
    elif plan_failed and (not plan_started or plan_failed["created_at"] >= plan_started["created_at"]):
        status = "error"
    elif plan_started:
        status = "running"
    else:
        status = "pending"
    stages.append(
        _stage(
            "plan",
            status,
            started_at=_iso((plan_started or {}).get("created_at")),
            ended_at=_iso((plan_done or plan_failed or {}).get("created_at")),
            duration_ms=(plan_done or plan_failed or {}).get("duration_ms"),
            note=(plan_failed or {}).get("payload", {}).get("error_type") if status == "error" else None,
        )
    )

    sel_started = _last(events, "question_selection_started")
    sel_done = _last(events, "question_selection_completed")
    sel_failed = _last(events, "question_selection_failed")
    if turns and plan_status == "LOCKED":
        status = "done"
    elif sel_failed and (not sel_started or sel_failed["created_at"] >= sel_started["created_at"]):
        status = "error"
    elif sel_started:
        status = "running"
    else:
        status = "pending"
    stages.append(
        _stage(
            "questions",
            status,
            started_at=_iso((sel_started or {}).get("created_at")),
            ended_at=_iso((sel_done or sel_failed or {}).get("created_at")),
            duration_ms=(sel_done or sel_failed or {}).get("duration_ms"),
        )
    )

    session_status = str(session.get("status") or "").upper()
    started_at = session.get("startedAt")
    ended_at = session.get("endedAt")
    answered = sum(1 for t in turns if str(t.get("status") or "").upper() == "ANSWERED")
    asked = any(str(t.get("status") or "").upper() == "ASKED" for t in turns)
    first_turn = next((e for e in events if e["event"] == "turn_spoken"), None)
    chat_started = _last(events, "chat_start_completed", "text_turn_asked")
    began = started_at if (first_turn or answered or asked or chat_started) else None
    if session_status == "CLOSED":
        end_reason = str(session.get("endReason") or "")
        status = "error" if end_reason in {"TECHNICAL_FAILURE", "FAST_FAIL_TECH"} else "done"
        if not answered and not first_turn:
            status = "skipped" if status == "done" else status
    elif began:
        status = "running"
    else:
        status = "pending"
    stages.append(
        _stage(
            "interview",
            status,
            started_at=began,
            ended_at=ended_at if session_status == "CLOSED" else None,
            duration_ms=_ms_between(began, ended_at) if began and ended_at else None,
            note=f"{answered}/{len(turns)}",
        )
    )

    eval_done = _last(events, "evaluation_pipeline_completed")
    eval_started = _last(events, "evaluation_pipeline_started")
    job_status = str((job or {}).get("status") or "").upper()
    if evaluation is not None and job_status != "PROCESSING":
        status = "done"
    elif job_status == "PROCESSING":
        status = "running"
    elif job_status == "FAILED":
        status = "error"
    elif session_status == "CLOSED" and not answered:
        status = "skipped"
    else:
        status = "pending"
    stages.append(
        _stage(
            "evaluation",
            status,
            started_at=_iso((eval_started or {}).get("created_at")) or _iso((job or {}).get("created_at")),
            ended_at=_iso((eval_done or {}).get("created_at")),
            duration_ms=(eval_done or {}).get("duration_ms"),
            note=(job or {}).get("last_error") if status == "error" else None,
        )
    )
    if evaluation is not None:
        report_status = "done"
    else:
        report_status = "skipped" if stages[-1]["status"] == "skipped" else "pending"
    stages.append(
        _stage(
            "report",
            report_status,
            ended_at=_iso((evaluation or {}).get("evaluated_at")),
        )
    )
    return stages


def live_state(
    *,
    session: dict[str, Any],
    stages: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    events: list[dict[str, Any]],
    job: dict[str, Any] | None,
    last_message: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """What is happening right now, and since when.

    ``{state, since, source, kind?}``. Voice and video sessions report their
    state from the room (voice_state events); chat sessions, and voice rooms
    whose agent went quiet, are read from the stored messages. ``None`` when
    nothing is running.
    """
    now = now or datetime.now(UTC)
    by_key = {stage["key"]: stage for stage in stages}
    if (by_key.get("evaluation") or {}).get("status") == "running":
        job_since = (job or {}).get("updated_at") or (job or {}).get("created_at")
        return {"state": "grading", "since": _iso(job_since), "source": "job"}
    for key in ("plan", "questions"):
        if (by_key.get(key) or {}).get("status") == "running":
            return {
                "state": "preparing",
                "since": by_key[key].get("startedAt"),
                "source": "events",
                "step": key,
            }
    if str(session.get("status") or "").upper() == "CLOSED":
        return None
    interview = by_key.get("interview") or {}
    if interview.get("status") != "running":
        ready = all((by_key.get(k) or {}).get("status") == "done" for k in ("plan", "questions"))
        return {"state": "not_started", "since": None, "source": "derived"} if ready else None

    voice_state = _last(events, "voice_state")
    last_voice = next((e for e in reversed(events) if e["event"].startswith("voice_")), None)
    if voice_state is not None and last_voice is not None:
        last_at = _as_dt(last_voice.get("created_at"))
        if (
            last_at
            and (now - last_at).total_seconds() <= _VOICE_STATE_STALE_SECONDS
            or (voice_state["payload"].get("state") in {"waiting_answer", "candidate_away"})
        ):
            payload = voice_state.get("payload") or {}
            return {
                "state": payload.get("state"),
                "kind": payload.get("kind"),
                "since": _iso(voice_state.get("created_at")),
                "source": "voice",
            }
    if last_message:
        role = str(last_message.get("role") or "")
        since = _iso(last_message.get("created_at"))
        if role == "user":
            return {"state": "processing", "since": since, "source": "messages"}
        return {"state": "waiting_answer", "since": since, "source": "messages"}
    asked = next((t for t in turns if str(t.get("status") or "").upper() == "ASKED"), None)
    return {"state": "waiting_answer" if asked else "not_started", "since": None, "source": "derived"}


def timing(session: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Elapsed and remaining interview time against the session's budget."""
    now = now or datetime.now(UTC)
    started = _as_dt(session.get("startedAt"))
    ended = _as_dt(session.get("endedAt"))
    budget = session.get("durationMinutes")
    elapsed = int(((ended or now) - started).total_seconds() * 1000) if started else None
    remaining = (
        int(budget) * 60_000 - elapsed if budget is not None and elapsed is not None and not ended else None
    )
    return {
        "startedAt": _iso(started),
        "endedAt": _iso(ended),
        "elapsedMs": max(0, elapsed) if elapsed is not None else None,
        "budgetMinutes": budget,
        "remainingMs": remaining,
    }


def voice_summary(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Voice-room health for one session: synthesis delay, Core delay, incidents."""
    voice = [e for e in events if e["event"].startswith("voice_")]
    if not voice:
        return None
    ttfb = [
        int(e["payload"]["ttfb_ms"])
        for e in voice
        if e["event"] == "voice_tts_metrics" and isinstance(e["payload"].get("ttfb_ms"), (int, float))
    ]
    core = [
        int(e["payload"]["core_ms"])
        for e in voice
        if e["event"] == "voice_core_completed" and isinstance(e["payload"].get("core_ms"), (int, float))
    ]
    counts: dict[str, int] = defaultdict(int)
    for event in voice:
        counts[event["event"]] += 1
    started = _last(voice, "voice_session_started")
    return {
        "ttsTtfbP50Ms": _percentile(ttfb, 0.5),
        "ttsTtfbP95Ms": _percentile(ttfb, 0.95),
        "coreP50Ms": _percentile(core, 0.5),
        "coreP95Ms": _percentile(core, 0.95),
        "answers": counts["voice_answer_submitted"],
        "notHeard": counts["voice_not_heard"],
        "unclear": counts["voice_unclear_transcript"],
        "replayed": counts["voice_question_replayed"],
        "reprompts": counts["voice_silence_reprompt"],
        "interruptions": counts["voice_interruption"],
        "coreFailures": counts["voice_core_failed"],
        "providerDegraded": counts["voice_provider_degraded"],
        "ttsFallback": counts["voice_tts_fallback"],
        "candidateLeft": counts["voice_candidate_left"],
        "sttModel": ((started or {}).get("payload") or {}).get("stt_model"),
        "ttsProvider": ((started or {}).get("payload") or {}).get("tts_provider"),
    }


def assemble_pipeline(
    *,
    audit: dict[str, Any],
    context: dict[str, Any],
    events: list[dict[str, Any]],
    job: dict[str, Any] | None,
    evaluation: dict[str, Any] | None,
    turn_evals: list[dict[str, Any]],
    ai_calls: list[dict[str, Any]],
    turn_times: dict[str, dict[str, Any]] | None = None,
    last_message: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    session = audit.get("session") or {}
    plan = audit.get("plan") or {}
    turn_rows = audit.get("turns") or []
    stages = _stages(session, plan, context, turn_rows, events, job, evaluation)
    running = any(s["status"] == "running" for s in stages)
    asked = next((t for t in turn_rows if str(t.get("status") or "").upper() == "ASKED"), None)
    interview_warnings = [
        _event_view(e)
        for e in events
        if e["stage"] in {"interview", "questions", "plan"} and e["status"] in {"warning", "error"}
    ]
    warmup = sum(1 for t in turn_rows if str(t.get("stage") or "").upper() == "WARM_UP")
    return {
        "session": session,
        "live": running,
        "liveState": live_state(
            session=session,
            stages=stages,
            turns=turn_rows,
            events=events,
            job=job,
            last_message=last_message,
            now=now,
        ),
        # No message yet: the opening question has not been asked, so the clock
        # has not started (started_at is still the row's creation time).
        "timing": timing(session if last_message else {**session, "startedAt": None}, now=now),
        "currentTurnIndex": asked.get("turnIndex") if asked else None,
        "turnCounts": {"total": len(turn_rows), "warmup": warmup, "main": len(turn_rows) - warmup},
        "voice": voice_summary(events),
        "stages": stages,
        "entry": _entry_section(context, plan, events),
        "plan": {
            **plan,
            "practiceTier": context.get("practiceTier"),
            "questionBudget": plan.get("questionBudget") or context.get("questionBudget"),
        },
        "questions": _questions_section(audit, events),
        "turns": _turns_section(audit, events, turn_evals, turn_times),
        "warnings": interview_warnings[-100:],
        "evaluation": _evaluation_section(events, job, evaluation),
        "ai": {**summarize_ai_calls(ai_calls), "recent": [ai_call_view(c) for c in ai_calls[-100:]][::-1]},
        "events": [_event_view(e) for e in events[-500:]],
        "eventsTruncated": len(events) > 500,
        "generatedAt": datetime.now(UTC).isoformat(),
    }


async def _rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]
    except Exception:  # noqa: BLE001 - tables missing before migration 0029
        await db.rollback()
        return []


async def load_pipeline(db: AsyncSession, session_id: str) -> dict[str, Any] | None:
    audit = await session_plan_audit(db, session_id)
    if audit is None:
        return None
    params = {"sid": session_id}
    context_rows = await _rows(
        db, "SELECT source_context FROM interview_session_plans WHERE session_id = :sid", params
    )
    context = (context_rows[0]["source_context"] if context_rows else None) or {}
    jobs = await _rows(
        db,
        "SELECT status, attempt_count, last_error, lease_until, created_at, updated_at "
        "FROM interview_evaluation_jobs WHERE session_id = :sid",
        params,
    )
    evaluations = await _rows(
        db,
        "SELECT id, overall_score, decision_recommendation, evaluated_at FROM interview_evaluations "
        "WHERE session_id = :sid ORDER BY evaluated_at DESC LIMIT 1",
        params,
    )
    evaluation = evaluations[0] if evaluations else None
    turn_evals = (
        await _rows(
            db,
            "SELECT turn_id, score, outcome, rubric_points, feedback FROM interview_turn_evaluations "
            "WHERE evaluation_id = :eid",
            {"eid": evaluation["id"]},
        )
        if evaluation
        else []
    )
    events = await _rows(
        db,
        f"""
        SELECT * FROM (
            SELECT id, stage, event, status, turn_index, duration_ms, payload, created_at
            FROM interview_pipeline_events WHERE session_id = :sid
            ORDER BY created_at DESC, id DESC LIMIT {_EVENT_LIMIT}
        ) recent ORDER BY created_at, id
        """,
        params,
    )
    ai_calls = await _rows(
        db,
        f"""
        SELECT * FROM (
            SELECT * FROM ai_call_logs WHERE session_id = :sid
            ORDER BY created_at DESC, id DESC LIMIT {_AI_CALL_LIMIT}
        ) recent ORDER BY created_at, id
        """,
        params,
    )
    turn_times = {
        str(row["turn_id"]): row
        for row in await _rows(
            db,
            """
            SELECT turn_id,
                   MIN(created_at) FILTER (WHERE role = 'assistant') AS asked_at,
                   MAX(created_at) FILTER (WHERE role = 'user') AS last_answer_at,
                   COUNT(*) FILTER (WHERE role = 'user') AS answers
            FROM interview_chat_messages
            WHERE session_id = :sid AND turn_id IS NOT NULL
            GROUP BY turn_id
            """,
            params,
        )
    }
    last_messages = await _rows(
        db,
        "SELECT role, message_type, created_at FROM interview_chat_messages "
        "WHERE session_id = :sid ORDER BY sequence DESC LIMIT 1",
        params,
    )
    return assemble_pipeline(
        audit=audit,
        context=context,
        events=events,
        job=jobs[0] if jobs else None,
        evaluation=evaluation,
        turn_evals=turn_evals,
        ai_calls=ai_calls,
        turn_times=turn_times,
        last_message=last_messages[0] if last_messages else None,
    )
