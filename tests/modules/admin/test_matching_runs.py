"""Admin matching runs: pure step assembly and the two endpoint contracts."""

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError

from src.modules.admin import matching_runs as mr

admin_router = importlib.import_module("src.modules.admin.router")

RUN = "4f7d0c1e-9a51-4c43-9d55-3f1f0b1c2d3e"
T0 = datetime(2026, 10, 19, 9, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=2)


def _ev(seconds: float, stage: str, event: str, status: str, payload=None, duration_ms=None, flow="match"):
    return {
        "id": int(seconds * 1000),
        "run_id": RUN,
        "flow": flow,
        "cache_key": "c" * 64,
        "user_id": "user-1",
        "resume_id": "cv-1",
        "job_id": "job-1",
        "stage": stage,
        "event": event,
        "status": status,
        "duration_ms": duration_ms,
        "payload": payload or {},
        "created_at": T0 + timedelta(seconds=seconds),
    }


SCORING = {
    "eligibility": "eligible",
    "compatibility_status": "compatible",
    "suitability_score": 0.7342,
    "diagnostic_score": 0.7342,
    "fit_band": "partial_fit",
    "decision": "assessed",
    "provenance_mode": "requirement_aware",
    "scored_count": 2,
    "unknown_count": 1,
    "total_count": 3,
    "failed_must_have_count": 0,
    "factors": {
        "requirement_coverage": {"score": 0.6667, "weight": 0.6},
        "semantic": {"score": 0.81, "weight": 0.25},
    },
    "warnings": ["semantic_dense_provider_fallback_to_sparse"],
}


def _request_events(flow="match"):
    return [
        _ev(
            0,
            "request",
            "match_requested",
            "started",
            {
                "endpoint": "match-ids",
                "policy_version": "balanced-v1",
                "semantic_mode": "hybrid",
                "bm25_provider_mode": "auto",
                "async": False,
            },
            flow=flow,
        ),
        _ev(
            0.05,
            "inputs",
            "inputs_resolved",
            "completed",
            {
                "jd_path": "canonical",
                "requirement_count": 3,
                "requirements_by_priority": {"must_have": 2, "nice_to_have": 1},
                "cv_parser_status": "ready",
                "has_raw_text": True,
                "cv_evidence_count": 14,
            },
            duration_ms=40,
            flow=flow,
        ),
    ]


def _scoring_events(offset=0.0, flow="match"):
    return [
        _ev(
            offset + 0.4,
            "reparse",
            "targeted_reparse_completed",
            "completed",
            {
                "alias_recovered_count": 1,
                "llm_requested_count": 1,
                "llm_recovered_count": 0,
                "llm_search_status": "no_verified_quotes",
            },
            duration_ms=300,
            flow=flow,
        ),
        _ev(
            offset + 0.45,
            "requirements",
            "requirements_evaluated",
            "completed",
            {
                "total": 3,
                "counts_by_status": {"met": 2, "unknown": 1},
                "counts_by_evaluator": {"knowledge_skill": 2, "experience_duration": 1},
                "must_have_unknown": 1,
                "must_have_not_met": 0,
                "overrides_applied": 0,
                "items": [
                    {
                        "requirement_id": "req-java",
                        "label": "Java",
                        "priority": "must_have",
                        "status": "met",
                        "evaluator": "knowledge_skill",
                        "reason_code": "skill_claim_verified",
                        "confidence": 0.95,
                    },
                    {
                        "requirement_id": "req-exp",
                        "label": "3+ years backend experience",
                        "priority": "must_have",
                        "status": "unknown",
                        "evaluator": "experience_duration",
                        "reason_code": "experience_duration_not_evidenced",
                        "confidence": 0.45,
                    },
                ],
            },
            duration_ms=35,
            flow=flow,
        ),
        _ev(
            offset + 0.9,
            "semantic",
            "semantic_scored",
            "completed",
            {
                "mode": "hybrid",
                "bm25_provider_used": "paradedb",
                "sparse_score": 0.62,
                "dense_score": None,
                "embedding_outcome": "error",
                "warning_code": "semantic_dense_provider_fallback_to_sparse",
            },
            duration_ms=420,
            flow=flow,
        ),
        _ev(offset + 0.91, "scoring", "match_scored", "completed", SCORING, flow=flow),
    ]


