"""Matching runs emit the run-level events of the admin matching view."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.core import pipeline_events as pe
from src.modules.matching.api import router as matching_router
from src.modules.matching.application import match_cache, run_tracking
from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.clarifications import (
    CandidateClarificationAnswer,
    MatchClarificationAnalysis,
    MatchClarificationRescoreRequest,
)
from src.modules.matching.clarifications import rescore as rescore_module
from src.modules.matching.clarifications.ambiguity import JevAmbiguityAnalyzer, build_clarification_plans
from src.modules.matching.clarifications.question_generation import (
    ClarificationQuestion,
    EmbeddingSemanticScorer,
)
from src.modules.matching.domain.schemas import MatchRequest
from src.modules.matching.rag.embedding import OpenAIEmbeddingAdapter
from src.workers.tasks import matching as matching_task

SHA256 = "a" * 64


class StubEmbedder:
    def embed_texts(self, texts):
        return [[1.0, 0.0] for _ in texts]


class PurposeEmbedder:
    def __init__(self):
        self.purposes = []

    def embed_texts(self, texts, *, purpose="matching.embeddings"):
        self.purposes.append(purpose)
        return [[1.0, 0.0] for _ in texts]


def _evidence(evidence_id, document_id, text):
    return {
        "evidenceId": evidence_id,
        "documentId": document_id,
        "documentSha256": SHA256,
        "section": "experience",
        "text": text,
        "charStart": 0,
        "charEnd": len(text),
        "page": 1,
    }


def _request(*, java=True) -> MatchRequest:
    skills = (
        [
            {
                "claimId": "claim-java",
                "concept": {
                    "conceptId": "skill-java",
                    "scheme": "internal",
                    "taxonomyVersion": "2026.1",
                    "label": "Java",
                },
                "rawLabel": "Java",
                "experienceMonths": 48,
                "evidenceRefs": ["cv-ev-java"],
            }
        ]
        if java
        else []
    )
    return MatchRequest.model_validate(
        {
            "schemaVersion": "2.1",
            "resume": {
                "schemaVersion": "2.1",
                "resumeId": "cv-1",
                "documentId": "cv-doc-1",
                "documentSha256": SHA256,
                "rawText": "SECRET CV TEXT with Java experience",
                "skills": skills,
                "evidence": [_evidence("cv-ev-java", "cv-doc-1", "Java experience at ACME")],
            },
            "job": {
                "schemaVersion": "2.1",
                "jobId": "job-1",
                "documentId": "job-doc-1",
                "documentSha256": SHA256,
                "jobTitle": "Backend Developer",
                "evidence": [_evidence("jd-ev-java", "job-doc-1", "Requires Java experience")],
                "requirements": [
                    {
                        "requirementId": "req-java",
                        "type": "skill",
                        "priority": "must_have",
                        "sourceEvidenceRef": "jd-ev-java",
                        "skill": {
                            "conceptId": "skill-java",
                            "scheme": "internal",
                            "taxonomyVersion": "2026.1",
                            "label": "Java",
                        },
                        "operator": "gte",
                        "minimumExperienceMonths": 36,
                    }
                ],
            },
            "matchingPolicy": {"policyVersion": "balanced-v1", "bm25ProviderMode": "in_memory"},
        }
    )


@pytest.fixture
def recorder(monkeypatch):
    monkeypatch.setattr(pe, "enabled", lambda: True)
    monkeypatch.setattr(pe, "_ensure_thread", lambda: None)
    buffers = (pe._recorder.events, pe._recorder.calls, pe._recorder.match_events, pe._recorder.match_calls)
    for buffer in buffers:
        buffer.clear()
    yield pe._recorder
    for buffer in buffers:
        buffer.clear()


def _events(recorder) -> dict[str, dict]:
    return {row["event"]: {**row, "payload": json.loads(row["payload"])} for row in recorder.match_events}


def test_facade_emits_one_event_per_stage_not_per_requirement(recorder):
    embedder = PurposeEmbedder()
    with run_tracking.match_run("match"):
        result = MatchingFacade(embedder).match(_request())
    events = _events(recorder)
    assert [row["event"] for row in recorder.match_events] == [
        "targeted_reparse_completed",
        "requirements_evaluated",
        "semantic_scored",
        "match_scored",
    ]
    requirements = events["requirements_evaluated"]["payload"]
    assert requirements["total"] == 1 and requirements["counts_by_status"] == {"met": 1}
    assert requirements["items"][0] == {
        "requirement_id": "req-java",
        "label": "Java",
        "priority": "must_have",
        "status": "met",
        "evaluator": "knowledge_skill",
        "reason_code": result.requirement_results[0].reason_code,
        "confidence": round(result.requirement_results[0].confidence, 3),
    }
    semantic = events["semantic_scored"]["payload"]
    assert semantic["mode"] == "hybrid" and semantic["bm25_provider_used"] == "in_memory"
    assert semantic["embedding_outcome"] == "ok" and semantic["dense_score"] == 1.0
    assert embedder.purposes == ["matching.semantic.embeddings"]
    scored = events["match_scored"]["payload"]
    assert scored["decision"] == result.decision and scored["fit_band"] == result.fit_band
    assert set(scored["factors"]) >= {"requirement_coverage", "semantic"}
    # No CV text anywhere in the stored rows.
    assert all(
        "SECRET" not in row["payload"] and "ACME" not in row["payload"] for row in recorder.match_events
    )


def test_facade_emits_nothing_without_a_run(recorder):
    MatchingFacade(StubEmbedder()).match(_request())
    assert not recorder.match_events


def test_embedding_adapter_logs_the_callers_purpose_and_disk_cache(recorder, tmp_path, monkeypatch):
    class _Embeddings:
        @staticmethod
        def create(**request):
            data = [SimpleNamespace(index=i, embedding=[0.1, 0.2]) for i, _ in enumerate(request["input"])]
            return SimpleNamespace(data=data, usage=None)

    adapter = OpenAIEmbeddingAdapter("text-embedding-3-small", 2, api_key="k", cache_dir=tmp_path)
    monkeypatch.setattr(adapter, "_client", SimpleNamespace(embeddings=_Embeddings()))
    scorer = EmbeddingSemanticScorer(adapter)
    scorer.score("question?", "intent")
    assert recorder.calls[-1]["purpose"] == "matching.clarification.semantic_check"
    with run_tracking.match_run("match"):
        payload = _request()
        MatchingFacade(adapter).match(payload)
        MatchingFacade(adapter).match(payload)  # second time: every vector from disk
    assert recorder.match_calls[0]["purpose"] == "matching.semantic.embeddings"
    outcomes = [
        json.loads(r["payload"])["embedding_outcome"]
        for r in recorder.match_events
        if r["event"] == "semantic_scored"
    ]
    assert outcomes == ["ok", "disk_cache"]


def test_jev_gate_is_logged_as_an_ai_call_with_gate_counts(recorder):
    def handle(_request):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "next_action": {
                        "type": "choice",
                        "choice": "review_existing_evidence",
                        "confidence": 0.9,
                    },
                    "missing_dimension": {"type": "choice", "choice": "duration", "confidence": 0.9},
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="https://unit.test")
    analyzer = JevAmbiguityAnalyzer(api_key="k", model="jev-x", client=client)
    request = _request(java=False)
    result = MatchingFacade(StubEmbedder()).match(request)
    assert result.requirement_results[0].status == "unknown"
    with run_tracking.match_run("clarify_prepare") as run:
        plans = build_clarification_plans(request, result, analyzer)
    assert plans == []
    (call,) = recorder.match_calls
    assert call["purpose"] == "matching.clarification.jev_gate" and call["provider"] == "typesafe"
    assert call["model"] == "jev-x" and call["match_run_id"] == run.run_id
    gate = _events(recorder)["clarification_gate_completed"]
    assert gate["stage"] == "clarify"
    assert gate["payload"]["analyzed_count"] == 1 and gate["payload"]["approved_count"] == 0
    assert gate["payload"]["rejected_by_reason"] == {"next_action_not_ask_candidate": 1}


def test_jev_provider_error_counts_as_a_failure(recorder):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)), base_url="https://u.t"
    )
    analyzer = JevAmbiguityAnalyzer(api_key="k", client=client)
    request = _request(java=False)
    result = MatchingFacade(StubEmbedder()).match(request)
    with run_tracking.match_run("clarify_prepare"):
        build_clarification_plans(request, result, analyzer)
    assert recorder.match_calls[0]["status"] == "error"
    assert _events(recorder)["clarification_gate_completed"]["payload"]["jev_failures"] == 1


async def test_rescore_passes_share_one_run_and_report_transitions(recorder, monkeypatch):
    monkeypatch.setattr(rescore_module, "get_matching_facade", lambda: MatchingFacade(StubEmbedder()))
    request = _request(java=False)
    initial = MatchingFacade(StubEmbedder()).match(request)
    question = ClarificationQuestion(
        requirementId="req-java",
        missingDimension="duration",
        confidence=0.9,
        evidenceRefs=[],
        questionText="How many months have you used Java in practical work?",
        semanticAlignmentScore=0.9,
    )
    payload = MatchClarificationRescoreRequest(
        matchRequest=request,
        initialAnalysis=MatchClarificationAnalysis(matchResult=initial, clarificationRequests=[question]),
        answers=[
            CandidateClarificationAnswer(requirementId="req-java", answerText="I have 36 months of Java.")
        ],
    )
    with run_tracking.match_run("rescore") as run:
        await rescore_module.rescore_match_with_clarification_answers(payload)
    assert {row["run_id"] for row in recorder.match_events} == {run.run_id}
    assert sum(row["event"] == "match_scored" for row in recorder.match_events) == 3
    rescore = _events(recorder)["rescore_completed"]["payload"]
    assert rescore["answers_count"] == 1 and rescore["passes"] == 3
    assert rescore["transitions"] == {"unknown->met": 1}
    assert "36 months" not in json.dumps(rescore)


class _CacheDb:
    def __init__(self, store):
        self.store = store

    async def scalar(self, sql, params):
        if "EXTRACT" in str(sql):
            return 42.7
        return self.store.get(params["key"])

    async def execute(self, _sql, params):
        self.store[params["key"]] = params["result"]

    async def commit(self):
        pass


async def test_cached_match_reports_lookup_write_and_hit(recorder, monkeypatch):
    store: dict = {}

    @asynccontextmanager
    async def factory():
        yield _CacheDb(store)

    monkeypatch.setattr(match_cache, "SessionFactory", factory)
    facade = MatchingFacade(StubEmbedder())
    with run_tracking.match_run("match") as first:
        await match_cache.cached_match(_request(), facade.match_async)
        run_tracking.emit_match_completed(None)
    assert first.cache_hit is False and len(first.cache_key) == 64
    names = [row["event"] for row in recorder.match_events]
    assert names[0] == "cache_lookup_completed" and names[-2:] == ["cache_write_completed", "match_completed"]
    recorder.match_events.clear()

    with run_tracking.match_run("match") as second:
        result = await match_cache.cached_match(_request(), facade.match_async)
        run_tracking.emit_match_completed(result)
    events = _events(recorder)
    assert list(events) == ["cache_lookup_completed", "match_completed"]
    lookup = events["cache_lookup_completed"]
    assert (
        lookup["status"] == "info" and lookup["payload"]["hit"] is True and lookup["payload"]["age_s"] == 42
    )
    done = events["match_completed"]["payload"]
    assert second.cache_hit is True and done["cache_hit"] is True
    assert done["scoring"]["decision"] == result.decision


async def test_match_ids_input_failure_ends_the_run(recorder, monkeypatch):
    async def missing_resume(_db, _cv_id, *, owner_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy hồ sơ CV với mã cv-x")

    monkeypatch.setattr(matching_router, "_resolve_resume", missing_resume)
    payload = matching_router.MatchIdsRequest(candidateId="cv-x", jobId="job-x")
    with pytest.raises(HTTPException):
        await matching_router._handle_match_ids(
            payload, SimpleNamespace(status_code=None), None, {"sub": "u1"}
        )
    rows = list(recorder.match_events)
    assert [r["event"] for r in rows] == ["match_requested", "inputs_failed", "match_failed"]
    assert {r["user_id"] for r in rows} == {"u1"} and {r["resume_id"] for r in rows} == {"cv-x"}
    failed = json.loads(rows[1]["payload"])
    assert failed["http_status"] == 404 and failed["reason"] == "resume_not_found"
    assert "detail" not in failed and rows[1]["status"] == "error"


def test_async_match_carries_the_run_to_celery(recorder, monkeypatch):
    captured = {}

    def delay(serialized):
        captured.update(serialized)
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(matching_router.match_cv_to_jd, "delay", delay)
    with run_tracking.match_run("match", user_id="u1", resume_id="cv-1", job_id="job-1") as run:
        accepted = matching_router._enqueue({"resume": {}}, run, SimpleNamespace(status_code=None))
    assert accepted.task_id == "task-1"
    assert captured[run_tracking.CELERY_RUN_KEY] == {
        "runId": run.run_id,
        "userId": "u1",
        "resumeId": "cv-1",
        "jobId": "job-1",
    }
    assert recorder.match_events[-1]["event"] == "match_enqueued"
    recorder.match_events.clear()

    seen = {}

    class _Facade:
        async def match_async(self, payload):
            seen["run"] = pe.current_match_run.get()
            return MatchingFacade(StubEmbedder()).match(payload)

    monkeypatch.setattr(matching_task, "get_matching_facade", lambda: _Facade())
    raw = _request().model_dump(by_alias=True)
    raw[run_tracking.CELERY_RUN_KEY] = captured[run_tracking.CELERY_RUN_KEY]
    matching_task.match_cv_to_jd.run(raw)
    assert seen["run"].run_id == run.run_id and seen["run"].flow == "celery"
    assert seen["run"].user_id == "u1"
    assert recorder.match_events[-1]["event"] == "match_completed"
    assert {r["flow"] for r in recorder.match_events} == {"celery"}
