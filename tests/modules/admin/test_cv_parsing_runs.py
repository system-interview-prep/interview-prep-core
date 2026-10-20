"""Admin CV-parsing runs: pure step assembly, run status and the two endpoint contracts."""

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError

from src.modules.admin import cv_parsing_runs as cr

admin_router = importlib.import_module("src.modules.admin.router")

CV = "0b5f1c9e-2a43-4d1e-8f55-6c7d8e9f0a1b"
T0 = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=2)
UPLOAD = "upload-attempt"
A1 = "attempt-1"
A2 = "attempt-2"

_counter = iter(range(1, 10_000))


def _ev(seconds, stage, event, status, payload=None, duration_ms=None, attempt=A1):
    return {
        "id": next(_counter),
        "cv_id": CV,
        "attempt_id": attempt,
        "user_id": "user-1",
        "stage": stage,
        "event": event,
        "status": status,
        "duration_ms": duration_ms,
        "payload": {**(payload or {}), **({"duration_ms": duration_ms} if duration_ms is not None else {})},
        "created_at": T0 + timedelta(seconds=seconds),
    }


QUALITY = {
    "status": "ready",
    "parser_version": "hybrid-resume-v3",
    "extraction_version": "pdf-text-v1",
    "source_characters": 4200,
    "section_counts": {"skills": 12, "employment": 2, "education": 1, "evidence": 30},
    "warnings_count": 1,
    "checks": {"source_text_present": True, "evidence_present": True, "parser_warnings_empty": False},
    "coverage": "complete",
    "raw_text_coverage": "complete",
    "canonical_section_coverage": {"skills": "complete", "projects": "complete_empty"},
    "evidence_index_coverage": "complete",
    "coverage_reason_codes": [],
    "warnings": [{"code": "llm_claim_rejected", "severity": "warning", "section": "projects"}],
}


def _upload_events():
    return [
        _ev(
            0,
            "upload",
            "cv_uploaded",
            "completed",
            {"extension": "pdf", "content_type": "application/pdf", "size_bytes": 182000, "duplicate": False},
            duration_ms=120,
            attempt=UPLOAD,
        ),
        _ev(0.01, "queue", "cv_enqueued", "info", {"reparse": False}, attempt=UPLOAD),
    ]


def _attempt(start, attempt=A1, *, quality=None, provider_fallback=False, attempt_no=1):
    events = [
        _ev(
            start,
            "queue",
            "cv_parse_claimed",
            "started",
            {"attempt": attempt_no, "parser_mode": "hybrid"},
            attempt=attempt,
        ),
        _ev(start + 0.01, "extract", "extraction_started", "started", {"bytes": 182000}, attempt=attempt),
    ]
    if provider_fallback:
        events += [
            _ev(
                start + 0.2,
                "extract",
                "provider_skipped",
                "info",
                {"provider": "docx_text", "error_type": "ExtractorNotApplicable"},
                attempt=attempt,
            ),
            _ev(
                start + 0.5,
                "extract",
                "provider_failed",
                "warning",
                {"provider": "mineru", "error_type": "TimeoutError", "next_provider": "paddleocr"},
                attempt=attempt,
            ),
            _ev(
                start + 0.9,
                "extract",
                "provider_fallback_used",
                "warning",
                {"provider": "paddleocr", "failed": ["docx_text", "mineru"]},
                attempt=attempt,
            ),
        ]
    provider = "paddleocr" if provider_fallback else "pdf_text"
    events += [
        _ev(
            start + 1.0,
            "extract",
            "extraction_completed",
            "completed",
            {
                "provider": provider,
                "extractor_version": "v1",
                "markdown_characters": 5000,
                "content_blocks": 80,
            },
            duration_ms=990,
            attempt=attempt,
        ),
        _ev(
            start + 1.05,
            "source",
            "source_built",
            "completed",
            {"source_characters": 4200, "block_count": 80, "source_section_block_counts": {"skills": 3}},
            duration_ms=40,
            attempt=attempt,
        ),
        _ev(
            start + 1.06,
            "parse",
            "parser_started",
            "started",
            {"extractor_version": "v1", "source_characters": 4200},
            attempt=attempt,
        ),
        _ev(
            start + 3.0,
            "parse",
            "parser_completed",
            "completed",
            {
                "parser_version": "hybrid-resume-v3",
                "parser_mode": "hybrid",
                "skills_count": 12,
                "evidence_count": 30,
                "llm_used": True,
            },
            duration_ms=1940,
            attempt=attempt,
        ),
        _ev(
            start + 3.01,
            "quality",
            "quality_gate",
            "completed" if (quality or QUALITY)["status"] == "ready" else "warning",
            quality or QUALITY,
            attempt=attempt,
        ),
        _ev(start + 3.05, "save", "cv_saved", "completed", {}, duration_ms=30, attempt=attempt),
        _ev(
            start + 3.06,
            "done",
            "completed",
            "completed",
            {"canonical_status": (quality or QUALITY)["status"], "parser_version": "hybrid-resume-v3"},
            duration_ms=3060,
            attempt=attempt,
        ),
    ]
    return events