def _normal_run():
    return [
        *_request_events(),
        _ev(0.1, "cache", "cache_lookup_completed", "info", {"hit": False, "age_s": None}, duration_ms=4),
        *_scoring_events(),
        _ev(0.95, "cache_write", "cache_write_completed", "completed", {"outcome": "stored"}),
        _ev(
            0.96,
            "done",
            "match_completed",
            "completed",
            {
                "decision": "assessed",
                "fit_band": "partial_fit",
                "suitability_score": 0.7342,
                "cache_hit": False,
            },
            duration_ms=960,
        ),
    ]


def _by_key(steps):
    return {s["key"]: s for s in steps}


def test_normal_run_steps_are_all_done_in_order():
    steps = mr.build_steps("match", _normal_run(), now=NOW)
    assert [s["key"] for s in steps] == list(mr.BASE_STEPS)
    assert {s["status"] for s in steps} == {"done"}
    by_key = _by_key(steps)
    # Events are emitted on completion: the step started duration_ms earlier.
    assert by_key["reparse"]["startedAt"] == (T0 + timedelta(seconds=0.1)).isoformat()
    assert by_key["reparse"]["durationMs"] == 300
    assert by_key["requirements"]["summary"]["counts_by_status"] == {"met": 2, "unknown": 1}
    assert "items" not in by_key["requirements"]["summary"]
    assert "factors" not in by_key["scoring"]["summary"]
    assert by_key["request"]["summary"]["endpoint"] == "match-ids"


def test_cache_hit_skips_the_computation_and_keeps_the_cached_scoring():
    events = [
        *_request_events(),
        _ev(0.1, "cache", "cache_lookup_completed", "info", {"hit": True, "age_s": 3600}, duration_ms=3),
        _ev(
            0.11,
            "done",
            "match_completed",
            "completed",
            {
                "decision": "assessed",
                "fit_band": "partial_fit",
                "suitability_score": 0.7342,
                "cache_hit": True,
                "scoring": SCORING,
            },
            duration_ms=110,
        ),
    ]
    steps = _by_key(mr.build_steps("match", events, now=NOW))
    for key in ("reparse", "requirements", "semantic", "scoring", "cache_write"):
        assert steps[key]["status"] == "skipped", key
    assert steps["scoring"]["summary"]["reason"] == "cache_hit"
    assert steps["scoring"]["summary"]["decision"] == "assessed"
    assert steps["cache"]["status"] == "done" and steps["done"]["status"] == "done"
    detail = mr.assemble_run_detail(RUN, events, [], now=NOW)
    assert detail["run"]["cacheHit"] is True and detail["run"]["status"] == "completed"
    assert detail["scoring"]["fitBand"] == "partial_fit" and len(detail["scoring"]["factors"]) == 2
    assert detail["requirements"] is None


def test_failure_at_inputs_marks_the_rest_not_reached():
    events = [
        _request_events()[0],
        _ev(
            0.05,
            "inputs",
            "inputs_failed",
            "error",
            {"http_status": 404, "reason": "resume_not_found", "error_type": "HTTPException"},
            duration_ms=12,
        ),
        _ev(0.06, "done", "match_failed", "error", {"error_type": "inputs_failed", "http_status": 404}, 60),
    ]
    steps = _by_key(mr.build_steps("match", events, now=NOW))
    assert steps["request"]["status"] == "done"
    assert steps["inputs"]["status"] == "error"
    assert steps["inputs"]["summary"]["reason"] == "resume_not_found"
    for key in ("cache", "reparse", "requirements", "semantic", "scoring", "cache_write"):
        assert steps[key]["status"] == "skipped" and steps[key]["summary"] == {"reason": "not_reached"}
    assert steps["done"]["status"] == "error"
    run = mr.run_view(mr.summarize_run(RUN, events), now=NOW)
    assert run["status"] == "failed" and run["warnings"] == ["inputs_failed"]
    assert run["finishedAt"] == (T0 + timedelta(seconds=0.06)).isoformat() and run["durationMs"] == 60


