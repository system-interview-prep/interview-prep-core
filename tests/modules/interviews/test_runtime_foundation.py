from pydantic import ValidationError

from src.modules.interviews.router import CreateInterviewSession


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


def test_create_interview_session_defaults_are_safe() -> None:
    payload = CreateInterviewSession.model_validate(
        {"resumeId": "cv-1", "jobId": "job-1"}
    )

    assert payload.mode == "text"
    assert payload.locale == "en-US"
    assert payload.duration_minutes == 25


def test_create_interview_session_rejects_invalid_duration() -> None:
    try:
        CreateInterviewSession.model_validate(
            {
                "resumeId": "cv-1",
                "jobId": "job-1",
                "durationMinutes": 2,
            }
        )
    except ValidationError:
        return
    raise AssertionError("durationMinutes below 5 must be rejected")


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
