"""Speculative preview calls are attributed to the session and marked as such.

The §13 pilot found 48 provider calls per anticipation session logged with no
session id and the same purpose as canonical calls: invisible to the budget
and to the report. The preview endpoint now sets the session context and a
``speculative.`` purpose prefix before it calls the engine.
"""

from __future__ import annotations

import pytest

from src.core import pipeline_events
from src.infrastructure.database import get_db
from src.modules.interviews.adapters.voice.api import lab
from src.modules.interviews.core.session_policy.blueprint import default_blueprint

TRANSCRIPT = "Em dùng Redis làm cache và đặt TTL năm phút cho từng key."


def test_the_prefix_lands_in_the_logged_purpose(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_events, "enabled", lambda: True)
    monkeypatch.setattr(pipeline_events, "_ensure_thread", lambda: None)
    recorder = pipeline_events._recorder
    before = len(recorder.calls)
    token = pipeline_events.current_purpose_prefix.set("speculative.")
    try:
        pipeline_events.record_ai_call(purpose="interviews.agent.decide", provider="openai", model="m", status="ok", duration_ms=1)
    finally:
        pipeline_events.current_purpose_prefix.reset(token)
    assert len(recorder.calls) == before + 1, "the call was buffered"
    row = recorder.calls.pop()
    assert row["purpose"] == "speculative.interviews.agent.decide"
    # Outside the preview context nothing is prefixed.
    pipeline_events.record_ai_call(purpose="interviews.agent.decide", provider="openai", model="m", status="ok", duration_ms=1)
    assert recorder.calls.pop()["purpose"] == "interviews.agent.decide"


@pytest.fixture
def preview_app(client, monkeypatch):
    app = client.app
    app.dependency_overrides[lab._voice_runtime_claims] = lambda: {"session_id": "s1", "user_id": "u1"}

    class _DB:
        async def rollback(self) -> None:
            pass

        async def execute(self, *a, **k):
            raise AssertionError("the preview must not reach the database in this test beyond the turns")

    async def _db():
        yield _DB()

    app.dependency_overrides[get_db] = _db
    session = {
        "id": "s1", "user_id": "u1", "resume_id": None, "job_id": None, "mode": "voice", "status": "OPEN",
        "plan_status": "LOCKED", "locale": "vi-VN", "duration_minutes": 25, "metadata": {}, "candidate_settings": {},
    }

    async def runtime_session(_db, _claims):
        return session

    turns = [
        {
            "id": "t1", "turn_index": 1, "status": "ASKED", "question_version_id": "qv-1", "answer_text": None,
            "started_at": None,
            "question_snapshot": {
                "stage": "DEEP_DIVE", "questionText": "Bạn xử lý cache invalidation thế nào?",
                "rubric": {"criteria": "TTL"}, "blueprint": {"policy": default_blueprint().to_payload(), "versionId": "bp-v1", "blueprintId": "bp"},
            },
        }
    ]

    async def load_turns(_db, _sid):
        return [dict(t) for t in turns]

    monkeypatch.setattr(lab, "_runtime_session", runtime_session)
    monkeypatch.setattr(lab, "_load_turns", load_turns)
    monkeypatch.setattr(lab, "unified_enabled", lambda: False)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def test_the_preview_endpoint_marks_its_calls_as_this_sessions_speculation(preview_app, monkeypatch) -> None:
    seen: dict = {}

    class _Engine:
        async def _evaluate_candidate_response(self, **kwargs):
            seen["session"] = pipeline_events.current_session_id.get()
            seen["prefix"] = pipeline_events.current_purpose_prefix.get()
            return {"intent": "ANSWER", "sufficiency_status": "SUFFICIENT"}

    monkeypatch.setattr(lab, "InterviewCoreEngine", _Engine)
    monkeypatch.setattr(lab.candidate_memory, "enabled", lambda: "0")
    response = preview_app.post("/ai/voice-lab/livekit/runtime/preview", json={"transcript": TRANSCRIPT})
    assert response.status_code == 200, response.text
    assert seen == {"session": "s1", "prefix": "speculative."}
