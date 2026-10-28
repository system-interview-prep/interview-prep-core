"""CV-JD matching runs for the admin view: list and step-by-step detail.

Reads ``matching_pipeline_events`` (written while a match run is active, see
src/modules/matching/application/run_tracking.py) and the model calls stamped
with the run (``ai_call_logs.match_run_id``):

    request -> inputs -> cache -> reparse -> requirements -> semantic
            -> scoring -> cache_write -> done  (+ clarify, questions, rescore)

``summarize_run``, ``run_view``, ``build_steps`` and ``assemble_run_detail``
are pure and unit tested; ``list_runs`` and ``load_run_detail`` only run the
queries. Event payload fields are returned as stored (snake_case), the read
model around them is camelCase.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.facade import ai_call_view, summarize_ai_calls

BASE_STEPS = (
    "request",
    "inputs",
    "cache",
    "reparse",
    "requirements",
    "semantic",
    "scoring",
    "cache_write",
    "done",
)
CLARIFICATION_STEPS = ("clarify", "questions", "rescore")
_FLOW_CLARIFICATION_STEPS = {
    "clarify_full": ("clarify", "questions"),
    "clarify_prepare": ("clarify",),
    "clarify_questions": ("questions",),
    "rescore": ("rescore",),
}
# Steps a cache hit never runs.
_CACHED_STEPS = frozenset({"reparse", "requirements", "semantic", "scoring", "cache_write"})
# Flows that go through the result cache.
_CACHED_FLOWS = frozenset({"match", "planner"})
STALLED_AFTER = timedelta(minutes=10)
_EVENT_LIMIT = 500
_AI_CALL_LIMIT = 300
# Large payload fields kept out of step summaries (shown in their own sections).
_SUMMARY_EXCLUDED = frozenset({"items", "factors", "scoring", "duration_ms"})
NOT_MIGRATED = "Matching pipeline events are not migrated (0032)"


# --------------------------------------------------------------------------- helpers


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: Any) -> str | None:
    dt = _as_dt(value)
    return dt.isoformat() if dt else None


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ms_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _latest(events: list[dict[str, Any]], stage: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event["stage"] == stage:
            return event
    return None


def _normalize(event: dict[str, Any]) -> dict[str, Any]:
    return {**event, "payload": _payload(event.get("payload")), "created_at": _as_dt(event.get("created_at"))}


# --------------------------------------------------------------------------- run summary


def summarize_run(run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """The aggregate the list query computes in SQL, from one run's events (oldest first)."""
    events = [_normalize(e) for e in events]
    done = _latest(events, "done")
    scored = next((e for e in reversed(events) if e["event"] == "match_scored"), None)

    def first(key: str) -> Any:
        return next((e.get(key) for e in events if e.get(key)), None)

    return {
        "run_id": run_id,
        "flow": events[-1].get("flow") if events else None,
        "started_at": events[0]["created_at"] if events else None,
        "last_at": events[-1]["created_at"] if events else None,
        "current_stage": events[-1]["stage"] if events else None,
        "done_event": done["event"] if done else None,
        "done_at": done["created_at"] if done else None,
        "done_payload": done["payload"] if done else None,
        "scoring_warnings": (scored or {}).get("payload", {}).get("warnings"),
        "problem_events": [e["event"] for e in events if e.get("status") in {"warning", "error"}],
        "user_id": first("user_id"),
        "resume_id": first("resume_id"),
        "job_id": first("job_id"),
    }


def run_status(summary: dict[str, Any], now: datetime) -> tuple[str, bool]:
    """(running | completed | failed, stalled)."""
    if summary.get("done_event") == "match_completed":
        return "completed", False
    if summary.get("done_event") == "match_failed":
        return "failed", False
    last = _as_dt(summary.get("last_at"))
    if last is not None and now - last > STALLED_AFTER:
        return "failed", True
    return "running", False


def _warning_codes(summary: dict[str, Any], stalled: bool) -> list[str]:
    codes: list[str] = []
    if stalled:
        codes.append("stalled")
    done = _payload(summary.get("done_payload"))
    scoring_warnings = summary.get("scoring_warnings")
    if isinstance(scoring_warnings, str):
        try:
            scoring_warnings = json.loads(scoring_warnings)
        except ValueError:
            scoring_warnings = None
    if not scoring_warnings and isinstance(done.get("scoring"), dict):
        scoring_warnings = done["scoring"].get("warnings")
    codes.extend(str(w) for w in scoring_warnings or [])
    problems = {
        "cache_lookup_completed": "cache_read_failed",
        "cache_write_completed": "cache_write_failed",
        "inputs_failed": "inputs_failed",
    }
    for event in summary.get("problem_events") or []:
        if event in problems:
            codes.append(problems[event])
    return list(dict.fromkeys(codes))