def _row(status="DONE", canonical="ready", updated=None, **extra):
    return {
        "cv_id": CV,
        "user_id": "user-1",
        "user_name": "Nguyễn An",
        "user_email": "an@example.com",
        "filename": "nguyen-an-cv.pdf",
        "size": 182000,
        "content_type": "application/pdf",
        "db_status": status,
        "canonical_status": canonical if status == "DONE" else None,
        "parse_source": "pdf_text+hybrid-resume-v3" if status == "DONE" else None,
        "created_at": T0,
        "updated_at": updated or (T0 + timedelta(seconds=5)),
        "stored_parser_version": "hybrid-resume-v3" if status == "DONE" else None,
        "stored_warnings_count": 1 if status == "DONE" else None,
        "stored_section_counts": {"skills": 12, "employment": 2} if status == "DONE" else None,
        **extra,
    }


def _by_key(steps):
    return {s["key"]: s for s in steps}


def _statuses(steps):
    return [(s["key"], s["status"]) for s in steps]


# --------------------------------------------------------------------------- status


@pytest.mark.parametrize(
    ("db_status", "canonical", "age_minutes", "expected"),
    [
        ("DONE", "ready", 60, ("completed", False)),
        ("DONE", "review_required", 60, ("review", False)),
        ("FAILED", None, 0, ("failed", False)),
        ("PENDING", None, 1, ("running", False)),
        ("PARSING", None, 14, ("running", False)),
        ("PARSING", None, 16, ("failed", True)),
        ("PENDING", None, 30, ("failed", True)),
    ],
)
def test_cv_status(db_status, canonical, age_minutes, expected):
    assert cr.cv_status(db_status, canonical, NOW - timedelta(minutes=age_minutes), NOW) == expected


# --------------------------------------------------------------------------- steps


def test_completed_parse_has_every_step_done_in_order():
    events = _upload_events() + _attempt(0.5)
    steps = cr.build_steps(events, db_status="DONE", canonical_status="ready", updated_at=T0, now=NOW)
    assert [s["key"] for s in steps] == list(cr.STEPS)
    assert all(s["status"] == "done" for s in steps)
    by_key = _by_key(steps)
    assert set(steps[0]) == {"key", "status", "startedAt", "durationMs", "summary"}
    assert by_key["upload"]["summary"] == {
        "extension": "pdf",
        "content_type": "application/pdf",
        "size_bytes": 182000,
        "duplicate": False,
    }
    assert by_key["upload"]["durationMs"] == 120
    # Queue time: from the enqueue to the worker claiming the CV.
    assert by_key["queue"]["durationMs"] == 490
    assert by_key["queue"]["summary"] == {"attempt": 1, "parser_mode": "hybrid"}
    assert by_key["extract"]["durationMs"] == 990
    assert by_key["extract"]["summary"]["provider"] == "pdf_text"
    assert "source_section_block_counts" not in by_key["source"]["summary"]
    assert by_key["parse"]["summary"]["llm_used"] is True
    assert "warnings" not in by_key["quality"]["summary"] and "checks" not in by_key["quality"]["summary"]
    assert by_key["done"]["durationMs"] == 3060


def test_review_required_quality_gate_is_a_warning():
    quality = {**QUALITY, "status": "review_required"}
    events = _upload_events() + _attempt(0.5, quality=quality)
    steps = _by_key(cr.build_steps(events, db_status="DONE", canonical_status="review_required", now=NOW))
    assert steps["quality"]["status"] == "done" and steps["quality"]["summary"]["warning"] is True


