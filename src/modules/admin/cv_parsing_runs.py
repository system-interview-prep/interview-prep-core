"""Uploaded CVs for the admin CV-parsing view: list and step-by-step detail.

Lists ``user_cvs`` (every CV uploaded in the window, also those uploaded
before migration 0033, ``tracked: false``) with an aggregate of
``cv_pipeline_events`` (written while a CV context is active, see
src/modules/user_cvs/application/run_tracking.py) and the model calls stamped
with the CV (``ai_call_logs.cv_id``):

    upload -> queue -> extract -> source -> parse -> quality -> save -> done

Steps show the latest parse attempt (the upload belongs to the CV). Status
comes from ``user_cvs.status`` and the canonical parsing status stored in
``parsed_data.parsing.status``; a PENDING/PARSING row untouched for 15 minutes
is stalled (the parse claim takes such rows over after the same delay).

``summarize_events``, ``cv_status``, ``run_view``, ``build_steps`` and
``assemble_cv_detail`` are pure and unit tested; ``list_cv_runs`` and
``load_cv_detail`` only run the queries. Event payload fields are returned as
stored (snake_case), the read model around them is camelCase.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.matching_runs import _as_dt, _iso, _ms_between, _payload
from src.modules.interviews.facade import ai_call_view, summarize_ai_calls

STEPS = ("upload", "queue", "extract", "source", "parse", "quality", "save", "done")
# Stages of a parse attempt (an attempt also starts with ``cv_parse_claimed``).
_ATTEMPT_STAGES = frozenset({"extract", "source", "parse", "quality", "save", "done"})
STALLED_AFTER = timedelta(minutes=15)
_EVENT_LIMIT = 500
_AI_CALL_LIMIT = 300
_MAX_WARNINGS = 40
# Large payload fields kept out of step summaries (shown in their own sections).
_SUMMARY_EXCLUDED = frozenset(
    {
        "duration_ms",
        "warnings",
        "checks",
        "canonical_section_coverage",
        "source_section_block_counts",
        "failed",
    }
)
_SECTION_KEYS = ("skills", "employment", "education", "projects", "certifications", "languages", "evidence")
_FLAG_CODES = {
    "provider_fallback_used": "provider_fallback",
    "cv_dispatch_failed": "dispatch_failed",
    "retry_scheduled": "retry",
}
_IGNORED_FOR_CURRENT = frozenset({"cv_upload_duplicate", "not_claimed"})
NOT_MIGRATED = "CV pipeline events are not migrated (0033)"


# --------------------------------------------------------------------------- helpers


def _normalize(event: dict[str, Any]) -> dict[str, Any]:
    return {**event, "payload": _payload(event.get("payload")), "created_at": _as_dt(event.get("created_at"))}


def _sort_key(event: dict[str, Any]) -> tuple:
    return (event.get("created_at") or datetime.min.replace(tzinfo=UTC), event.get("id") or 0)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _is_attempt_event(event: dict[str, Any]) -> bool:
    return event["event"] == "cv_parse_claimed" or event["stage"] in _ATTEMPT_STAGES


def _section_of(path: Any) -> str | None:
    if not path:
        return None
    head = str(path).split(".", 1)[0].split("[", 1)[0].split("/", 1)[0].strip()
    return head[:40] if head and head.replace("_", "").isalnum() else None


def _warning_view(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict) or not item.get("code"):
        return None
    section = item.get("section") if "section" in item else _section_of(item.get("path"))
    return {"code": str(item["code"])[:64], "severity": item.get("severity"), "section": section}


# --------------------------------------------------------------------------- run summary


def summarize_events(cv_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    """The aggregate the list query computes in SQL, from one CV's events (oldest first)."""
    events = sorted((_normalize(e) for e in events), key=_sort_key)

    def latest(predicate) -> dict[str, Any] | None:
        return next((e for e in reversed(events) if predicate(e)), None)

    relevant = latest(lambda e: e["event"] not in _IGNORED_FOR_CURRENT)
    done = latest(lambda e: e["stage"] == "done")
    quality = latest(lambda e: e["event"] == "quality_gate")
    extraction = latest(lambda e: e["event"] == "extraction_completed")
    versioned = latest(lambda e: e["payload"].get("parser_version") is not None)
    attempt_starts = [e["created_at"] for e in events if _is_attempt_event(e)]
    uploads = [e["created_at"] for e in events if e["event"] == "cv_uploaded"]
    return {
        "cv_id": cv_id,
        # Events of this CV's own pipeline (a duplicate upload of an older CV does not count).
        "event_count": sum(1 for e in events if e["event"] != "cv_upload_duplicate"),
        "started_at": min(attempt_starts) if attempt_starts else None,
        "last_at": events[-1]["created_at"] if events else None,
        "current_stage": relevant["stage"] if relevant else None,
        "last_event": relevant["event"] if relevant else None,
        "done_event": done["event"] if done else None,
        "done_at": done["created_at"] if done else None,
        "done_payload": done["payload"] if done else None,
        "uploaded_event_at": min(uploads) if uploads else None,
        "quality_payload": quality["payload"] if quality else None,
        "provider": (extraction["payload"].get("provider") if extraction else None),
        "parser_version": (str(versioned["payload"]["parser_version"]) if versioned else None),
        "attempts": len({e.get("attempt_id") for e in events if _is_attempt_event(e)}),
        "flag_events": sorted({e["event"] for e in events if e["event"] in _FLAG_CODES}),
        "reparsed": any(e["event"] == "cv_enqueued" and e["payload"].get("reparse") is True for e in events),
    }


