"""The reading pass asks once per unresolved requirement, with bounded, non-private candidates."""

from __future__ import annotations

import json

from src.core.config import Settings
from src.modules.matching.domain.schemas import CanonicalJob, UnresolvedRequirement
from src.modules.matching.judge.grounded_judge import GroundedJudge
from src.modules.matching.judge.reading_pass import build_reading_set, select_candidates
from src.modules.user_cvs.schemas import CanonicalResume

SHA = "d" * 64


def _resume(spans: list[tuple[str, str, str, str | None]]) -> CanonicalResume:
    evidence = []
    for index, (evidence_id, section, text, answers) in enumerate(spans):
        item = {
            "evidenceId": evidence_id,
            "documentId": "resume-pass",
            "documentSha256": SHA,
            "section": section,
            "text": text,
            "charStart": index * 1000,
            "charEnd": index * 1000 + len(text),
        }
        if answers is not None:
            item["evidenceSource"] = "candidate_self_report"
            item["sourceRequirementId"] = answers
        evidence.append(item)
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-pass",
            "documentId": "resume-pass",
            "documentSha256": SHA,
            "evidence": evidence,
            "skills": [],
            "languages": [],
            "employment": [],
            "education": [],
            "projects": [],
            "certifications": [],
        }
    )


def _requirement(requirement_id: str, label: str) -> dict:
    return {
        "requirementId": requirement_id,
        "type": "unresolved",
        "sourceEvidenceRef": "jd-ev-1",
        "kind": "other",
        "priority": "must_have",
        "rawLabel": label,
    }


def _job(*requirements: dict) -> CanonicalJob:
    return CanonicalJob.model_validate(
        {
            "schemaVersion": "2.1",
            "jobId": "job-pass",
            "documentId": "job-pass",
            "documentSha256": "b" * 64,
            "jobTitle": "Backend Engineer",
            "requirements": list(requirements),
            "evidence": [
                {
                    "evidenceId": "jd-ev-1",
                    "documentId": "job-pass",
                    "documentSha256": "b" * 64,
                    "section": "requirements",
                    "text": "requirements",
                    "charStart": 0,
                    "charEnd": 12,
                }
            ],
        }
    )


class RecordingClient:
    def __init__(self) -> None:
        self.inputs: list[dict] = []

    def complete(self, *, instructions: str, input_text: str, output_schema: dict) -> str:
        self.inputs.append(json.loads(input_text))
        return json.dumps({"relation": "unsure", "selected": []})


def _settings(enabled: bool, top_k: int = 8) -> Settings:
    return Settings(matching_judge_enabled=enabled, matching_judge_top_k=top_k, openai_api_key=None)


def test_disabled_builds_nothing() -> None:
    assert build_reading_set(_resume([]), _job(), settings=_settings(False)) is None


def test_enabled_without_a_model_records_the_fallback_for_every_requirement() -> None:
    job = _job(_requirement("req-1", "SQL"), _requirement("req-2", "Docker"))

    readings = build_reading_set(_resume([]), job, settings=_settings(True))

    assert readings is not None
    assert readings.entailments == {}
    assert readings.fallbacks == {"req-1": "judge_unavailable", "req-2": "judge_unavailable"}


def test_one_call_per_unresolved_requirement_with_private_sections_withheld() -> None:
    resume = _resume(
        [
            ("cv-sql", "employment", "Tuned SQL queries on PostgreSQL.", None),
            ("cv-phone", "contact", "Phone 0901 SQL", None),
            ("cv-docker", "skills", "Docker, Kubernetes", None),
        ]
    )
    job = _job(_requirement("req-1", "SQL"), _requirement("req-2", "Docker"))
    client = RecordingClient()

    readings = build_reading_set(resume, job, judge=GroundedJudge(client), settings=_settings(True))

    assert readings is not None
    assert set(readings.entailments) == {"req-1", "req-2"}
    assert len(client.inputs) == 2
    cited = {item["evidence_id"] for call in client.inputs for item in call["excerpts"]}
    assert "cv-phone" not in cited
    assert client.inputs[0]["requirement"] == "SQL"


def test_candidates_are_ranked_and_capped_and_the_requirements_own_answer_is_always_in() -> None:
    spans = [(f"cv-{i}", "employment", f"line {i} about nothing", None) for i in range(10)]
    spans.append(("cv-sql", "employment", "Tuned SQL queries on PostgreSQL.", None))
    spans.append(("ans-1", "candidate_answer", "Tôi từng làm việc với SQL hai năm.", "req-1"))
    spans.append(("ans-2", "candidate_answer", "Tôi dùng Docker.", "req-2"))
    resume = _resume(spans)
    requirement = UnresolvedRequirement.model_validate(_requirement("req-1", "SQL"))

    candidates = select_candidates(requirement, resume, top_k=3)

    ids = [item.evidence_id for item in candidates]
    assert ids[0] == "ans-1"
    assert "ans-2" not in ids
    assert "cv-sql" in ids
    assert len(ids) == 4