def test_failure_marks_the_failed_step_and_the_rest_not_reached():
    events = _upload_events() + [
        _ev(0.5, "queue", "cv_parse_claimed", "started", {"attempt": 4}),
        _ev(0.51, "extract", "extraction_started", "started"),
        _ev(1.0, "extract", "extraction_completed", "completed", {"provider": "mineru"}, duration_ms=490),
        _ev(1.05, "source", "source_built", "completed", duration_ms=10),
        _ev(1.06, "parse", "parser_started", "started"),
        _ev(
            2.0,
            "done",
            "failed",
            "error",
            {"error_type": "ValidationError", "failed_stage": "parse", "reason": "invalid_parser_output"},
            duration_ms=1500,
        ),
    ]
    steps = cr.build_steps(events, db_status="FAILED", updated_at=T0 + timedelta(seconds=2), now=NOW)
    assert _statuses(steps) == [
        ("upload", "done"),
        ("queue", "done"),
        ("extract", "done"),
        ("source", "done"),
        ("parse", "error"),
        ("quality", "skipped"),
        ("save", "skipped"),
        ("done", "error"),
    ]
    by_key = _by_key(steps)
    assert by_key["quality"]["summary"] == {"reason": "not_reached"}
    assert by_key["done"]["summary"]["reason"] == "invalid_parser_output"


def test_failure_before_any_stage_event_marks_that_step():
    events = _upload_events() + [
        _ev(0.5, "queue", "cv_parse_claimed", "started"),
        _ev(
            0.6,
            "done",
            "failed",
            "error",
            {"error_type": "ClientError", "failed_stage": "extract", "reason": "storage_error"},
        ),
    ]
    by_key = _by_key(cr.build_steps(events, db_status="FAILED", now=NOW))
    assert by_key["extract"]["status"] == "error" and by_key["extract"]["summary"] == {
        "reason": "storage_error"
    }
    assert by_key["source"]["status"] == "skipped"


def test_running_parse_marks_the_current_step_running_and_the_rest_pending():
    events = _upload_events() + _attempt(0.5)[:2]  # claimed + extraction_started
    steps = cr.build_steps(events, db_status="PARSING", updated_at=NOW - timedelta(seconds=30), now=NOW)
    assert _statuses(steps) == [
        ("upload", "done"),
        ("queue", "done"),
        ("extract", "running"),
        ("source", "pending"),
        ("parse", "pending"),
        ("quality", "pending"),
        ("save", "pending"),
        ("done", "pending"),
    ]


def test_waiting_in_the_queue_is_running():
    steps = cr.build_steps(_upload_events(), db_status="PENDING", updated_at=T0, now=NOW)
    assert _statuses(steps)[:3] == [("upload", "done"), ("queue", "running"), ("extract", "pending")]


def test_stalled_parse_is_an_error_at_the_stuck_step():
    events = _upload_events() + _attempt(0.5)[:2]
    now = T0 + timedelta(minutes=20)
    steps = cr.build_steps(events, db_status="PARSING", updated_at=T0 + timedelta(seconds=1), now=now)
    by_key = _by_key(steps)
    assert by_key["extract"]["status"] == "error" and by_key["extract"]["summary"]["reason"] == "stalled"
    assert by_key["source"]["status"] == "skipped" and by_key["source"]["summary"] == {
        "reason": "not_reached"
    }
    assert by_key["done"]["status"] == "error" and by_key["done"]["summary"] == {"reason": "stalled"}