def cv_status(db_status: Any, canonical_status: Any, updated_at: Any, now: datetime) -> tuple[str, bool]:
    """(running | completed | review | failed, stalled) from the ``user_cvs`` row."""
    status = str(db_status or "").upper()
    if status == "DONE":
        return ("review" if canonical_status == "review_required" else "completed"), False
    if status == "FAILED":
        return "failed", False
    updated = _as_dt(updated_at)
    if updated is not None and now - updated > STALLED_AFTER:
        return "failed", True
    return "running", False


def _untracked_current_step(db_status: str) -> str:
    return {"PENDING": "queue", "PARSING": "extract"}.get(db_status, "done")


def run_view(
    row: dict[str, Any],
    summary: dict[str, Any] | None = None,
    *,
    ai: dict[str, int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One ``Run`` of the admin contract from a ``user_cvs`` row and its event aggregate."""
    now = now or datetime.now(UTC)
    summary = summary or {}
    tracked = bool(summary.get("event_count"))
    db_status = str(row.get("db_status") or "").upper()
    canonical = row.get("canonical_status")
    status, stalled = cv_status(db_status, canonical, row.get("updated_at"), now)
    finished_state = status != "running" and not stalled
    done_at = _as_dt(summary.get("done_at"))
    done = _payload(summary.get("done_payload"))
    quality = _payload(summary.get("quality_payload"))

    finished = None
    duration = None
    if finished_state:
        finished = done_at if tracked and done_at else _as_dt(row.get("updated_at"))
        if tracked and done_at:
            uploaded = _as_dt(summary.get("uploaded_event_at"))
            if uploaded is not None and not summary.get("reparsed"):
                duration = _ms_between(uploaded, done_at)
            else:
                duration = _int(done.get("duration_ms"))

    if tracked:
        current = summary.get("current_stage") or "upload"
        if summary.get("last_event") == "cv_dispatch_failed":
            current = "queue"
        elif status == "running" and not stalled and current == "done":
            current = "queue"  # reparse or retry waiting for a worker
        elif finished_state and summary.get("done_event"):
            current = "done"
    else:
        current = _untracked_current_step(db_status)

    parse_source = str(row.get("parse_source") or "")
    provider = summary.get("provider") or (parse_source.split("+", 1)[0] if "+" in parse_source else None)
    section_counts = quality.get("section_counts")
    if not isinstance(section_counts, dict):
        stored = _payload(row.get("stored_section_counts"))
        section_counts = stored or None
    warnings_count = _int(quality.get("warnings_count"))
    if warnings_count is None:
        warnings_count = _int(row.get("stored_warnings_count")) or 0

    codes: list[str] = []
    if stalled:
        codes.append("stalled")
    flags = summary.get("flag_events") or []
    for event in ("provider_fallback_used", "cv_dispatch_failed", "retry_scheduled"):
        if event in flags:
            codes.append(_FLAG_CODES[event])
    if status == "review":
        codes.append("review_required")

    user_id = row.get("user_id")
    return {
        "cvId": str(row["cv_id"]),
        "tracked": tracked,
        "status": status,
        "dbStatus": row.get("db_status"),
        "canonicalStatus": canonical,
        "uploadedAt": _iso(row.get("created_at")),
        "startedAt": _iso(summary.get("started_at")),
        "finishedAt": _iso(finished),
        "durationMs": duration,
        "attempts": int(summary.get("attempts") or 0),
        "user": (
            {"id": str(user_id), "name": row.get("user_name"), "email": row.get("user_email")}
            if user_id
            else None
        ),
        "cv": {
            "id": str(row["cv_id"]),
            "fileName": row.get("filename"),
            "sizeBytes": _int(row.get("size")),
            "contentType": row.get("content_type"),
        },
        "currentStep": current,
        "provider": provider,
        "parserVersion": summary.get("parser_version") or row.get("stored_parser_version"),
        "sectionCounts": section_counts,
        "warningsCount": warnings_count,
        "aiCalls": int((ai or {}).get("calls") or 0),
        "aiErrors": int((ai or {}).get("errors") or 0),
        "warnings": list(dict.fromkeys(codes)),
    }


# --------------------------------------------------------------------------- steps


def _step(key: str, status: str, *, started_at: Any = None, duration_ms: Any = None, summary=None) -> dict:
    return {
        "key": key,
        "status": status,
        "startedAt": _iso(started_at),
        "durationMs": duration_ms,
        "summary": summary or {},
    }


def select_step_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The events the steps show: upload events plus the latest parse attempt.

    Returns ``{"by_stage", "attempt_id", "pending_enqueue", "dispatch_failed"}``;
    ``pending_enqueue``/``dispatch_failed`` is a queue event newer than the
    latest attempt (a reparse or upload waiting for, or failing to reach, a worker).
    """
    events = sorted((_normalize(e) for e in events), key=_sort_key)
    starts: dict[str, datetime | None] = {}
    for event in events:
        if _is_attempt_event(event):
            starts.setdefault(event.get("attempt_id") or "", event["created_at"])
    attempt_id = max(starts, key=lambda a: starts[a] or datetime.min.replace(tzinfo=UTC)) if starts else None
    attempt_start = starts.get(attempt_id) if attempt_id is not None else None

    def newer(event: dict[str, Any] | None) -> bool:
        if event is None:
            return False
        if attempt_start is None:
            return True
        return event["created_at"] is not None and event["created_at"] > attempt_start

    enqueues = [e for e in events if e["event"] == "cv_enqueued"]
    dispatch = next((e for e in reversed(events) if e["event"] == "cv_dispatch_failed"), None)
    last_enqueue = enqueues[-1] if enqueues else None
    pending_dispatch = (
        dispatch
        if newer(dispatch) and (last_enqueue is None or dispatch["created_at"] >= last_enqueue["created_at"])
        else None
    )
    pending_enqueue = last_enqueue if newer(last_enqueue) and pending_dispatch is None else None

    by_stage: dict[str, list[dict[str, Any]]] = {key: [] for key in STEPS}
    by_stage["upload"] = [e for e in events if e["stage"] == "upload"]
    if pending_dispatch is not None or pending_enqueue is not None or attempt_id is None:
        if pending_enqueue is not None:
            by_stage["queue"] = [pending_enqueue]
        attempt_id = None
    else:
        before = [
            e
            for e in enqueues
            if attempt_start is None or (e["created_at"] and e["created_at"] <= attempt_start)
        ]
        attempt_events = [
            e for e in events if (e.get("attempt_id") or "") == attempt_id and e["stage"] != "upload"
        ]
        by_stage["queue"] = ([before[-1]] if before else []) + [
            e for e in attempt_events if e["stage"] == "queue"
        ]
        for event in attempt_events:
            if event["stage"] in _ATTEMPT_STAGES:
                by_stage[event["stage"]].append(event)
    return {
        "by_stage": by_stage,
        "attempt_id": attempt_id,
        "pending_enqueue": pending_enqueue,
        "dispatch_failed": pending_dispatch,
    }


def _clean_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _SUMMARY_EXCLUDED}


def _upload_step(events: list[dict[str, Any]]) -> dict[str, Any]:
    uploaded = next((e for e in events if e["event"] == "cv_uploaded"), None)
    duplicates = sum(1 for e in events if e["event"] == "cv_upload_duplicate")
    if uploaded is None:
        summary: dict[str, Any] = {"reason": "duplicate" if duplicates else "untracked"}
        if duplicates:
            summary["duplicates"] = duplicates
        first = events[0] if events else None
        return _step("upload", "done", started_at=first["created_at"] if first else None, summary=summary)
    summary = _clean_summary(uploaded["payload"])
    if duplicates:
        summary["duplicates"] = duplicates
    if any(e["event"] == "cv_dispatch_failed" for e in events):
        summary["dispatch_failed"] = True
    duration = _int(uploaded.get("duration_ms"))
    started = uploaded["created_at"]
    if started is not None and duration is not None:
        started = started - timedelta(milliseconds=duration)
    return _step("upload", "done", started_at=started, duration_ms=duration, summary=summary)


def _untracked_steps(db_status: str, status: str, stalled: bool) -> list[dict[str, Any]]:
    untracked = {"reason": "untracked"}
    if db_status == "DONE":
        return [_step(key, "done", summary=untracked) for key in STEPS]
    if status == "failed" and not stalled:
        return [
            _step("upload", "done", summary=untracked),
            *(_step(key, "skipped", summary=untracked) for key in STEPS[1:-1]),
            _step("done", "error", summary=untracked),
        ]
    active = "extract" if db_status == "PARSING" else "queue"
    steps = []
    reached = True
    for key in STEPS:
        if key == active:
            steps.append(
                _step(key, "error", summary={"reason": "stalled"}) if stalled else _step(key, "running")
            )
            reached = False
        elif reached:
            steps.append(_step(key, "done", summary=untracked))
        elif stalled:
            steps.append(
                _step(
                    key,
                    "error" if key == "done" else "skipped",
                    summary={"reason": "stalled" if key == "done" else "not_reached"},
                )
            )
        else:
            steps.append(_step(key, "pending"))
    return steps


def build_steps(
    events: list[dict[str, Any]],
    *,
    db_status: Any,
    canonical_status: Any = None,
    updated_at: Any = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Ordered ``Step`` list for one CV (events in any order)."""
    now = now or datetime.now(UTC)
    db_status = str(db_status or "").upper()
    status, stalled = cv_status(db_status, canonical_status, updated_at, now)
    if not events:
        return _untracked_steps(db_status, status, stalled)
    selected = select_step_events(events)
    by_stage = selected["by_stage"]
    if selected["attempt_id"] is None and not by_stage["queue"] and selected["dispatch_failed"] is None:
        # No parse attempt recorded (parsed before migration 0033): infer from the row.
        return [_upload_step(by_stage["upload"]), *_untracked_steps(db_status, status, stalled)[1:]]
    running = status == "running"
    done_events = by_stage["done"]
    done_latest = done_events[-1] if done_events else None
    failed_stage = None
    if done_latest is not None and done_latest["event"] in {"failed", "retry_scheduled"}:
        failed_stage = done_latest["payload"].get("failed_stage")
    dispatch = selected["dispatch_failed"]

    active = running and not stalled
    reached_index = [i for i, k in enumerate(STEPS) if k != "upload" and by_stage[k]]
    last_reached = max(reached_index) if reached_index else 0
    failed_reason = done_latest["payload"].get("reason") if done_latest else None

    steps: dict[str, dict[str, Any]] = {"upload": _upload_step(by_stage["upload"])}
    halted = False  # a stalled step was marked: later steps were never reached
    for index, key in enumerate(STEPS[1:], start=1):
        stage_events = by_stage[key]
        if not stage_events:
            continue
        latest = stage_events[-1]
        first = stage_events[0]
        event_status = latest.get("status")
        summary = _clean_summary(latest["payload"])
        is_last = index == last_reached
        if key == failed_stage or event_status == "error":
            step_status = "error"
        elif key == "done" and latest["event"] == "retry_scheduled":
            step_status = "running" if active else "error"
            summary.setdefault("reason", "retry")
        elif event_status == "skipped":
            step_status = "skipped"
        elif is_last and stalled and key != "done":
            step_status = "error"
            summary["reason"] = "stalled"
            halted = True
        elif event_status == "started" or (key == "queue" and latest["event"] == "cv_enqueued"):
            step_status = "running" if active and is_last else "done"
        else:
            step_status = "done"
        if event_status == "warning":
            summary["warning"] = True
        if key == "extract":
            failures = sum(1 for e in stage_events if e["event"] == "provider_failed")
            if failures:
                summary["provider_failures"] = failures
        started_at = first["created_at"]
        if key == "queue":
            enqueue = next((e for e in stage_events if e["event"] == "cv_enqueued"), None)
            claim = next((e for e in stage_events if e["event"] == "cv_parse_claimed"), None)
            duration = _ms_between(enqueue["created_at"], claim["created_at"]) if enqueue and claim else None
        else:
            durations = [d for d in (_int(e.get("duration_ms")) for e in stage_events) if d is not None]
            duration = sum(durations) if durations else None
            if started_at is not None and _int(first.get("duration_ms")) is not None:
                started_at = started_at - timedelta(milliseconds=_int(first["duration_ms"]))
        steps[key] = _step(key, step_status, started_at=started_at, duration_ms=duration, summary=summary)

    if dispatch is not None:
        steps["queue"] = _step(
            "queue",
            "error",
            started_at=dispatch["created_at"],
            summary={"reason": "dispatch_failed", "error_type": dispatch["payload"].get("error_type")},
        )

    # Steps with no event yet: running (the next one), pending, or never reached.
    next_marked = halted or steps.get(STEPS[last_reached], {}).get("status") == "running"
    for index, key in enumerate(STEPS):
        if key in steps:
            continue
        if key == failed_stage:
            steps[key] = _step(key, "error", summary={"reason": failed_reason or "failed"})
        elif dispatch is not None:
            reason = "dispatch_failed" if key == "done" else "not_reached"
            steps[key] = _step(key, "error" if key == "done" else "skipped", summary={"reason": reason})
        elif stalled and key == "done":
            steps[key] = _step(key, "error", summary={"reason": "stalled"})
        elif stalled and index > last_reached and not next_marked:
            steps[key] = _step(key, "error", summary={"reason": "stalled"})
            next_marked = True
        elif active and index > last_reached and not next_marked:
            steps[key] = _step(key, "running")
            next_marked = True
        elif active:
            steps[key] = _step(key, "pending")
        else:
            steps[key] = _step(key, "skipped", summary={"reason": "not_reached"})
    return [steps[key] for key in STEPS]


# --------------------------------------------------------------------------- detail sections


def extraction_section(
    by_stage: dict[str, list[dict[str, Any]]], *, parse_source: Any = None, parsing: Any = None
) -> dict[str, Any] | None:
    events = by_stage.get("extract") or []
    used = next((e for e in reversed(events) if e["event"] == "extraction_completed"), None)
    attempts = []
    for event in events:
        payload = event["payload"]
        outcome = {
            "provider_failed": "failed",
            "provider_skipped": "skipped",
            "extraction_completed": "used",
        }.get(event["event"])
        if outcome:
            attempts.append(
                {
                    "provider": payload.get("provider"),
                    "outcome": outcome,
                    "errorType": payload.get("error_type") if outcome != "used" else None,
                }
            )
    if events:
        payload = used["payload"] if used else {}
        return {
            "provider": payload.get("provider"),
            "extractorVersion": payload.get("extractor_version"),
            "markdownCharacters": _int(payload.get("markdown_characters")),
            "contentBlocks": _int(payload.get("content_blocks")),
            "attempts": attempts,
        }
    source = str(parse_source or "")
    stored = _payload(parsing)
    if "+" not in source and not stored.get("extractionVersion"):
        return None
    return {
        "provider": source.split("+", 1)[0] if "+" in source else None,
        "extractorVersion": stored.get("extractionVersion"),
        "markdownCharacters": None,
        "contentBlocks": None,
        "attempts": [],
    }


def quality_section(
    by_stage: dict[str, list[dict[str, Any]]],
    *,
    parsing: Any = None,
    section_counts: Any = None,
) -> dict[str, Any] | None:
    gate = next((e for e in reversed(by_stage.get("quality") or []) if e["event"] == "quality_gate"), None)
    if gate is not None:
        payload = gate["payload"]
        return {
            "status": payload.get("status"),
            "parserVersion": payload.get("parser_version"),
            "sourceCharacters": _int(payload.get("source_characters")),
            "sectionCounts": payload.get("section_counts")
            if isinstance(payload.get("section_counts"), dict)
            else None,
            "coverage": {
                "rawText": payload.get("raw_text_coverage"),
                "evidence": payload.get("evidence_index_coverage"),
                "sections": payload.get("canonical_section_coverage") or {},
            },
            "reasonCodes": [str(c) for c in payload.get("coverage_reason_codes") or []],
            "checks": {str(k): bool(v) for k, v in (payload.get("checks") or {}).items()},
            "warnings": [w for w in (_warning_view(i) for i in payload.get("warnings") or []) if w],
        }
    stored = _payload(parsing)
    if not stored:
        return None
    counts = _payload(section_counts)
    return {
        "status": stored.get("status"),
        "parserVersion": stored.get("parserVersion"),
        "sourceCharacters": None,
        "sectionCounts": counts or None,
        "coverage": {
            "rawText": stored.get("rawTextCoverage"),
            "evidence": stored.get("evidenceIndexCoverage"),
            "sections": stored.get("canonicalSectionCoverage") or {},
        },
        "reasonCodes": [str(c) for c in stored.get("coverageReasonCodes") or []],
        "checks": {},
        "warnings": [
            w for w in (_warning_view(i) for i in (stored.get("warnings") or [])[:_MAX_WARNINGS]) if w
        ],
    }


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": _iso(event.get("created_at")),
        "stage": event["stage"],
        "event": event["event"],
        "status": event["status"],
        "durationMs": event.get("duration_ms"),
        "attemptId": event.get("attempt_id"),
        "payload": event["payload"],
    }


def assemble_cv_detail(
    row: dict[str, Any],
    events: list[dict[str, Any]],
    ai_calls: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The CV detail response from its ``user_cvs`` row (see ``_CV_COLUMNS``)."""
    now = now or datetime.now(UTC)
    events = sorted((_normalize(e) for e in events), key=_sort_key)
    summary = summarize_events(str(row["cv_id"]), events) if events else None
    ai = {"calls": len(ai_calls), "errors": sum(1 for c in ai_calls if c.get("status") == "error")}
    selected = select_step_events(events) if events else {"by_stage": {}}
    parsing = row.get("parsing") if str(row.get("db_status") or "").upper() == "DONE" else None
    return {
        "run": run_view(row, summary, ai=ai, now=now),
        "steps": build_steps(
            events,
            db_status=row.get("db_status"),
            canonical_status=row.get("canonical_status"),
            updated_at=row.get("updated_at"),
            now=now,
        ),
        "extraction": extraction_section(
            selected["by_stage"],
            parse_source=row.get("parse_source") if parsing is not None else None,
            parsing=parsing,
        ),
        "quality": quality_section(
            selected["by_stage"], parsing=parsing, section_counts=row.get("stored_section_counts")
        ),
        "events": [_event_view(e) for e in events[-_EVENT_LIMIT:]],
        "ai": {"summary": summarize_ai_calls(ai_calls), "calls": [ai_call_view(c) for c in ai_calls]},
    }


# --------------------------------------------------------------------------- queries


async def _mappings(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


def _array_length(key: str) -> str:
    return (
        f"CASE WHEN jsonb_typeof(c.parsed_data -> '{key}') = 'array' "
        f"THEN jsonb_array_length(c.parsed_data -> '{key}') ELSE 0 END"
    )


_SECTION_COUNTS_SQL = (
    "CASE WHEN c.parsed_data IS NOT NULL THEN jsonb_build_object("
    + ", ".join(f"'{key}', {_array_length(key)}" for key in _SECTION_KEYS)
    + ") END"
)
_CV_COLUMNS = f"""
    c.id AS cv_id, c.user_id, c.filename, c.size, c.content_type, c.status AS db_status,
    c.parse_source, c.created_at, c.updated_at,
    c.parsed_data -> 'parsing' ->> 'status' AS canonical_status,
    c.parsed_data -> 'parsing' ->> 'parserVersion' AS stored_parser_version,
    CASE WHEN jsonb_typeof(c.parsed_data -> 'parsing' -> 'warnings') = 'array'
         THEN jsonb_array_length(c.parsed_data -> 'parsing' -> 'warnings') END AS stored_warnings_count,
    {_SECTION_COUNTS_SQL} AS stored_section_counts,
    u.name AS user_name, u.email AS user_email
"""
_CV_FROM = "FROM user_cvs c LEFT JOIN users u ON u.id = c.user_id"
_STALE_SQL = "c.updated_at < now() - make_interval(mins => 15)"
_STATUS_FILTERS = {
    "running": f"(c.status IN ('PENDING', 'PARSING') AND NOT ({_STALE_SQL}))",
    "failed": f"(c.status = 'FAILED' OR (c.status IN ('PENDING', 'PARSING') AND {_STALE_SQL}))",
    "review": "(c.status = 'DONE' AND c.parsed_data -> 'parsing' ->> 'status' = 'review_required')",
    "completed": (
        "(c.status = 'DONE' AND COALESCE(c.parsed_data -> 'parsing' ->> 'status', '') <> 'review_required')"
    ),
}
_SEARCH_FILTER = (
    "(LOWER(COALESCE(u.name, '')) LIKE :q OR LOWER(COALESCE(u.email, '')) LIKE :q "
    "OR LOWER(COALESCE(c.filename, '')) LIKE :q)"
)
_EVENTS_AGG = """
    SELECT cv_id,
           COUNT(*) FILTER (WHERE event <> 'cv_upload_duplicate') AS event_count,
           MIN(created_at) FILTER (
             WHERE event = 'cv_parse_claimed'
                OR stage IN ('extract', 'source', 'parse', 'quality', 'save', 'done')
           ) AS started_at,
           MAX(created_at) AS last_at,
           (ARRAY_AGG(stage ORDER BY created_at DESC, id DESC)
              FILTER (WHERE event NOT IN ('cv_upload_duplicate', 'not_claimed')))[1] AS current_stage,
           (ARRAY_AGG(event ORDER BY created_at DESC, id DESC)
              FILTER (WHERE event NOT IN ('cv_upload_duplicate', 'not_claimed')))[1] AS last_event,
           (ARRAY_AGG(event ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
             AS done_event,
           (ARRAY_AGG(created_at ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
             AS done_at,
           (ARRAY_AGG(payload ORDER BY created_at DESC, id DESC) FILTER (WHERE stage = 'done'))[1]
             AS done_payload,
           MIN(created_at) FILTER (WHERE event = 'cv_uploaded') AS uploaded_event_at,
           (ARRAY_AGG(payload ORDER BY created_at DESC, id DESC) FILTER (WHERE event = 'quality_gate'))[1]
             AS quality_payload,
           (ARRAY_AGG(payload ->> 'provider' ORDER BY created_at DESC, id DESC)
              FILTER (WHERE event = 'extraction_completed'))[1] AS provider,
           (ARRAY_AGG(payload ->> 'parser_version' ORDER BY created_at DESC, id DESC)
              FILTER (WHERE payload ->> 'parser_version' IS NOT NULL))[1] AS parser_version,
           COUNT(DISTINCT attempt_id) FILTER (WHERE event = 'cv_parse_claimed' OR stage = 'extract')
             AS attempts,
           ARRAY_AGG(DISTINCT event) FILTER (
             WHERE event IN ('provider_fallback_used', 'cv_dispatch_failed', 'retry_scheduled')
           ) AS flag_events,
           COALESCE(BOOL_OR(event = 'cv_enqueued' AND payload ->> 'reparse' = 'true'), false) AS reparsed
    FROM cv_pipeline_events
    WHERE cv_id = ANY(:ids)
    GROUP BY cv_id
"""


def cv_runs_query(*, status: str, q: str | None) -> tuple[str, str, bool]:
    """(page SQL, count SQL, has search) for the CV list filters."""
    clauses = ["c.created_at >= now() - make_interval(days => :days)"]
    if status in _STATUS_FILTERS:
        clauses.append(_STATUS_FILTERS[status])
    search = bool(q and q.strip())
    if search:
        clauses.append(_SEARCH_FILTER)
    where = "WHERE " + " AND ".join(clauses)
    page = (
        f"SELECT {_CV_COLUMNS} {_CV_FROM} {where} "
        "ORDER BY c.created_at DESC, c.id LIMIT :limit OFFSET :offset"
    )
    count = f"SELECT COUNT(*) AS total {_CV_FROM} {where}"
    return page, count, search


async def _ai_counts(db: AsyncSession, cv_ids: list[str]) -> dict[str, dict[str, int]]:
    rows = await _mappings(
        db,
        "SELECT cv_id, COUNT(*) AS calls, COUNT(*) FILTER (WHERE status = 'error') AS errors "
        "FROM ai_call_logs WHERE cv_id = ANY(:ids) GROUP BY cv_id",
        {"ids": cv_ids},
    )
    return {str(r["cv_id"]): {"calls": r["calls"], "errors": r["errors"]} for r in rows}


async def list_cv_runs(
    db: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str = "all",
    days: int = 7,
    q: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``{"items": [Run], "total"}``; raises when the tables are missing (caller maps to 503)."""
    page_sql, count_sql, search = cv_runs_query(status=status, q=q)
    params: dict[str, Any] = {"days": days}
    if search:
        params["q"] = f"%{(q or '').strip().lower()}%"
    rows = await _mappings(db, page_sql, {**params, "limit": limit, "offset": offset})
    total_rows = await _mappings(db, count_sql, params)
    total = int((total_rows[0]["total"] if total_rows else 0) or 0)
    ids = [str(r["cv_id"]) for r in rows]
    # Run even for an empty page, so a missing migration is reported (503).
    summaries = {str(r["cv_id"]): r for r in await _mappings(db, _EVENTS_AGG, {"ids": ids})}
    ai = await _ai_counts(db, ids)
    now = now or datetime.now(UTC)
    return {
        "items": [
            run_view(row, summaries.get(str(row["cv_id"])), ai=ai.get(str(row["cv_id"])), now=now)
            for row in rows
        ],
        "total": total,
    }


async def load_cv_detail(db: AsyncSession, cv_id: str) -> dict[str, Any] | None:
    """The CV detail, or None when the CV does not exist."""
    rows = await _mappings(
        db,
        f"SELECT {_CV_COLUMNS}, c.parsed_data -> 'parsing' AS parsing {_CV_FROM} WHERE c.id = :cid",
        {"cid": cv_id},
    )
    if not rows:
        return None
    events = await _mappings(
        db,
        f"""
        SELECT * FROM (
            SELECT id, cv_id, attempt_id, user_id, stage, event, status, duration_ms, payload, created_at
            FROM cv_pipeline_events WHERE cv_id = :cid
            ORDER BY created_at DESC, id DESC LIMIT {_EVENT_LIMIT * 4}
        ) recent ORDER BY created_at, id
        """,
        {"cid": cv_id},
    )
    calls = await _mappings(
        db,
        f"""
        SELECT * FROM (
            SELECT * FROM ai_call_logs WHERE cv_id = :cid
            ORDER BY created_at DESC, id DESC LIMIT {_AI_CALL_LIMIT}
        ) recent ORDER BY created_at, id
        """,
        {"cid": cv_id},
    )
    return assemble_cv_detail(rows[0], events, calls)
