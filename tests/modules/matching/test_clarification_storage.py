"""The question asked and the answer given have to survive a reload.

Both used to live only in React state, so refreshing the page discarded every
answer the candidate had typed and sent the browser back through Jev and the
question model for a fresh set - two provider calls to re-ask the same
requirements, possibly in different words, with the answers gone.

These exercise the store against a stub session, so they assert the contract
the router depends on: what is written, what a reload reads back, and that a
failing store degrades to "ask again" rather than failing the assessment.
"""

from __future__ import annotations

import json

import pytest

from src.modules.matching.clarifications import storage
from src.modules.matching.clarifications.models import ClarificationAnswerOutcome
from src.modules.matching.clarifications.question_generation import ClarificationQuestion

USER = {"sub": "user-1", "roles": ["CANDIDATE"]}
# The matching run these clarifications belong to.
ATTEMPT = "11111111-1111-1111-1111-111111111111"


def _question(requirement_id: str = "req-1", text: str = "Bạn đã dùng React bao lâu rồi?"):
    return ClarificationQuestion(
        requirementId=requirement_id,
        missingDimension="duration",
        confidence=0.9,
        evidenceRefs=["cv-ev-1"],
        questionText=text,
        semanticAlignmentScore=0.84,
    )


class StubSession:
    """Records statements; returns whatever rows the test loaded into it."""

    def __init__(self, rows=None, fail_on=None):
        self.rows = rows or []
        self.fail_on = fail_on
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("database is down")
        return _Result(self.rows)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