def test_retry_then_success_shows_the_latest_attempt():
    retry = [
        _ev(0.5, "queue", "cv_parse_claimed", "started", {"attempt": 1}),
        _ev(0.51, "extract", "extraction_started", "started"),
        _ev(
            30.5,
            "extract",
            "provider_failed",
            "warning",
            {"provider": "mineru", "error_type": "TimeoutError"},
        ),
        _ev(
            30.6,
            "done",
            "retry_scheduled",
            "warning",
            {"error_type": "TimeoutError", "failed_stage": "extract", "reason": "timeout"},
        ),
    ]
    waiting = cr.build_steps(
        _upload_events() + retry,
        db_status="PENDING",
        updated_at=T0 + timedelta(seconds=31),
        now=T0 + timedelta(seconds=40),
    )
    by_key = _by_key(waiting)
    assert by_key["extract"]["status"] == "error"
    assert by_key["done"]["status"] == "running" and by_key["done"]["summary"]["reason"] == "timeout"
    assert by_key["source"]["status"] == "pending"

    events = _upload_events() + retry + _attempt(60, A2, attempt_no=2)
    steps = cr.build_steps(events, db_status="DONE", canonical_status="ready", now=NOW)
    assert all(s["status"] == "done" for s in steps)
    assert _by_key(steps)["queue"]["summary"]["attempt"] == 2
    run = cr.run_view(_row(), cr.summarize_events(CV, events), now=NOW)
    assert run["attempts"] == 2 and "retry" in run["warnings"]


def test_dispatch_failure_marks_the_queue_step():
    events = [
        _upload_events()[0],
        _ev(
            0.02,
            "upload",
            "cv_dispatch_failed",
            "error",
            {"error_type": "OperationalError", "reason": "dispatch_failed"},
            attempt=UPLOAD,
        ),
    ]
    steps = cr.build_steps(events, db_status="FAILED", now=NOW)
    assert _statuses(steps) == [
        ("upload", "done"),
        ("queue", "error"),
        *((key, "skipped") for key in cr.STEPS[2:-1]),
        ("done", "error"),
    ]
    assert _by_key(steps)["queue"]["summary"] == {
        "reason": "dispatch_failed",
        "error_type": "OperationalError",
    }
    run = cr.run_view(_row("FAILED"), cr.summarize_events(CV, events), now=NOW)
    assert run["currentStep"] == "queue" and run["warnings"] == ["dispatch_failed"]


def test_reparse_waiting_for_a_worker_restarts_at_the_queue():
    events = (
        _upload_events()
        + _attempt(0.5)
        + [_ev(100, "queue", "cv_enqueued", "info", {"reparse": True}, attempt="reparse")]
    )
    steps = cr.build_steps(
        events, db_status="PENDING", updated_at=T0 + timedelta(seconds=100), now=T0 + timedelta(seconds=110)
    )
    assert _statuses(steps)[:3] == [("upload", "done"), ("queue", "running"), ("extract", "pending")]
    run = cr.run_view(
        _row("PENDING", updated=T0 + timedelta(seconds=100)),
        cr.summarize_events(CV, events),
        now=T0 + timedelta(seconds=110),
    )
    assert run["status"] == "running" and run["currentStep"] == "queue" and run["finishedAt"] is None


@pytest.mark.parametrize(
    ("db_status", "updated_minutes_ago", "expected"),
    [
        ("DONE", 60, [(key, "done") for key in cr.STEPS]),
        ("FAILED", 60, [("upload", "done"), *((k, "skipped") for k in cr.STEPS[1:-1]), ("done", "error")]),
        ("PENDING", 1, [("upload", "done"), ("queue", "running"), *((k, "pending") for k in cr.STEPS[2:])]),
        (
            "PARSING",
            1,
            [
                ("upload", "done"),
                ("queue", "done"),
                ("extract", "running"),
                *((k, "pending") for k in cr.STEPS[3:]),
            ],
        ),
        (
            "PARSING",
            20,
            [
                ("upload", "done"),
                ("queue", "done"),
                ("extract", "error"),
                *((k, "skipped") for k in cr.STEPS[3:-1]),
                ("done", "error"),
            ],
        ),
    ],
)
def test_untracked_cvs_infer_steps_from_the_row(db_status, updated_minutes_ago, expected):
    steps = cr.build_steps(
        [], db_status=db_status, updated_at=NOW - timedelta(minutes=updated_minutes_ago), now=NOW
    )
    assert _statuses(steps) == expected
    assert _by_key(steps)["upload"]["summary"] == {"reason": "untracked"}


def test_duplicate_upload_of_an_untracked_cv_stays_untracked():
    events = [_ev(5, "upload", "cv_upload_duplicate", "info", {"duplicate": True}, attempt="dup")]
    steps = cr.build_steps(events, db_status="DONE", canonical_status="ready", now=NOW)
    assert all(s["status"] == "done" for s in steps)
    assert steps[0]["summary"] == {"reason": "duplicate", "duplicates": 1}
    assert steps[1]["summary"] == {"reason": "untracked"}
    assert cr.run_view(_row(), cr.summarize_events(CV, events), now=NOW)["tracked"] is False