def run_view(
    summary: dict[str, Any],
    *,
    names: dict[str, dict[str, Any]] | None = None,
    ai: dict[str, int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One ``Run`` of the admin contract."""
    now = now or datetime.now(UTC)
    names = names or {}
    status, stalled = run_status(summary, now)
    started = _as_dt(summary.get("started_at"))
    done_at = _as_dt(summary.get("done_at"))
    done = _payload(summary.get("done_payload"))
    finished = done_at if status != "running" and not stalled else None
    if finished is not None:
        duration = done.get("duration_ms") if isinstance(done.get("duration_ms"), int) else None
        duration = duration if duration is not None else _ms_between(started, finished)
    elif stalled:
        duration = _ms_between(started, _as_dt(summary.get("last_at")))
    else:
        duration = None
    user = names.get("users", {}).get(summary.get("user_id") or "")
    cv = names.get("cvs", {}).get(summary.get("resume_id") or "")
    if user is None and cv and cv.get("userId"):
        user = names.get("users", {}).get(cv["userId"])
    job = names.get("jobs", {}).get(summary.get("job_id") or "")
    cache_hit = done.get("cache_hit")
    return {
        "runId": summary["run_id"],
        "flow": summary.get("flow"),
        "status": status,
        "startedAt": _iso(started),
        "finishedAt": _iso(finished),
        "durationMs": duration,
        "user": (
            {"id": user["id"], "name": user.get("name"), "email": user.get("email")}
            if user
            else ({"id": summary["user_id"], "name": None, "email": None} if summary.get("user_id") else None)
        ),
        "cv": (
            {"id": cv["id"], "fileName": cv.get("fileName")}
            if cv
            else ({"id": summary["resume_id"], "fileName": None} if summary.get("resume_id") else None)
        ),
        "job": (
            {"id": job["id"], "title": job.get("title")}
            if job
            else ({"id": summary["job_id"], "title": None} if summary.get("job_id") else None)
        ),
        "decision": done.get("decision"),
        "fitBand": done.get("fit_band"),
        "suitabilityScore": _float(done.get("suitability_score")),
        "cacheHit": cache_hit if isinstance(cache_hit, bool) else None,
        "currentStep": summary.get("current_stage") or "request",
        "aiCalls": int((ai or {}).get("calls") or 0),
        "aiErrors": int((ai or {}).get("errors") or 0),
        "warnings": _warning_codes(summary, stalled),
    }


# --------------------------------------------------------------------------- steps


def step_keys(flow: str | None, events: list[dict[str, Any]]) -> list[str]:
    stages = {e["stage"] for e in events}
    extra = set(_FLOW_CLARIFICATION_STEPS.get(flow or "", ())) | (stages & set(CLARIFICATION_STEPS))
    return [*BASE_STEPS, *(key for key in CLARIFICATION_STEPS if key in extra)]


def _execution_order(keys: list[str]) -> list[str]:
    """The order steps actually run in: clarification steps come before done."""
    base = [k for k in keys if k != "done" and k not in CLARIFICATION_STEPS]
    return [*base, *(k for k in keys if k in CLARIFICATION_STEPS), "done"]


def _summary(key: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    latest = events[-1]
    summary = {k: v for k, v in latest["payload"].items() if k not in _SUMMARY_EXCLUDED}
    if key == "request":
        enqueued = next((e for e in events if e["event"] == "match_enqueued"), None)
        requested = next((e for e in events if e["event"] == "match_requested"), latest)
        summary = {k: v for k, v in requested["payload"].items() if k not in _SUMMARY_EXCLUDED}
        if enqueued:
            summary["task_id"] = enqueued["payload"].get("task_id")
    if len(events) > 1 and key not in {"request"}:
        summary["passes"] = len(events)
    return summary


def _step(key: str, status: str, *, started_at: Any = None, duration_ms: Any = None, summary=None) -> dict:
    return {
        "key": key,
        "status": status,
        "startedAt": _iso(started_at),
        "durationMs": duration_ms,
        "summary": summary or {},
    }


def _cached_scoring_summary(done_payload: dict[str, Any]) -> dict[str, Any]:
    scoring = done_payload.get("scoring")
    if not isinstance(scoring, dict):
        return {"reason": "cache_hit"}
    return {"reason": "cache_hit", **{k: v for k, v in scoring.items() if k not in _SUMMARY_EXCLUDED}}


def build_steps(
    flow: str | None, events: list[dict[str, Any]], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Ordered ``Step`` list for one run (events oldest first)."""
    now = now or datetime.now(UTC)
    events = [_normalize(e) for e in events]
    flow = flow or (events[-1].get("flow") if events else None)
    keys = step_keys(flow, events)
    by_stage: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
    for event in events:
        if event["stage"] in by_stage:
            by_stage[event["stage"]].append(event)
    summary = summarize_run("", events) if events else {}
    status, stalled = run_status(summary, now) if events else ("running", False)
    done_payload = _payload(summary.get("done_payload"))
    cache_hit = done_payload.get("cache_hit") is True or any(
        e["event"] == "cache_lookup_completed" and e["payload"].get("hit") is True for e in events
    )

    steps: dict[str, dict[str, Any]] = {}
    for key in keys:
        stage_events = by_stage[key]
        if stage_events:
            latest = stage_events[-1]
            first = stage_events[0]
            durations = [e["duration_ms"] for e in stage_events if isinstance(e.get("duration_ms"), int)]
            started_at = first["created_at"]
            if started_at is not None and isinstance(first.get("duration_ms"), int):
                started_at = started_at - timedelta(milliseconds=first["duration_ms"])
            event_status = latest.get("status")
            if key == "request":
                step_status = "done"
            elif event_status == "error":
                step_status = "error"
            elif event_status == "skipped":
                step_status = "skipped"
            elif event_status == "started":
                step_status = "running" if status == "running" else "done"
            else:
                step_status = "done"
            step_summary = _summary(key, stage_events)
            if event_status == "warning":
                step_summary["warning"] = True
            steps[key] = _step(
                key,
                step_status,
                started_at=started_at,
                duration_ms=sum(durations) if durations else None,
                summary=step_summary,
            )
        elif cache_hit and key in _CACHED_STEPS:
            steps[key] = _step(
                key,
                "skipped",
                summary=(
                    _cached_scoring_summary(done_payload) if key == "scoring" else {"reason": "cache_hit"}
                ),
            )
        elif key in {"cache", "cache_write"} and flow not in _CACHED_FLOWS:
            steps[key] = _step(key, "skipped", summary={"reason": "not_cached"})

    # Steps with no event yet: running (the next one), pending, or never reached.
    order = [k for k in _execution_order(keys) if k in keys]
    reached = [i for i, k in enumerate(order) if k in steps and by_stage[k]]
    last_reached = max(reached) if reached else -1
    next_marked = False
    for index, key in enumerate(order):
        if key in steps:
            continue
        if stalled and key == "done":
            steps[key] = _step(key, "error", summary={"reason": "stalled"})
        elif status == "running" and index > last_reached and not next_marked:
            steps[key] = _step(key, "running")
            next_marked = True
        elif stalled and index > last_reached and not next_marked:
            steps[key] = _step(key, "error", summary={"reason": "stalled"})
            next_marked = True
        elif status == "running":
            steps[key] = _step(key, "pending")
        else:
            steps[key] = _step(key, "skipped", summary={"reason": "not_reached"})
    return [steps[key] for key in keys]


# --------------------------------------------------------------------------- detail sections


def _requirements_section(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = next((e for e in reversed(events) if e["event"] == "requirements_evaluated"), None)
    if latest is None:
        return None
    payload = latest["payload"]
    return {
        "total": payload.get("total"),
        "byStatus": payload.get("counts_by_status") or {},
        "byEvaluator": payload.get("counts_by_evaluator") or {},
        "items": [
            {
                "requirementId": item.get("requirement_id"),
                "label": item.get("label"),
                "priority": item.get("priority"),
                "status": item.get("status"),
                "evaluator": item.get("evaluator"),
                "reasonCode": item.get("reason_code"),
                "confidence": item.get("confidence"),
            }
            for item in payload.get("items") or []
            if isinstance(item, dict)
        ],
    }


def _scoring_section(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = next((e for e in reversed(events) if e["event"] == "match_scored"), None)
    payload = latest["payload"] if latest else None
    if payload is None:
        done = _latest(events, "done")
        cached = (done or {}).get("payload", {}).get("scoring")
        payload = cached if isinstance(cached, dict) else None
    if payload is None:
        return None
    factors = payload.get("factors") or {}
    return {
        "eligibility": payload.get("eligibility"),
        "compatibilityStatus": payload.get("compatibility_status"),
        "suitabilityScore": _float(payload.get("suitability_score")),
        "diagnosticScore": _float(payload.get("diagnostic_score")),
        "fitBand": payload.get("fit_band"),
        "decision": payload.get("decision"),
        "provenanceMode": payload.get("provenance_mode"),
        "factors": [
            {"name": name, "score": _float(value.get("score")), "weight": _float(value.get("weight"))}
            for name, value in factors.items()
            if isinstance(value, dict)
        ],
        "warnings": [str(w) for w in payload.get("warnings") or []],
    }


def _clarification_section(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    sections = {
        "gate": "clarification_gate_completed",
        "questions": "clarification_questions_completed",
        "rescore": "rescore_completed",
    }
    found = {
        key: next((e["payload"] for e in reversed(events) if e["event"] == name), None)
        for key, name in sections.items()
    }
    return found if any(value is not None for value in found.values()) else None


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": _iso(event.get("created_at")),
        "stage": event["stage"],
        "event": event["event"],
        "status": event["status"],
        "durationMs": event.get("duration_ms"),
        "payload": event["payload"],
    }


def assemble_run_detail(
    run_id: str,
    events: list[dict[str, Any]],
    ai_calls: list[dict[str, Any]],
    *,
    names: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The run detail response (without ``success``)."""
    now = now or datetime.now(UTC)
    events = [_normalize(e) for e in events]
    summary = summarize_run(run_id, events)
    ai = {"calls": len(ai_calls), "errors": sum(1 for c in ai_calls if c.get("status") == "error")}
    return {
        "run": run_view(summary, names=names, ai=ai, now=now),
        "steps": build_steps(summary.get("flow"), events, now=now),
        "requirements": _requirements_section(events),
        "scoring": _scoring_section(events),
        "clarification": _clarification_section(events),
        "events": [_event_view(e) for e in events[-_EVENT_LIMIT:]],
        "ai": {"summary": summarize_ai_calls(ai_calls), "calls": [ai_call_view(c) for c in ai_calls]},
    }


# --------------------------------------------------------------------------- queries


async def _mappings(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


async def resolve_names(
    db: AsyncSession, summaries: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Users, CVs and jobs of the runs: one batched query each, deleted rows tolerated."""
    resume_ids = sorted({s["resume_id"] for s in summaries if s.get("resume_id")})
    job_ids = sorted({s["job_id"] for s in summaries if s.get("job_id")})
    cvs: dict[str, dict[str, Any]] = {}
    if resume_ids:
        for row in await _mappings(
            db, "SELECT id, user_id, filename FROM user_cvs WHERE id = ANY(:ids)", {"ids": resume_ids}
        ):
            cvs[str(row["id"])] = {
                "id": str(row["id"]),
                "fileName": row["filename"],
                "userId": row["user_id"],
            }
    user_ids = sorted(
        {s["user_id"] for s in summaries if s.get("user_id")}
        | {cv["userId"] for cv in cvs.values() if cv.get("userId")}
    )
    users: dict[str, dict[str, Any]] = {}
    if user_ids:
        for row in await _mappings(
            db, "SELECT id, name, email FROM users WHERE id = ANY(:ids)", {"ids": user_ids}
        ):
            users[str(row["id"])] = {"id": str(row["id"]), "name": row["name"], "email": row["email"]}
    jobs: dict[str, dict[str, Any]] = {}
    if job_ids:
        for row in await _mappings(
            db, "SELECT id, title FROM job_descriptions WHERE id = ANY(:ids)", {"ids": job_ids}
        ):
            jobs[str(row["id"])] = {"id": str(row["id"]), "title": row["title"]}
    return {"users": users, "cvs": cvs, "jobs": jobs}


async def _ai_counts(db: AsyncSession, run_ids: list[str]) -> dict[str, dict[str, int]]:
    if not run_ids:
        return {}
    rows = await _mappings(
        db,
        "SELECT match_run_id, COUNT(*) AS calls, COUNT(*) FILTER (WHERE status = 'error') AS errors "
        "FROM ai_call_logs WHERE match_run_id = ANY(:ids) GROUP BY match_run_id",
        {"ids": run_ids},
    )
    return {str(r["match_run_id"]): {"calls": r["calls"], "errors": r["errors"]} for r in rows}


_STALLED_SQL = "make_interval(mins => 10)"
_STATUS_FILTERS = {
    "completed": "done_event = 'match_completed'",
    "failed": f"(done_event = 'match_failed' OR (done_event IS NULL AND last_at < now() - {_STALLED_SQL}))",
    "running": f"(done_event IS NULL AND last_at >= now() - {_STALLED_SQL})",
}
_RUNS_CTE = """
    WITH runs AS (
      SELECT run_id,
             (ARRAY_AGG(flow ORDER BY created_at DESC, id DESC))[1] AS flow,
             MIN(created_at) AS started_at,
             MAX(created_at) AS last_at,
             (ARRAY_AGG(stage ORDER BY created_at DESC, id DESC))[1] AS current_stage,
             (ARRAY_AGG(event ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
               AS done_event,
             (ARRAY_AGG(created_at ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
               AS done_at,
             (ARRAY_AGG(payload ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
               AS done_payload,
             (ARRAY_AGG(payload -> 'warnings' ORDER BY created_at DESC, id DESC)
                FILTER (WHERE event = 'match_scored'))[1] AS scoring_warnings,
             ARRAY_AGG(event ORDER BY created_at, id) FILTER (WHERE status IN ('warning', 'error'))
               AS problem_events,
             (ARRAY_AGG(user_id ORDER BY created_at, id) FILTER (WHERE user_id IS NOT NULL))[1] AS user_id,
             (ARRAY_AGG(resume_id ORDER BY created_at, id) FILTER (WHERE resume_id IS NOT NULL))[1]
               AS resume_id,
             (ARRAY_AGG(job_id ORDER BY created_at, id) FILTER (WHERE job_id IS NOT NULL))[1] AS job_id
      FROM matching_pipeline_events
      WHERE created_at >= now() - make_interval(days => :days)
      GROUP BY run_id
    )
"""
_SEARCH_JOINS = """
    LEFT JOIN user_cvs c ON c.id = runs.resume_id
    LEFT JOIN users u ON u.id = COALESCE(runs.user_id, c.user_id)
    LEFT JOIN job_descriptions j ON j.id = runs.job_id
"""
_SEARCH_FILTER = (
    "(LOWER(COALESCE(u.name, '')) LIKE :q OR LOWER(COALESCE(u.email, '')) LIKE :q "
    "OR LOWER(COALESCE(c.filename, '')) LIKE :q OR LOWER(COALESCE(j.title, '')) LIKE :q)"
)


def runs_query(*, status: str, q: str | None) -> tuple[str, str, bool]:
    """(page SQL, count SQL, has search) for the run list filters."""
    clauses = []
    if status in _STATUS_FILTERS:
        clauses.append(_STATUS_FILTERS[status])
    search = bool(q and q.strip())
    if search:
        clauses.append(_SEARCH_FILTER)
    joins = _SEARCH_JOINS if search else ""
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    page = (
        f"{_RUNS_CTE} SELECT runs.* FROM runs {joins} {where} "
        "ORDER BY runs.started_at DESC, runs.run_id LIMIT :limit OFFSET :offset"
    )
    count = f"{_RUNS_CTE} SELECT COUNT(*) AS total FROM runs {joins} {where}"
    return page, count, search


async def list_runs(
    db: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str = "all",
    days: int = 7,
    q: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``{"items": [Run], "total"}``; raises when the table is missing (caller maps to 503)."""
    page_sql, count_sql, search = runs_query(status=status, q=q)
    params: dict[str, Any] = {"days": days}
    if search:
        params["q"] = f"%{(q or '').strip().lower()}%"
    rows = await _mappings(db, page_sql, {**params, "limit": limit, "offset": offset})
    total_rows = await _mappings(db, count_sql, params)
    total = int((total_rows[0]["total"] if total_rows else 0) or 0)
    names = await resolve_names(db, rows)
    ai = await _ai_counts(db, [str(r["run_id"]) for r in rows])
    now = now or datetime.now(UTC)
    return {
        "items": [run_view(row, names=names, ai=ai.get(str(row["run_id"])), now=now) for row in rows],
        "total": total,
    }


async def load_run_detail(db: AsyncSession, run_id: str) -> dict[str, Any] | None:
    """The run detail, or None when the run has no events."""
    events = await _mappings(
        db,
        f"""
        SELECT * FROM (
            SELECT id, run_id, flow, cache_key, user_id, resume_id, job_id, stage, event, status,
                   duration_ms, payload, created_at
            FROM matching_pipeline_events WHERE run_id = :rid
            ORDER BY created_at DESC, id DESC LIMIT {_EVENT_LIMIT * 4}
        ) recent ORDER BY created_at, id
        """,
        {"rid": run_id},
    )
    if not events:
        return None
    calls = await _mappings(
        db,
        f"""
        SELECT * FROM (
            SELECT * FROM ai_call_logs WHERE match_run_id = :rid
            ORDER BY created_at DESC, id DESC LIMIT {_AI_CALL_LIMIT}
        ) recent ORDER BY created_at, id
        """,
        {"rid": run_id},
    )
    names = await resolve_names(db, [summarize_run(run_id, events)])
    return assemble_run_detail(run_id, events, calls, names=names)

