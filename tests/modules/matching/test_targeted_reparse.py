from datetime import UTC, datetime

from src.modules.matching.evaluation.requirement_evaluators import evaluate_unresolved_requirement
from src.modules.matching.domain.schemas import CanonicalJob
from src.modules.matching.application.targeted_reparse import reparse_partial_resume
from src.modules.matching.application.targeted_reparse import EvidenceSearchResult
from src.modules.matching.application.facade import MatchingFacade
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


def _skill_job(label: str) -> CanonicalJob:
    payload = _job(label).model_dump(by_alias=True)
    payload["requirements"] = [
        {
            "requirementId": "req-skill",
            "type": "skill",
            "priority": "must_have",
            "sourceEvidenceRef": "jd-1",
            "skill": {
                "conceptId": "skill-kafka",
                "scheme": "skills",
                "taxonomyVersion": "test-v1",
                "label": label,
            },
        }
    ]
    return CanonicalJob.model_validate(payload)


def _multi_skill_job() -> CanonicalJob:
    payload = _job("Kafka and Redis").model_dump(by_alias=True)
    payload["requirements"] = [
        {
            "requirementId": "req-kafka",
            "type": "skill",
            "priority": "must_have",
            "sourceEvidenceRef": "jd-1",
            "skill": {"conceptId": "skill-kafka", "scheme": "skills", "taxonomyVersion": "test", "label": "Kafka"},
        },
        {
            "requirementId": "req-redis",
            "type": "skill",
            "priority": "must_have",
            "sourceEvidenceRef": "jd-1",
            "skill": {"conceptId": "skill-redis", "scheme": "skills", "taxonomyVersion": "test", "label": "Redis"},
        },
    ]
    return CanonicalJob.model_validate(payload)


def test_targeted_reparse_recovers_csharp_span_from_raw_text() -> None:
    result = reparse_partial_resume(
        _resume("Backend developer using C# and .NET APIs."),
        _job("Strong hands-on experience with C# / .NET backend development"),
    )

    assert len(result.evidence) == 2
    assert {item.text for item in result.evidence} == {"C#", ".NET"}
    assert all(item.section == "raw_text_fallback" for item in result.evidence)


def test_targeted_reparse_checks_raw_text_when_ready_parser_has_no_requirement_evidence() -> None:
    resume = _resume("Backend developer using C#.").model_copy(
        update={
            "parsing": _resume("x").parsing.model_copy(
                update={
                    "status": "ready",
                    "raw_text_coverage": "complete",
                    "evidence_index_coverage": "complete",
                }
            )
        }
    )

    result = reparse_partial_resume(resume, _job("C#"))

    assert [item.text for item in result.evidence] == ["C#"]


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


class _FakeEvidenceSearcher:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    def search(self, raw_text: str, requirements: list[dict[str, str]]) -> EvidenceSearchResult:
        self.calls += 1
        return EvidenceSearchResult.model_validate(self.result)


def test_llm_retrieval_accepts_only_verbatim_quotes_and_uses_source_offsets() -> None:
    raw_text = "Profile: built event-driven pipelines with a distributed message broker."
    searcher = _FakeEvidenceSearcher(
        {"items": [{"requirement_id": "req-csharp", "exact_quote": "distributed message broker"}]}
    )

    result = reparse_partial_resume(
        _resume(raw_text), _job("Kafka experience"), evidence_searcher=searcher
    )

    evidence = result.evidence[0]
    assert searcher.calls == 1
    assert evidence.text == raw_text[evidence.char_start : evidence.char_end]
    assert evidence.section == "raw_text_llm_retrieval"


def test_llm_retrieval_handles_normalized_skill_requirements() -> None:
    raw_text = "Implemented asynchronous event streams across several services."
    searcher = _FakeEvidenceSearcher(
        {"items": [{"requirement_id": "req-skill", "exact_quote": "asynchronous event streams"}]}
    )

    result = reparse_partial_resume(
        _resume(raw_text), _skill_job("Kafka"), evidence_searcher=searcher
    )

    assert len(result.evidence) == 1
    assert result.evidence[0].text == raw_text[result.evidence[0].char_start : result.evidence[0].char_end]


