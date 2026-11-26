"""The server binds a preview to authoritative versions and enforces them.

V2 architecture review, part 1. A preview decision may execute only if the
session, turn, question version, session state, rubric and policy it was
computed against are exactly what the database says at commit time. The key
is built from server state at issuance, sealed in the token, rebuilt at
redemption and compared strictly -- a missing field rejects, a moved field
rejects. The worker never supplies any of it.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from src.core.config import get_settings
from src.infrastructure.database import get_db
from src.modules.interviews.adapters.voice.api import lab
from src.modules.interviews.core.session_policy.blueprint import default_blueprint
from src.modules.interviews.session.chat_runtime import build_session_state_from_db

URL = "/ai/voice-lab/livekit/runtime/message"
TRANSCRIPT = "Em dùng Redis làm cache và đặt TTL năm phút cho từng key."
TRANSCRIPT_HASH = hashlib.sha256(TRANSCRIPT.encode("utf-8")).hexdigest()


def _session() -> dict:
    return {
        "id": "s1",
        "user_id": "u1",
        "resume_id": None,
        "job_id": None,
        "mode": "voice",
        "status": "OPEN",
        "plan_status": "LOCKED",
        "locale": "vi-VN",
        "duration_minutes": 25,
        "started_at": datetime.now(UTC) - timedelta(minutes=5),
        "metadata": {},
        "candidate_settings": {},
    }


def _blueprint(version_id: str) -> dict:
    return {"policy": default_blueprint().to_payload(), "versionId": version_id, "blueprintId": "bp"}


def _turns(
    *,
    rubric: str = "TTL, invalidation khi ghi",
    version_id: str = "bp-v1",
    question_version: str = "qv-1",
) -> list[dict]:
    return [
        {
            "id": "t0",
            "turn_index": 0,
            "status": "COMPLETED",
            "question_version_id": "qv-0",
            "answer_text": "Dạ.",
            "started_at": None,
            "question_snapshot": {
                "stage": "WARM_UP",
                "questionText": "Bạn giới thiệu nhé?",
                "blueprint": _blueprint(version_id),
            },
        },
        {
            "id": "t1",
            "turn_index": 1,
            "status": "ASKED",
            "question_version_id": question_version,
            "answer_text": None,
            "started_at": None,
            "question_snapshot": {
                "stage": "DEEP_DIVE",
                "questionText": "Bạn xử lý cache invalidation thế nào?",
                "rubric": {"criteria": rubric},
                "blueprint": _blueprint(version_id),
            },
        },
    ]


def _advanced(turns: list[dict]) -> list[dict]:
    """The same session one turn later: t1 answered, a new question asked."""
    moved = copy.deepcopy(turns)
    moved[1]["status"] = "ANSWERED"
    moved.append(
        {
            "id": "t2",
            "turn_index": 2,
            "status": "ASKED",
            "question_version_id": "qv-1",
            "answer_text": None,
            "started_at": None,
            "question_snapshot": {
                "stage": "CHALLENGE",
                "questionText": "Thiết kế lại cho mười lần tải?",
                "rubric": {"criteria": "TTL, invalidation khi ghi"},
                "blueprint": _blueprint("bp-v1"),
            },
        }
    )
    return moved


def _key(session=None, turns=None, transcript=TRANSCRIPT):
    session = session or _session()
    turns = turns or _turns()
    current = lab._only_asked_turn(turns)
    state = build_session_state_from_db(session, turns, current, False)
    return lab._authoritative_preview_key(
        session=session, current_turn=current, session_state=state, transcript=transcript
    )


# --------------------------------------------------------------- the key


def test_the_server_key_is_complete_and_deterministic() -> None:
    key = _key()
    assert key.missing() == [], key.missing()
    assert key == _key()


@pytest.mark.parametrize(
    "change,field",
    [
        (dict(rubric="TTL, invalidation khi ghi, xử lý khi Redis lỗi"), "rubric_version"),
        (dict(version_id="bp-v2"), "policy_version"),
        (dict(question_version="qv-2"), "question_id"),
    ],
)
def test_each_authoritative_field_moves_with_its_own_source(change: dict, field: str) -> None:
    before, after = _key(), _key(turns=_turns(**change))
    differing = before.differing(after)
    assert field in differing, differing
    # The question version also feeds the rubric and state hashes by design;
    # what matters is that nothing unrelated moved.
    assert set(differing) <= {field, "rubric_version", "state_version"}


def test_session_state_version_moves_when_the_session_moves() -> None:
    before = _key()
    assert "state_version" in before.differing(_key(turns=_advanced(_turns())))
    session = _session()
    session["plan_status"] = "READY"
    assert "state_version" in before.differing(_key(session=session))


def test_a_session_without_a_frozen_contract_has_no_policy_reference() -> None:
    """Without a contract there is no version id to bind the policy to."""
    turns = _turns()
    for turn in turns:
        turn["question_snapshot"].pop("blueprint", None)
    state = build_session_state_from_db(_session(), turns, lab._only_asked_turn(turns), False)
    reference = (state.get("interview_contract") or {}).get("reference") or {}
    assert not reference.get("version_id")


# ---------------------------------------------------- HTTP redemption


@pytest.fixture
def runtime_app(client, monkeypatch):
    app = client.app
    app.dependency_overrides[lab._voice_runtime_claims] = lambda: {"session_id": "s1", "user_id": "u1"}

    class _DB:
        async def rollback(self) -> None:
            pass

    async def _db():
        yield _DB()

    app.dependency_overrides[get_db] = _db

    async def session(_db, _claims):
        return _session()

    monkeypatch.setattr(lab, "_runtime_session", session)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


DECISION = {
    "action": "acknowledge_and_probe",
    "args": {"kind": "gap", "missing_aspect": "invalidation", "probe_text": "Khi dữ liệu đổi, em xoá key cache thế nào?"},
    "reason": "preview",
    "evidence_refs": ["answer:sent:1"],
    "path": ["model"],
}
EVALUATION = {"intent": "ANSWER", "sufficiency_status": "INSUFFICIENT", "missing_aspect": "invalidation"}


def _token(turns: list[dict], *, with_key: bool = True) -> str:
    """A preview token exactly as issuance seals it, against `turns`."""
    settings = get_settings()
    claims = {
        "scope": "interview:voice-preview",
        "sid": "s1",
        "sub": "u1",
        "turn_id": "t1",
        "transcript_hash": TRANSCRIPT_HASH,
        "evaluation": EVALUATION,
        "decision": DECISION,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=2),
    }
    if with_key:
        claims["key"] = _key(turns=turns).as_dict()
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _redeem(app, monkeypatch, *, token: str, now: list[dict]) -> dict:
    """Redeem a token against the session as it is `now`; return what reached the turn."""
    seen: dict = {}

    async def submit(**kwargs):
        seen.update(kwargs)
        return {"action": "PROBE", "assistantResponse": {"content": "ok"}}

    async def load_turns(_db, _sid):
        return copy.deepcopy(now)

    monkeypatch.setattr(lab, "submit_voice_answer", submit)
    monkeypatch.setattr(lab, "_load_turns", load_turns)
    response = app.post(URL, json={"clientMessageId": "m-1", "content": TRANSCRIPT, "previewToken": token})
    assert response.status_code == 200
    return seen


def test_an_unchanged_session_redeems_both_evaluation_and_decision(runtime_app, monkeypatch) -> None:
    seen = _redeem(runtime_app, monkeypatch, token=_token(_turns()), now=_turns())
    assert seen["precomputed_evaluation"]["intent"] == "ANSWER"
    assert seen["precomputed_decision"]["action"] == "acknowledge_and_probe"


@pytest.mark.parametrize(
    "now",
    [
        pytest.param(_turns(rubric="TTL, invalidation khi ghi, xử lý khi Redis lỗi"), id="rubric_changed"),
        pytest.param(_turns(version_id="bp-v2"), id="policy_changed"),
        pytest.param(_turns(question_version="qv-2"), id="question_changed"),
        pytest.param(_advanced(_turns()), id="session_state_changed"),
    ],
)
def test_a_moved_authoritative_version_rejects_the_whole_preview(runtime_app, monkeypatch, now) -> None:
    """Changed rubric, policy, question or session state: neither the decision
    nor the evaluation may be reused. There is no partial credit."""
    seen = _redeem(runtime_app, monkeypatch, token=_token(_turns()), now=now)
    assert seen["precomputed_decision"] is None
    assert seen["precomputed_evaluation"] is None


def test_an_old_token_without_a_key_is_rejected(runtime_app, monkeypatch) -> None:
    """Strict mode: no key is not 'nothing to check', it is a rejection."""
    seen = _redeem(runtime_app, monkeypatch, token=_token(_turns(), with_key=False), now=_turns())
    assert seen["precomputed_decision"] is None
    assert seen["precomputed_evaluation"] is None
