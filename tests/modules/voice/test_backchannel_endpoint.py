"""``/livekit/runtime/backchannel``: the Talker's units as NDJSON, one call, no state.

The Talker is scripted; the test checks what context the endpoint builds for
it (the question, the last lines, the transcript the worker sent, the signals;
no rubric), that one line per unit streams, that the call runs under a
one-call budget, and that an unconfigured feature answers without calling.
"""

from __future__ import annotations

import json

import pytest

from src.infrastructure.database import get_db
from src.modules.ai import budget
from src.modules.interviews.adapters.voice.api import lab
from src.modules.interviews.core.session_policy.blueprint import default_blueprint

URL = "/ai/voice-lab/livekit/runtime/backchannel"


@pytest.fixture
def backchannel_app(client, monkeypatch):
    app = client.app
    app.dependency_overrides[lab._voice_runtime_claims] = lambda: {"session_id": "s1", "user_id": "u1"}

    class _DB:
        async def rollback(self) -> None:
            pass

    async def _db():
        yield _DB()

    app.dependency_overrides[get_db] = _db
    session = {
        "id": "s1", "user_id": "u1", "resume_id": None, "job_id": None, "mode": "voice", "status": "OPEN",
        "plan_status": "LOCKED", "locale": "vi-VN", "duration_minutes": 25, "metadata": {}, "candidate_settings": {},
    }

    async def runtime_session(_db, _claims):
        return session

    turns = [{
        "id": "t1", "turn_index": 1, "status": "ASKED", "question_version_id": "qv-1", "answer_text": None, "started_at": None,
        "question_snapshot": {
            "stage": "DEEP_DIVE", "questionText": "Bạn xử lý cache invalidation thế nào?",
            "rubric": {"criteria": "TTL và xoá theo sự kiện"},
            "blueprint": {"policy": default_blueprint().to_payload(), "versionId": "bp-v1", "blueprintId": "bp"},
        },
    }]

    async def load_turns(_db, _sid):
        return [dict(t) for t in turns]

    async def recent(_db, _sid, limit=4, call_name=None):
        return ["Ừm, mình hiểu ý bạn.", "Bạn xử lý cache invalidation thế nào?"]

    monkeypatch.setattr(lab, "_runtime_session", runtime_session)
    monkeypatch.setattr(lab, "_load_turns", load_turns)
    monkeypatch.setattr(lab, "recent_interviewer_lines", recent)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _lines(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_when_the_feature_is_off_nothing_is_called(backchannel_app, monkeypatch) -> None:
    monkeypatch.setattr(lab, "backchannel_enabled", lambda: False)
    called = []

    async def talker(ctx, **_kw):
        called.append(ctx)
        yield {"type": "done"}

    monkeypatch.setattr(lab, "backchannel_by_talker", talker)
    response = backchannel_app.post(URL, json={"transcript": "Em dùng Redis", "phase": "listening"})
    assert response.status_code == 200
    assert _lines(response) == [{"type": "done", "enabled": False, "reason": "disabled"}] and called == []


def test_units_stream_as_ndjson_from_a_compact_context_under_a_one_call_budget(backchannel_app, monkeypatch) -> None:
    monkeypatch.setattr(lab, "backchannel_enabled", lambda: True)
    seen: dict = {}

    async def talker(ctx, **_kw):
        seen["ctx"] = ctx
        seen["budget"] = budget.current_call_budget.get()
        yield {"type": "unit", "text": "À, TTL theo từng key.", "kind": "reflective", "index": 1, "ttft_ms": 310, "at_ms": 420}
        yield {"type": "done", "kind": "reflective", "ttft_ms": 310, "total_ms": 480, "units": 1, "rejected": [], "reason": None}

    monkeypatch.setattr(lab, "backchannel_by_talker", talker)
    body = {
        "transcript": "Em dùng Redis làm cache và đặt TTL năm phút cho từng key",
        "phase": "processing", "speaking": False,
        "signals": {"seconds_since_last_backchannel": 9.5, "answer_seconds": 14.2, "fragments": 3,
                    "last_backchannels": ["Ừm.", "Mình theo được."]},
    }
    response = backchannel_app.post(URL, json=body)
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/x-ndjson")
    lines = _lines(response)
    assert [l["type"] for l in lines] == ["unit", "done"] and lines[0]["text"] == "À, TTL theo từng key."

    ctx = seen["ctx"]
    assert ctx.session_id == "s1" and ctx.phase == "processing" and ctx.speaking is False
    assert ctx.question == "Bạn xử lý cache invalidation thế nào?"
    assert ctx.transcript == body["transcript"]
    assert list(ctx.recent_lines) == ["Ừm, mình hiểu ý bạn.", "Bạn xử lý cache invalidation thế nào?"]
    assert list(ctx.last_backchannels) == ["Ừm.", "Mình theo được."]
    assert ctx.seconds_since_last == 9.5 and ctx.answer_seconds == 14.2 and ctx.fragments == 3
    assert not hasattr(ctx, "rubric") and "TTL và xoá theo sự kiện" not in ctx.question
    assert seen["budget"] is not None and seen["budget"].limit == 1 and seen["budget"].scope == "backchannel:s1"


def test_the_phase_is_validated(backchannel_app, monkeypatch) -> None:
    monkeypatch.setattr(lab, "backchannel_enabled", lambda: True)
    response = backchannel_app.post(URL, json={"transcript": "x", "phase": "deciding"})
    assert response.status_code == 422