def test_running_run_marks_the_next_step_running():
    events = [*_request_events(), _ev(0.1, "cache", "cache_lookup_completed", "info", {"hit": False})]
    steps = _by_key(mr.build_steps("match", events, now=T0 + timedelta(seconds=5)))
    assert steps["reparse"]["status"] == "running"
    assert [steps[k]["status"] for k in ("requirements", "semantic", "scoring", "cache_write", "done")] == [
        "pending"
    ] * 5
    run = mr.run_view(mr.summarize_run(RUN, events), now=T0 + timedelta(seconds=5))
    assert run["status"] == "running" and run["finishedAt"] is None and run["currentStep"] == "cache"


def test_stalled_run_counts_as_failed():
    events = [
        *_request_events(),
        _ev(0.1, "cache", "cache_lookup_completed", "info", {"hit": False}),
        *_scoring_events()[:2],
    ]
    later = T0 + timedelta(minutes=11)
    steps = _by_key(mr.build_steps("match", events, now=later))
    assert steps["semantic"]["status"] == "error" and steps["semantic"]["summary"] == {"reason": "stalled"}
    assert steps["scoring"]["status"] == "skipped" and steps["done"]["status"] == "error"
    run = mr.run_view(mr.summarize_run(RUN, events), now=later)
    assert run["status"] == "failed" and "stalled" in run["warnings"]
    assert run["finishedAt"] is None and run["durationMs"] == 450
    # Not yet stalled at 9 minutes.
    assert mr.run_view(mr.summarize_run(RUN, events), now=T0 + timedelta(minutes=9))["status"] == "running"


def test_clarification_rescore_flow():
    flow = "rescore"
    passes = [*_scoring_events(0, flow), *_scoring_events(1, flow), *_scoring_events(2, flow)]
    events = [
        *_request_events(flow),
        *passes,
        _ev(
            3.5,
            "rescore",
            "rescore_completed",
            "completed",
            {
                "answers_count": 1,
                "transitions": {"unknown->met": 1},
                "suitability_before": 0.61,
                "suitability_after": 0.74,
                "fit_band_before": "partial_fit",
                "fit_band_after": "partial_fit",
                "passes": 3,
            },
            duration_ms=3400,
            flow=flow,
        ),
        _ev(
            3.6,
            "done",
            "match_completed",
            "completed",
            {"decision": "assessed", "fit_band": "partial_fit", "suitability_score": 0.74, "cache_hit": None},
            duration_ms=3600,
            flow=flow,
        ),
    ]
    steps = mr.build_steps(flow, events, now=T0 + timedelta(seconds=10))
    assert [s["key"] for s in steps] == [*mr.BASE_STEPS, "rescore"]
    by_key = _by_key(steps)
    assert by_key["cache"]["status"] == "skipped" and by_key["cache"]["summary"] == {"reason": "not_cached"}
    assert by_key["requirements"]["summary"]["passes"] == 3
    assert by_key["requirements"]["durationMs"] == 105
    assert by_key["rescore"]["status"] == "done"
    assert by_key["rescore"]["summary"]["transitions"] == {"unknown->met": 1}
    detail = mr.assemble_run_detail(RUN, events, [], now=T0 + timedelta(seconds=10))
    assert detail["clarification"] == {
        "gate": None,
        "questions": None,
        "rescore": events[-2]["payload"],
    }


def test_clarify_full_running_marks_clarify_before_done():
    flow = "clarify_full"
    events = [*_request_events(flow), *_scoring_events(0, flow)]
    steps = mr.build_steps(flow, events, now=T0 + timedelta(seconds=2))
    assert [s["key"] for s in steps][-3:] == ["done", "clarify", "questions"]
    by_key = _by_key(steps)
    assert by_key["clarify"]["status"] == "running"
    assert by_key["questions"]["status"] == "pending" and by_key["done"]["status"] == "pending"


