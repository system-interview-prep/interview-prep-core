"""Guards on what the candidate-facing interview API exposes and keeps."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.modules.interviews.api.router as interview_router


def test_candidate_evaluation_view_drops_every_hiring_verdict_field() -> None:
    stored = {
        "sessionId": "ses-1",
        "overall_score": 6.8,
        "overallScore": 6.8,
        "decision_recommendation": "REJECT",
        "decisionRecommendation": "REJECT",
        "recommendation": "REJECT",
        "candidate_feedback": "Nêu rõ kết quả đo được.",
    }

    view = interview_router._candidate_evaluation_view(stored)

    assert not {"decision_recommendation", "decisionRecommendation", "recommendation"} & view.keys()
    assert view["overallScore"] == 6.8
    assert view["candidate_feedback"] == "Nêu rõ kết quả đo được."
    assert "REJECT" in stored.values()  # the stored record itself is untouched


async def test_get_evaluation_never_returns_a_hiring_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def owned(_db, _uid, _sid):
        return {"id": "ses-1"}

    async def stored_evaluation(*, db, session_id):
        return {"sessionId": session_id, "overallScore": 4.0, "decisionRecommendation": "REJECT"}

    monkeypatch.setattr(interview_router, "_owned_session", owned)
    monkeypatch.setattr(interview_router, "get_session_evaluation", stored_evaluation)

    body = await interview_router.get_session_evaluation_endpoint(
        session_id="ses-1", user={"sub": "user-a", "roles": []}, db=object()
    )

    assert body == {"sessionId": "ses-1", "overallScore": 4.0}


class _CapturingDb:
    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.sql: list[str] = []

    async def execute(self, statement, _params):
        self.sql.append(str(statement))
        row = self.row
        return SimpleNamespace(
            mappings=lambda: SimpleNamespace(one_or_none=lambda: row, all=lambda: [row] if row else [])
        )


async def test_owned_session_survives_a_deleted_cv() -> None:
    # resume_id/job_id are ON DELETE SET NULL; the session must stay readable.
    db = _CapturingDb({"id": "ses-1", "resume_id": None, "job_id": None, "plan_id": "plan-1"})

    session = await interview_router._owned_session(db, "user-a", "ses-1")

    assert session["id"] == "ses-1"
    assert "resume_id IS NOT NULL" not in db.sql[0]
    assert "job_id IS NOT NULL" not in db.sql[0]
    assert "p.id IS NOT NULL" in db.sql[0]


async def test_session_list_keeps_sessions_whose_cv_was_deleted() -> None:
    db = _CapturingDb(None)

    await interview_router.list_interview_sessions(user={"sub": "user-a"}, db=db)

    assert "resume_id IS NOT NULL" not in db.sql[0]
    assert "p.id IS NOT NULL" in db.sql[0]


async def test_owned_session_still_hides_other_users_sessions() -> None:
    with pytest.raises(HTTPException) as exc:
        await interview_router._owned_session(_CapturingDb(None), "user-a", "ses-of-b")
    assert exc.value.status_code == 404