# --------------------------------------------------------------------------- run view


def test_run_view_of_a_completed_cv():
    events = _upload_events() + _attempt(0.5, provider_fallback=True)
    run = cr.run_view(_row(), cr.summarize_events(CV, events), ai={"calls": 3, "errors": 1}, now=NOW)
    assert run == {
        "cvId": CV,
        "tracked": True,
        "status": "completed",
        "dbStatus": "DONE",
        "canonicalStatus": "ready",
        "uploadedAt": "2026-10-20T09:00:00+00:00",
        "startedAt": "2026-10-20T09:00:00.500000+00:00",
        "finishedAt": "2026-10-20T09:00:03.560000+00:00",
        "durationMs": 3560,
        "attempts": 1,
        "user": {"id": "user-1", "name": "Nguyễn An", "email": "an@example.com"},
        "cv": {
            "id": CV,
            "fileName": "nguyen-an-cv.pdf",
            "sizeBytes": 182000,
            "contentType": "application/pdf",
        },
        "currentStep": "done",
        "provider": "paddleocr",
        "parserVersion": "hybrid-resume-v3",
        "sectionCounts": {"skills": 12, "employment": 2, "education": 1, "evidence": 30},
        "warningsCount": 1,
        "aiCalls": 3,
        "aiErrors": 1,
        "warnings": ["provider_fallback"],
    }


def test_run_view_of_an_untracked_cv_uses_the_stored_parse():
    run = cr.run_view(_row(canonical="review_required"), None, now=NOW)
    assert run["tracked"] is False and run["status"] == "review"
    assert run["startedAt"] is None and run["durationMs"] is None
    assert run["finishedAt"] == "2026-10-20T09:00:05+00:00"
    assert run["provider"] == "pdf_text" and run["parserVersion"] == "hybrid-resume-v3"
    assert run["sectionCounts"] == {"skills": 12, "employment": 2} and run["warningsCount"] == 1
    assert run["currentStep"] == "done" and run["warnings"] == ["review_required"]


def test_run_view_of_a_stalled_cv():
    run = cr.run_view(_row("PARSING", updated=NOW - timedelta(minutes=30)), None, now=NOW)
    assert run["status"] == "failed" and run["warnings"] == ["stalled"]
    assert run["finishedAt"] is None and run["currentStep"] == "extract"


def test_summary_mirrors_the_sql_aggregate_keys():
    summary = cr.summarize_events(CV, _upload_events() + _attempt(0.5))
    for column in (
        "event_count",
        "started_at",
        "last_at",
        "current_stage",
        "last_event",
        "done_event",
        "done_at",
        "done_payload",
        "uploaded_event_at",
        "quality_payload",
        "provider",
        "parser_version",
        "attempts",
        "flag_events",
        "reparsed",
    ):
        assert column in summary
        assert f" AS {column}" in cr._EVENTS_AGG
    assert summary["current_stage"] == "done" and summary["reparsed"] is False


# --------------------------------------------------------------------------- sections


def test_detail_sections_from_events():
    events = _upload_events() + _attempt(0.5, provider_fallback=True)
    detail = cr.assemble_cv_detail(_row(parsing={"status": "ready"}), events, [], now=NOW)
    assert detail["extraction"] == {
        "provider": "paddleocr",
        "extractorVersion": "v1",
        "markdownCharacters": 5000,
        "contentBlocks": 80,
        "attempts": [
            {"provider": "docx_text", "outcome": "skipped", "errorType": "ExtractorNotApplicable"},
            {"provider": "mineru", "outcome": "failed", "errorType": "TimeoutError"},
            {"provider": "paddleocr", "outcome": "used", "errorType": None},
        ],
    }
    assert detail["quality"] == {
        "status": "ready",
        "parserVersion": "hybrid-resume-v3",
        "sourceCharacters": 4200,
        "sectionCounts": {"skills": 12, "employment": 2, "education": 1, "evidence": 30},
        "coverage": {
            "rawText": "complete",
            "evidence": "complete",
            "sections": {"skills": "complete", "projects": "complete_empty"},
        },
        "reasonCodes": [],
        "checks": {"source_text_present": True, "evidence_present": True, "parser_warnings_empty": False},
        "warnings": [{"code": "llm_claim_rejected", "severity": "warning", "section": "projects"}],
    }
    assert set(detail["events"][0]) == {
        "at",
        "stage",
        "event",
        "status",
        "durationMs",
        "attemptId",
        "payload",
    }
    assert detail["events"][0]["attemptId"] == UPLOAD