def _stored_row(**overrides):
    row = {
        "requirement_id": "req-1",
        "question_text": "Bạn đã dùng React bao lâu rồi?",
        "missing_dimension": "duration",
        "confidence": 0.9,
        "semantic_alignment_score": 0.84,
        "evidence_refs": json.dumps(["cv-ev-1"]),
        "answer_text": None,
        "outcome_status": None,
        "outcome_reason_code": None,
        "outcome_explanation": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_a_generated_question_is_written_as_asked() -> None:
    db = StubSession()

    written = await storage.save_questions(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT, questions=[_question()]
    )

    assert written == 1
    assert db.commits == 1
    sql, params = db.statements[0]
    assert "INSERT INTO clarification_sessions" in sql
    assert params[0]["question_text"] == "Bạn đã dùng React bao lâu rồi?"
    assert params[0]["user_id"] == "user-1"
    assert params[0]["fit_attempt_id"] == ATTEMPT
    assert json.loads(params[0]["evidence_refs"]) == ["cv-ev-1"]


@pytest.mark.asyncio
async def test_an_answered_question_is_never_reworded_underneath_the_candidate() -> None:
    """The upsert keeps an answered row's wording; only unanswered rows change.

    A candidate answering "3 năm" to one question and seeing a different
    question above their answer after a reload would have no way to tell which
    one they had actually replied to.
    """

    db = StubSession()
    await storage.save_questions(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT, questions=[_question()]
    )

    sql, _ = db.statements[0]
    assert "ON CONFLICT (fit_attempt_id, requirement_id) DO UPDATE" in sql
    assert "WHEN clarification_sessions.answer_text IS NULL THEN EXCLUDED.question_text" in sql


@pytest.mark.asyncio
async def test_nothing_is_written_when_there_is_nothing_to_write() -> None:
    db = StubSession()

    assert await storage.save_questions(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT, questions=[]
    ) == 0
    assert db.statements == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_an_answer_is_merged_onto_its_question_with_its_outcome() -> None:
    db = StubSession()

    written = await storage.save_answers(
        db,
        user=USER,
        cv_id="cv-1",
        job_id="job-1",
        fit_attempt_id=ATTEMPT,
        answers={"req-1": "Tôi có 3 năm kinh nghiệm."},
        outcomes=[
            ClarificationAnswerOutcome(
                requirementId="req-1",
                evidenceRef="clarification-abc-123",
                status="met",
                reasonCode="candidate_self_report_meets_requirement",
                explanation="Ứng viên tự khai 36 tháng kinh nghiệm.",
            )
        ],
    )

    assert written == 1
    sql, params = db.statements[0]
    assert "UPDATE clarification_sessions" in sql
    assert params[0]["answer_text"] == "Tôi có 3 năm kinh nghiệm."
    assert params[0]["outcome_status"] == "met"
    assert params[0]["outcome_reason_code"] == "candidate_self_report_meets_requirement"


@pytest.mark.asyncio
async def test_a_reload_reads_back_the_question_the_answer_and_the_outcome() -> None:
    db = StubSession(
        rows=[
            _stored_row(
                answer_text="Tôi có 3 năm kinh nghiệm.",
                outcome_status="met",
                outcome_reason_code="candidate_self_report_meets_requirement",
                outcome_explanation="Ứng viên tự khai 36 tháng kinh nghiệm.",
            )
        ]
    )

    questions, answers, outcomes = await storage.load_session(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT
    )

    assert [item.question_text for item in questions] == ["Bạn đã dùng React bao lâu rồi?"]
    assert questions[0].evidence_refs == ["cv-ev-1"]
    assert answers == {"req-1": "Tôi có 3 năm kinh nghiệm."}
    assert [item.status for item in outcomes] == ["met"]
    assert outcomes[0].reason_code == "candidate_self_report_meets_requirement"


@pytest.mark.asyncio
async def test_an_unanswered_question_comes_back_without_an_answer() -> None:
    db = StubSession(rows=[_stored_row()])

    questions, answers, outcomes = await storage.load_session(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT
    )

    assert len(questions) == 1
    assert answers == {}
    assert outcomes == []


@pytest.mark.asyncio
async def test_jsonb_evidence_refs_are_accepted_as_a_list_too() -> None:
    """psycopg may hand back parsed JSON rather than a string."""

    db = StubSession(rows=[_stored_row(evidence_refs=["cv-ev-1", "cv-ev-2"])])

    questions, _, _ = await storage.load_session(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT
    )

    assert questions[0].evidence_refs == ["cv-ev-1", "cv-ev-2"]


@pytest.mark.asyncio
async def test_one_unreadable_row_does_not_hide_the_rest() -> None:
    db = StubSession(rows=[_stored_row(question_text="short"), _stored_row(requirement_id="req-2")])

    questions, _, _ = await storage.load_session(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT
    )

    assert [item.requirement_id for item in questions] == ["req-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_statement", ["INSERT INTO", "UPDATE clarification_sessions"])
async def test_a_failed_write_is_rolled_back_and_never_raised(failing_statement) -> None:
    """Persistence may not fail a completed assessment; it costs a reload."""

    db = StubSession(fail_on=failing_statement)

    written = (
        await storage.save_questions(
            db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT, questions=[_question()]
        )
        if failing_statement == "INSERT INTO"
        else await storage.save_answers(
            db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT,
            answers={"req-1": "Tôi có 3 năm."}, outcomes=[],
        )
    )

    assert written == 0
    assert db.rollbacks == 1
    assert db.commits == 0


@pytest.mark.asyncio
async def test_an_unreadable_store_falls_back_to_asking_again() -> None:
    db = StubSession(fail_on="SELECT")

    assert await storage.load_session(db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT
    ) == ([], {}, [])


@pytest.mark.asyncio
async def test_a_rollback_that_itself_fails_is_not_raised_either() -> None:
    """The recovery path may not be the thing that breaks the request."""

    class ExplodingRollback(StubSession):
        async def rollback(self):
            raise RuntimeError("connection already gone")

    db = ExplodingRollback(fail_on="INSERT INTO")

    assert await storage.save_questions(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT, questions=[_question()]
    ) == 0


@pytest.mark.asyncio
async def test_a_new_run_does_not_read_an_earlier_run_s_answers() -> None:
    """Re-assessing the same CV against the same posting has to ask afresh.

    Keyed on (user, cv, job) alone, a second assessment restored the first
    one's questions and the answers given to them, so a deliberate new run
    looked like a page that had never reset. The run id is what separates
    them, and the query is scoped to it.
    """

    db = StubSession(rows=[])

    questions, answers, outcomes = await storage.load_session(
        db, user=USER, cv_id="cv-1", job_id="job-1",
        fit_attempt_id="22222222-2222-2222-2222-222222222222",
    )

    assert (questions, answers, outcomes) == ([], {}, [])
    _, params = db.statements[0]
    assert params["fit_attempt_id"] == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_run", [None, ""])
async def test_a_caller_with_no_run_keeps_no_record(missing_run) -> None:
    """The admin matching view records no attempt, so it gets no shared bucket."""

    db = StubSession(rows=[_stored_row()])

    assert await storage.save_questions(
        db, user=USER, cv_id="cv-1", job_id="job-1",
        fit_attempt_id=missing_run, questions=[_question()],
    ) == 0
    assert await storage.save_answers(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=missing_run,
        answers={"req-1": "Có."}, outcomes=[],
    ) == 0
    assert await storage.load_session(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=missing_run
    ) == ([], {}, [])
    assert db.statements == []


@pytest.mark.asyncio
async def test_an_answer_is_merged_only_within_its_own_run() -> None:
    db = StubSession()

    await storage.save_answers(
        db, user=USER, cv_id="cv-1", job_id="job-1", fit_attempt_id=ATTEMPT,
        answers={"req-1": "Tôi có 3 năm."}, outcomes=[],
    )

    sql, params = db.statements[0]
    assert "WHERE fit_attempt_id = CAST(:fit_attempt_id AS uuid)" in sql
    assert params[0]["fit_attempt_id"] == ATTEMPT
