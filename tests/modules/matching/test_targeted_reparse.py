from datetime import UTC, datetime

from src.modules.matching.requirement_evaluators import evaluate_unresolved_requirement
from src.modules.matching.schemas import CanonicalJob
from src.modules.matching.targeted_reparse import reparse_partial_resume
from src.modules.user_cvs.schemas import CanonicalResume


def _resume(raw_text: str) -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-reparse",
            "documentId": "resume-reparse",
            "documentSha256": "a" * 64,
            "parsing": {
                "parserVersion": "test",
                "extractionVersion": "test",
                "parsedAt": datetime.now(UTC).isoformat(),
                "status": "review_required",
            },
        }
    ).model_copy(update={"raw_text": raw_text})


def _job(label: str) -> CanonicalJob:
    return CanonicalJob.model_validate(
        {
            "schemaVersion": "2.1",
            "jobId": "job-reparse",
            "documentId": "job-reparse",
            "documentSha256": "b" * 64,
            "jobTitle": "Backend Engineer",
            "requirements": [
                {
                    "requirementId": "req-csharp",
                    "type": "unresolved",
                    "kind": "experience",
                    "priority": "must_have",
                    "sourceEvidenceRef": "jd-1",
                    "rawLabel": label,
                }
            ],
            "evidence": [
                {
                    "evidenceId": "jd-1",
                    "documentId": "job-reparse",
                    "documentSha256": "b" * 64,
                    "section": "requirements",
                    "text": label,
                    "charStart": 0,
                    "charEnd": len(label),
                }
            ],
        }
    )


def test_targeted_reparse_recovers_csharp_span_from_raw_text() -> None:
    result = reparse_partial_resume(
        _resume("Backend developer using C# and .NET APIs."),
        _job("Strong hands-on experience with C# / .NET backend development"),
    )

    assert len(result.evidence) == 2
    assert {item.text for item in result.evidence} == {"C#", ".NET"}
    assert all(item.section == "raw_text_fallback" for item in result.evidence)


def test_targeted_reparse_does_not_change_complete_resume() -> None:
    resume = _resume("Backend developer using C#.").model_copy(
        update={"parsing": _resume("x").parsing.model_copy(update={"status": "ready"})}
    )

    result = reparse_partial_resume(resume, _job("C#"))

    assert result.evidence == []


def test_recovered_spans_are_visible_to_requirement_evaluator() -> None:
    resume = reparse_partial_resume(
        _resume("Backend developer using C# and .NET APIs."),
        _job("Strong hands-on experience with C# / .NET backend development"),
    )

    requirement = _job(
        "Strong hands-on experience with C# / .NET backend development"
    ).requirements[0]
    result = evaluate_unresolved_requirement(requirement, resume)

    assert result.status == "unknown"
    assert result.evidence_refs


def test_targeted_reparse_does_not_recover_generic_requirement_words() -> None:
    result = reparse_partial_resume(
        _resume("Backend developer with API development experience."),
        _job("Strong hands-on experience with C# / .NET backend development"),
    )

    assert result.evidence == []


def test_targeted_reparse_recovers_taxonomy_alias_py_for_python() -> None:
    result = reparse_partial_resume(_resume("Built services with py."), _job("Python"))

    assert [item.text for item in result.evidence] == ["py"]