# --------------------------------------------------------------------------- endpoints


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return SimpleNamespace(all=lambda: self.rows, one=lambda: self.rows[0])


class _Db:
    """Answers each query by the table it reads."""

    def __init__(self, *, runs=None, events=None, calls=None, missing=False):
        self.runs = runs or []
        self.events = events or []
        self.calls = calls or []
        self.missing = missing
        self.statements: list[tuple[str, dict]] = []
        self.rolled_back = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params or {}))
        if self.missing:
            raise ProgrammingError(
                sql, params, Exception('relation "matching_pipeline_events" does not exist')
            )
        if "COUNT(*) AS total FROM runs" in sql:
            return _Result([{"total": len(self.runs)}])
        if "WITH runs AS" in sql:
            return _Result(self.runs)
        if "FROM matching_pipeline_events WHERE run_id" in sql:
            return _Result([e for e in self.events if e["run_id"] == params["rid"]])
        if "GROUP BY match_run_id" in sql:
            return _Result([{"match_run_id": RUN, "calls": 2, "errors": 1}])
        if "FROM ai_call_logs WHERE match_run_id" in sql:
            return _Result(self.calls)
        if "FROM user_cvs" in sql:
            return _Result([{"id": "cv-1", "user_id": "user-1", "filename": "nguyen-an-cv.pdf"}])
        if "FROM users" in sql:
            return _Result([{"id": "user-1", "name": "Nguyễn An", "email": "an@example.com"}])
        if "FROM job_descriptions" in sql:
            return _Result([{"id": "job-1", "title": "Backend Developer"}])
        raise AssertionError(sql)

    async def rollback(self):
        self.rolled_back = True


_CALLS = [
    {
        "id": 101,
        "session_id": None,
        "match_run_id": RUN,
        "purpose": "matching.semantic.embeddings",
        "provider": "openai",
        "model": "text-embedding-3-small",
        "status": "error",
        "duration_ms": 410,
        "input_tokens": None,
        "output_tokens": None,
        "input_chars": None,
        "output_chars": None,
        "error_type": "APITimeoutError",
        "error_message": "Request timed out.",
        "input_preview": None,
        "output_preview": None,
        "created_at": T0 + timedelta(seconds=0.85),
    },
    {
        "id": 100,
        "session_id": None,
        "match_run_id": RUN,
        "purpose": "matching.targeted_reparse.evidence_search",
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "status": "ok",
        "duration_ms": 280,
        "input_tokens": 1800,
        "output_tokens": 40,
        "input_chars": 6400,
        "output_chars": 120,
        "error_type": None,
        "error_message": None,
        "input_preview": None,
        "output_preview": None,
        "created_at": T0 + timedelta(seconds=0.38),
    },
]


async def test_list_endpoint_contract(monkeypatch):
    summary = mr.summarize_run(RUN, _normal_run())
    db = _Db(runs=[summary])
    body = await admin_router.list_admin_matching_runs(
        limit=20, offset=0, status="completed", days=7, q="An", _={"sub": "admin"}, db=db
    )
    assert body == {
        "items": [
            {
                "runId": RUN,
                "flow": "match",
                "status": "completed",
                "startedAt": "2026-10-19T09:00:00+00:00",
                "finishedAt": "2026-10-19T09:00:00.960000+00:00",
                "durationMs": 960,
                "user": {"id": "user-1", "name": "Nguyễn An", "email": "an@example.com"},
                "cv": {"id": "cv-1", "fileName": "nguyen-an-cv.pdf"},
                "job": {"id": "job-1", "title": "Backend Developer"},
                "decision": "assessed",
                "fitBand": "partial_fit",
                "suitabilityScore": 0.7342,
                "cacheHit": False,
                "currentStep": "done",
                "aiCalls": 2,
                "aiErrors": 1,
                "warnings": ["semantic_dense_provider_fallback_to_sparse"],
            }
        ],
        "total": 1,
    }
    page_sql, page_params = db.statements[0]
    assert "done_event = 'match_completed'" in page_sql and "LOWER(COALESCE(u.name" in page_sql
    assert page_params == {"days": 7, "q": "%an%", "limit": 20, "offset": 0}
    # Names come from one batched query per table.
    assert sum("FROM users WHERE id = ANY" in sql for sql, _ in db.statements) == 1


