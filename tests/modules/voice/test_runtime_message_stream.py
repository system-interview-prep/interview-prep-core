"""The internal message endpoint can stream the Hybrid early acknowledgment.

V2 spec §6.4 / §12 and the correction brief §7. The LiveKit worker POSTs the
answer and, by default, gets one JSON body back — unchanged here. With
``?stream=1`` it gets NDJSON: zero or one ``response.early`` as soon as the
Talker's guarded line exists, then one ``response.final`` carrying exactly
the body the plain path returns. The turn itself — command, checkpoint,
persistence — is identical; only delivery is incremental.
"""

from __future__ import annotations

import json

import pytest

from src.infrastructure.database import get_db
from src.modules.interviews.adapters.voice.api import lab
from src.modules.interviews.session.voice_facade import ChatRuntimeError

URL = "/ai/voice-lab/livekit/runtime/message"
BODY = {"clientMessageId": "m-1", "content": "Em dùng Redis làm cache và đặt TTL năm phút."}
FINAL = {"action": "PROBE", "sessionStatus": "OPEN", "assistantResponse": {"content": "Khi database đổi, em xử lý cache thế nào?"}}


@pytest.fixture
def runtime_app(client, monkeypatch):
    """Authenticated worker, stubbed session and DB; the turn itself is scripted per test."""
    app = client.app
    app.dependency_overrides[lab._voice_runtime_claims] = lambda: {"session_id": "s1", "user_id": "u1"}

    class _DB:
        async def rollback(self) -> None:
            pass

    async def _db():
        yield _DB()

    app.dependency_overrides[get_db] = _db

    async def session(_db, _claims):
        return {"id": "s1", "mode": "voice", "user_id": "u1"}

    monkeypatch.setattr(lab, "_runtime_session", session)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _script(monkeypatch, *, early: str | None = None, raise_: Exception | None = None):
    """Make submit_voice_answer behave like a turn: optionally speak early, then finish."""
    seen: dict = {}

    async def submit(**kwargs):
        seen.update(kwargs)
        if early and kwargs.get("on_early_speech"):
            kwargs["on_early_speech"](early)
        if raise_:
            raise raise_
        return dict(FINAL)

    monkeypatch.setattr(lab, "submit_voice_answer", submit)
    return seen


def test_the_plain_path_is_unchanged(runtime_app, monkeypatch) -> None:
    seen = _script(monkeypatch, early="Mình nghe rồi.")

    response = runtime_app.post(URL, json=BODY)

    assert response.status_code == 200
    assert response.json() == FINAL
    # No stream requested: no callback was handed to the turn at all.
    assert seen["on_early_speech"] is None if "on_early_speech" in seen else True
    assert seen["precomputed_decision"] is None


def test_streaming_delivers_the_early_line_before_the_final_body(runtime_app, monkeypatch) -> None:
    _script(monkeypatch, early="Mình nghe rồi.")

    with runtime_app.stream("POST", URL, params={"stream": "1"}, json=BODY) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in response.iter_lines() if line.strip()]

    assert [e["type"] for e in events] == ["response.early", "response.final"]
    assert events[0]["text"] == "Mình nghe rồi."
    final = {k: v for k, v in events[1].items() if k != "type"}
    assert final == FINAL


def test_streaming_without_an_early_line_is_just_the_final(runtime_app, monkeypatch) -> None:
    _script(monkeypatch)

    with runtime_app.stream("POST", URL, params={"stream": "1"}, json=BODY) as response:
        events = [json.loads(line) for line in response.iter_lines() if line.strip()]

    assert [e["type"] for e in events] == ["response.final"]


def test_a_runtime_error_after_headers_arrives_as_response_error(runtime_app, monkeypatch) -> None:
    """The plain path would raise 409; after the headers are out it becomes an event."""
    _script(monkeypatch, raise_=ChatRuntimeError("session closed"))

    with runtime_app.stream("POST", URL, params={"stream": "1"}, json=BODY) as response:
        events = [json.loads(line) for line in response.iter_lines() if line.strip()]

    assert events == [{"type": "response.error", "status": 409, "detail": "session closed"}]
