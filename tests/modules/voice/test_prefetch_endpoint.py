"""The deterministic prefetch endpoint warms context and makes no provider call.

Brief §4 (critical path, 2026-11-26): the preview endpoint prefetched the
candidate's memory only as a side effect of a speculative inference that cost
two provider calls. ``/livekit/runtime/prefetch`` does the deterministic part
on its own -- candidate memory, interviewer playbook -- under a zero call
budget, so a provider call from inside it raises instead of spending.
"""

from __future__ import annotations

import pytest

from src.infrastructure.database import get_db
from src.modules.ai import budget
from src.modules.interviews.adapters.voice.api import lab
from src.modules.interviews.admin import interviewer_playbook


@pytest.fixture
def prefetch_app(client, monkeypatch):
    app = client.app
    app.dependency_overrides[lab._voice_runtime_claims] = lambda: {"session_id": "s1", "user_id": "u1"}

    class _DB:
        async def rollback(self) -> None:
            pass

    async def _db():
        yield _DB()

    app.dependency_overrides[get_db] = _db
    session = {"id": "s1", "user_id": "u1", "mode": "voice", "status": "OPEN", "locale": "vi-VN", "metadata": {}}

    async def runtime_session(_db, _claims):
        return session

    async def load_turns(_db, _sid):
        return [{"id": "t1", "turn_index": 1, "status": "ASKED", "question_version_id": "qv-1"}]

    monkeypatch.setattr(lab, "_runtime_session", runtime_session)
    monkeypatch.setattr(lab, "_load_turns", load_turns)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def test_prefetch_warms_memory_and_playbook_under_a_zero_call_budget(prefetch_app, monkeypatch) -> None:
    seen: dict = {}

    async def user_of(_db, _sid):
        return "u1"

    async def load(user_id, **_kw):
        seen["memory_user"] = user_id
        seen["budget"] = budget.current_call_budget.get()
        return [{"kind": "topic", "skill_key": "redis", "text": "cache"}]

    async def refresh(*_a, **_k):
        seen["playbook"] = True

    monkeypatch.setattr(lab.candidate_memory, "enabled", lambda: "readwrite")
    monkeypatch.setattr(lab.candidate_memory, "_user_id_of_session", user_of)
    monkeypatch.setattr(lab.candidate_memory, "load", load)
    monkeypatch.setattr(interviewer_playbook, "refresh", refresh)

    response = prefetch_app.post("/ai/voice-lab/livekit/runtime/prefetch", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["warmed"] == ["candidate_memory", "playbook"] and body["activeTurn"] is True
    assert isinstance(body["ms"], int)
    assert seen["memory_user"] == "u1" and seen["playbook"] is True
    assert seen["budget"] is not None and seen["budget"].limit == 0, "a provider call in here would raise"


def test_a_provider_call_inside_the_prefetch_is_refused_not_paid(prefetch_app, monkeypatch) -> None:
    attempted: list[str] = []

    async def user_of(_db, _sid):
        return "u1"

    async def load(user_id, **_kw):
        # Whatever tried to call the provider from here is stopped at dispatch.
        try:
            budget.charge("interviews.agent.decide")
        except budget.CallBudgetExceeded as exc:
            attempted.append(exc.level)
            raise
        return []

    async def refresh(*_a, **_k):
        pass

    monkeypatch.setattr(lab.candidate_memory, "enabled", lambda: "readwrite")
    monkeypatch.setattr(lab.candidate_memory, "_user_id_of_session", user_of)
    monkeypatch.setattr(lab.candidate_memory, "load", load)
    monkeypatch.setattr(interviewer_playbook, "refresh", refresh)

    response = prefetch_app.post("/ai/voice-lab/livekit/runtime/prefetch", json={})

    assert response.status_code == 200
    assert response.json()["warmed"] == ["playbook"], "the memory step failed closed, the rest still warmed"
    assert attempted == ["scope"]


def test_prefetch_skips_memory_when_it_is_off(prefetch_app, monkeypatch) -> None:
    async def refresh(*_a, **_k):
        pass

    monkeypatch.setattr(lab.candidate_memory, "enabled", lambda: "0")
    monkeypatch.setattr(interviewer_playbook, "refresh", refresh)
    response = prefetch_app.post("/ai/voice-lab/livekit/runtime/prefetch", json={})
    assert response.status_code == 200 and response.json()["warmed"] == ["playbook"]
