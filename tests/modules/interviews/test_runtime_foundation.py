from pydantic import ValidationError

from src.modules.interviews.api.router import CreateInterviewSession


def test_create_interview_session_contract_accepts_camel_case() -> None:
    payload = CreateInterviewSession.model_validate(
        {
            "resumeId": "cv-1",
            "jobId": "job-1",
            "mode": "voice",
            "locale": "vi-VN",
            "durationMinutes": 30,
        }
    )

    assert payload.resume_id == "cv-1"
    assert payload.job_id == "job-1"
    assert payload.mode == "voice"
    assert payload.locale == "vi-VN"
    assert payload.duration_minutes == 30


def test_create_interview_session_accepts_an_exact_due_question_to_repeat() -> None:
    payload = CreateInterviewSession.model_validate(
        {
            "resumeId": "cv-1",
            "jobId": "job-1",
            "reaskConceptId": "skill-ai",
            "reaskQuestionVersionId": "question-version-1",
        }
    )
    assert payload.reask_concept_id == "skill-ai"
    assert payload.reask_question_version_id == "question-version-1"


def test_create_interview_session_accepts_a_selected_fit_attempt() -> None:
    payload = CreateInterviewSession.model_validate(
        {
            "resumeId": "cv-1",
            "jobId": "job-1",
            "fitAttemptId": "attempt-1",
        }
    )
    assert payload.fit_attempt_id == "attempt-1"


def test_create_interview_session_defaults_are_safe() -> None:
    payload = CreateInterviewSession.model_validate(
        {"resumeId": "cv-1", "jobId": "job-1"}
    )

    assert payload.mode == "text"
    assert payload.locale == "vi-VN"
    assert payload.duration_minutes == 25


def test_create_interview_session_rejects_invalid_duration() -> None:
    try:
        CreateInterviewSession.model_validate(
            {
                "resumeId": "cv-1",
                "jobId": "job-1",
                "durationMinutes": 1,
            }
        )
    except ValidationError:
        return
    raise AssertionError("durationMinutes below 2 must be rejected")


def test_create_interview_session_accepts_two_minute_demo() -> None:
    payload = CreateInterviewSession.model_validate(
        {"resumeId": "cv-1", "jobId": "job-1", "durationMinutes": 2}
    )
    assert payload.duration_minutes == 2


def test_create_interview_session_rejects_unknown_mode() -> None:
    try:
        CreateInterviewSession.model_validate(
            {
                "resumeId": "cv-1",
                "jobId": "job-1",
                "mode": "hologram",
            }
        )
    except ValidationError:
        return
    raise AssertionError("unknown interview mode must be rejected")