async def test_list_endpoint_without_filters_has_no_joins():
    db = _Db(runs=[])
    body = await admin_router.list_admin_matching_runs(
        limit=20, offset=0, status="all", days=7, q=None, _={"sub": "admin"}, db=db
    )
    assert body == {"items": [], "total": 0}
    assert "LEFT JOIN users" not in db.statements[0][0]


async def test_detail_endpoint_contract():
    db = _Db(events=_normal_run(), calls=sorted(_CALLS, key=lambda c: c["created_at"]))
    body = await admin_router.get_admin_matching_run(RUN, _={"sub": "admin"}, db=db)
    assert set(body) == {"run", "steps", "requirements", "scoring", "clarification", "events", "ai"}
    assert body["run"]["aiCalls"] == 2 and body["run"]["aiErrors"] == 1
    assert [s["key"] for s in body["steps"]] == list(mr.BASE_STEPS)
    assert set(body["steps"][0]) == {"key", "status", "startedAt", "durationMs", "summary"}
    assert body["requirements"]["byStatus"] == {"met": 2, "unknown": 1}
    assert body["requirements"]["items"][1] == {
        "requirementId": "req-exp",
        "label": "3+ years backend experience",
        "priority": "must_have",
        "status": "unknown",
        "evaluator": "experience_duration",
        "reasonCode": "experience_duration_not_evidenced",
        "confidence": 0.45,
    }
    assert body["scoring"] == {
        "eligibility": "eligible",
        "compatibilityStatus": "compatible",
        "suitabilityScore": 0.7342,
        "diagnosticScore": 0.7342,
        "fitBand": "partial_fit",
        "decision": "assessed",
        "provenanceMode": "requirement_aware",
        "factors": [
            {"name": "requirement_coverage", "score": 0.6667, "weight": 0.6},
            {"name": "semantic", "score": 0.81, "weight": 0.25},
        ],
        "warnings": ["semantic_dense_provider_fallback_to_sparse"],
    }
    assert body["clarification"] is None
    assert set(body["events"][0]) == {"at", "stage", "event", "status", "durationMs", "payload"}
    assert len(body["events"]) == len(_normal_run())
    assert body["ai"]["summary"]["total"] == 2 and body["ai"]["summary"]["errors"] == 1
    assert [c["id"] for c in body["ai"]["calls"]] == [100, 101]
    assert body["ai"]["calls"][1]["purpose"] == "matching.semantic.embeddings"


async def test_detail_endpoint_404_for_unknown_run():
    with pytest.raises(HTTPException) as caught:
        await admin_router.get_admin_matching_run("nope", _={"sub": "admin"}, db=_Db())
    assert caught.value.status_code == 404


@pytest.mark.parametrize("call", ["list", "detail"])
async def test_endpoints_are_503_before_the_migration(call):
    db = _Db(missing=True)
    with pytest.raises(HTTPException) as caught:
        if call == "list":
            await admin_router.list_admin_matching_runs(
                limit=20, offset=0, status="all", days=7, q=None, _={"sub": "admin"}, db=db
            )
        else:
            await admin_router.get_admin_matching_run(RUN, _={"sub": "admin"}, db=db)
    assert caught.value.status_code == 503 and "0032" in caught.value.detail
    assert db.rolled_back


def test_matching_admin_routes_require_admin(client):
    assert client.get("/admin/matching/runs").status_code == 401
    assert client.get(f"/admin/matching/runs/{RUN}").status_code == 401