def test_untracked_detail_derives_quality_from_the_stored_parsing():
    parsing = {
        "status": "review_required",
        "parserVersion": "deterministic-resume-v6",
        "extractionVersion": "mineru-2",
        "rawTextCoverage": "complete",
        "evidenceIndexCoverage": "partial",
        "canonicalSectionCoverage": {"employment": "partial"},
        "coverageReasonCodes": ["evidence_index_incomplete"],
        "warnings": [
            {"code": "NO_SKILLS_DETECTED", "path": "skills", "severity": "warning", "message": "secret"}
        ],
    }
    detail = cr.assemble_cv_detail(_row(canonical="review_required", parsing=parsing), [], [], now=NOW)
    assert detail["run"]["tracked"] is False
    assert detail["extraction"] == {
        "provider": "pdf_text",
        "extractorVersion": "mineru-2",
        "markdownCharacters": None,
        "contentBlocks": None,
        "attempts": [],
    }
    assert detail["quality"]["status"] == "review_required"
    assert detail["quality"]["coverage"] == {
        "rawText": "complete",
        "evidence": "partial",
        "sections": {"employment": "partial"},
    }
    assert detail["quality"]["reasonCodes"] == ["evidence_index_incomplete"]
    assert detail["quality"]["warnings"] == [
        {"code": "NO_SKILLS_DETECTED", "severity": "warning", "section": "skills"}
    ]
    assert detail["quality"]["sectionCounts"] == {"skills": 12, "employment": 2}


def test_failed_untracked_detail_has_no_sections():
    detail = cr.assemble_cv_detail(_row("FAILED"), [], [], now=NOW)
    assert detail["extraction"] is None and detail["quality"] is None
    assert detail["steps"][-1] == {
        "key": "done",
        "status": "error",
        "startedAt": None,
        "durationMs": None,
        "summary": {"reason": "untracked"},
    }


# --------------------------------------------------------------------------- endpoints


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return SimpleNamespace(all=lambda: self.rows)


class _Db:
    """Answers each query by the table it reads."""

    def __init__(self, *, cvs=None, events=None, calls=None, missing=False):
        self.cvs = cvs or []
        self.events = events or []
        self.calls = calls or []
        self.missing = missing
        self.statements: list[tuple[str, dict]] = []
        self.rolled_back = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params or {}))
        if self.missing and ("cv_pipeline_events" in sql or "ai_call_logs" in sql):
            raise ProgrammingError(sql, params, Exception('relation "cv_pipeline_events" does not exist'))
        if "SELECT COUNT(*) AS total FROM user_cvs" in sql:
            return _Result([{"total": len(self.cvs)}])
        if "WHERE c.id = :cid" in sql:
            return _Result([r for r in self.cvs if r["cv_id"] == params["cid"]])
        if "FROM user_cvs c" in sql:
            return _Result(self.cvs)
        if "GROUP BY cv_id" in sql and "cv_pipeline_events" in sql:
            ids = set(params["ids"])
            rows = [cr.summarize_events(CV, self.events)] if CV in ids and self.events else []
            return _Result(rows)
        if "FROM cv_pipeline_events WHERE cv_id = :cid" in sql:
            return _Result([e for e in self.events if e["cv_id"] == params["cid"]])
        if "GROUP BY cv_id" in sql and "ai_call_logs" in sql:
            return _Result([{"cv_id": CV, "calls": 2, "errors": 0}] if CV in params["ids"] else [])
        if "FROM ai_call_logs WHERE cv_id = :cid" in sql:
            return _Result(self.calls)
        raise AssertionError(sql)

    async def rollback(self):
        self.rolled_back = True