def test_llm_search_runs_when_quality_gate_says_review_even_if_coverage_flags_look_complete() -> None:
    raw_text = "UIT | Bachelor of Computer Science | 2022 - Expected 2026"
    resume = _resume(raw_text).model_copy(
        update={
            "parsing": _resume(raw_text).parsing.model_copy(
                update={
                    "raw_text_coverage": "complete",
                    "evidence_index_coverage": "complete",
                    "canonical_section_coverage": {"education": "complete_empty"},
                }
            )
        }
    )
    searcher = _FakeEvidenceSearcher(
        {"items": [{"requirement_id": "req-csharp", "exact_quote": raw_text}]}
    )

    result = reparse_partial_resume(resume, _job("Sinh vien nam 4"), evidence_searcher=searcher)

    assert searcher.calls == 1
    assert result.evidence[0].text == raw_text


def test_llm_retrieval_rejects_invented_quotes_and_unknown_requirement_ids() -> None:
    raw_text = "Profile: backend developer."
    searcher = _FakeEvidenceSearcher(
        {
            "items": [
                {"requirement_id": "req-csharp", "exact_quote": "used Kafka in production"},
                {"requirement_id": "made-up", "exact_quote": "backend developer"},
            ]
        }
    )

    result = reparse_partial_resume(
        _resume(raw_text), _job("Kafka experience"), evidence_searcher=searcher
    )

    assert result.evidence == []


def test_llm_search_failure_does_not_fail_matching_or_inject_evidence() -> None:
    class BrokenSearcher:
        def search(self, raw_text: str, requirements: list[dict[str, str]]) -> EvidenceSearchResult:
            raise RuntimeError("provider unavailable")

    result = reparse_partial_resume(
        _resume("Backend developer."), _job("Kafka experience"), evidence_searcher=BrokenSearcher()
    )

    assert result.evidence == []


def test_requirement_scoped_llm_evidence_reaches_skill_evaluator_as_unknown() -> None:
    raw_text = "Implemented asynchronous event streams across several services."
    searcher = _FakeEvidenceSearcher(
        {"items": [{"requirement_id": "req-skill", "exact_quote": "asynchronous event streams"}]}
    )
    resume = reparse_partial_resume(
        _resume(raw_text), _skill_job("Kafka"), evidence_searcher=searcher
    )

    result = MatchingFacade._evaluate_requirements(resume, _skill_job("Kafka"))[0]

    assert result.status == "unknown"
    assert result.reason_code == "skill_retrieved_evidence_needs_confirmation"
    assert result.evidence_refs == [resume.evidence[0].evidence_id]


def test_llm_recovered_quote_does_not_leak_into_other_requirements() -> None:
    raw_text = "Implemented asynchronous event streams across several services."
    searcher = _FakeEvidenceSearcher(
        {"items": [{"requirement_id": "req-kafka", "exact_quote": "asynchronous event streams"}]}
    )
    job = _multi_skill_job()
    resume = reparse_partial_resume(_resume(raw_text), job, evidence_searcher=searcher)

    results = MatchingFacade._evaluate_requirements(resume, job)

    assert results[0].status == "unknown"
    assert results[0].evidence_refs
    assert results[1].status == "not_met"
    assert not results[1].evidence_refs


def test_unmentioned_skill_is_not_met_when_parser_and_source_find_no_quote() -> None:
    incomplete = _resume("Backend developer.")
    complete = incomplete.model_copy(
        update={
            "parsing": incomplete.parsing.model_copy(
                update={
                    "raw_text_coverage": "complete",
                    "evidence_index_coverage": "complete",
                    "canonical_section_coverage": {"skills": "complete_empty"},
                }
            )
        }
    )

    incomplete_result = MatchingFacade._evaluate_requirements(incomplete, _skill_job("Kafka"))[0]
    complete_result = MatchingFacade._evaluate_requirements(complete, _skill_job("Kafka"))[0]

    assert incomplete_result.status == "not_met"
    assert incomplete_result.reason_code == "skill_not_found"
    assert complete_result.status == "not_met"
    assert complete_result.reason_code == "skill_not_found"