_CALLS = [
    {
        "id": 200,
        "session_id": None,
        "cv_id": CV,
        "purpose": "user_cvs.hybrid.HybridResumeParser.parse",
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "status": "ok",
        "duration_ms": 1800,
        "input_tokens": 3000,
        "output_tokens": 900,
        "input_chars": 4200,
        "output_chars": 3100,
        "error_type": None,
        "error_message": None,
        "input_preview": None,
        "output_preview": None,
        "created_at": T0 + timedelta(seconds=2),
    }
]


async def test_list_endpoint_contract():
    db = _Db(cvs=[_row()], events=_upload_events() + _attempt(0.5))
    body = await admin_router.list_admin_cv_parsing_runs(
        limit=20, offset=0, status="review", days=7, q=" An ", _={"sub": "admin"}, db=db
    )
    assert body["total"] == 1
    (item,) = body["items"]
    assert item["cvId"] == CV and item["tracked"] is True and item["status"] == "completed"
    assert item["aiCalls"] == 2 and item["aiErrors"] == 0
    page_sql, page_params = db.statements[0]
    assert "c.parsed_data -> 'parsing' ->> 'status' = 'review_required'" in page_sql
    assert "LOWER(COALESCE(u.name" in page_sql and "LOWER(COALESCE(c.filename" in page_sql
    assert "ORDER BY c.created_at DESC" in page_sql
    assert page_params == {"days": 7, "q": "%an%", "limit": 20, "offset": 0}
    # Events are aggregated for the page only, in one query.
    agg = [p for sql, p in db.statements if "FROM cv_pipeline_events" in sql]
    assert agg == [{"ids": [CV]}]


async def test_list_endpoint_includes_untracked_cvs():
    db = _Db(cvs=[_row("PENDING", updated=datetime.now(UTC))])
    body = await admin_router.list_admin_cv_parsing_runs(
        limit=20, offset=0, status="all", days=7, q=None, _={"sub": "admin"}, db=db
    )
    (item,) = body["items"]
    assert item["tracked"] is False and item["status"] == "running" and item["currentStep"] == "queue"
    assert "LIKE :q" not in db.statements[0][0]


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        ("running", "c.status IN ('PENDING', 'PARSING') AND NOT"),
        ("failed", "c.status = 'FAILED' OR"),
        ("completed", "<> 'review_required'"),
    ],
)
def test_status_filters(status, fragment):
    page, count, _ = cr.cv_runs_query(status=status, q=None)
    assert fragment in page and fragment in count


async def test_detail_endpoint_contract():
    db = _Db(cvs=[_row(parsing={"status": "ready"})], events=_upload_events() + _attempt(0.5), calls=_CALLS)
    body = await admin_router.get_admin_cv_parsing_run(CV, _={"sub": "admin"}, db=db)
    assert set(body) == {"run", "steps", "extraction", "quality", "events", "ai"}
    assert [s["key"] for s in body["steps"]] == list(cr.STEPS)
    assert body["run"]["aiCalls"] == 1
    assert body["ai"]["summary"]["total"] == 1
    assert body["ai"]["calls"][0]["purpose"] == "user_cvs.hybrid.HybridResumeParser.parse"
    assert len(body["events"]) == len(_upload_events() + _attempt(0.5))


async def test_detail_endpoint_404_for_unknown_cv():
    with pytest.raises(HTTPException) as caught:
        await admin_router.get_admin_cv_parsing_run("nope", _={"sub": "admin"}, db=_Db())
    assert caught.value.status_code == 404


@pytest.mark.parametrize("call", ["list", "detail"])
async def test_endpoints_are_503_before_the_migration(call):
    db = _Db(cvs=[_row()], missing=True)
    with pytest.raises(HTTPException) as caught:
        if call == "list":
            await admin_router.list_admin_cv_parsing_runs(
                limit=20, offset=0, status="all", days=7, q=None, _={"sub": "admin"}, db=db
            )
        else:
            await admin_router.get_admin_cv_parsing_run(CV, _={"sub": "admin"}, db=db)
    assert caught.value.status_code == 503 and caught.value.detail == cr.NOT_MIGRATED
    assert db.rolled_back


def test_cv_parsing_admin_routes_require_admin(client):
    assert client.get("/admin/cv-parsing/runs").status_code == 401
    assert client.get(f"/admin/cv-parsing/runs/{CV}").status_code == 401
