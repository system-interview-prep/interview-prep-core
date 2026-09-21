import pytest
from pydantic import ValidationError

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, build_source_document
from src.modules.user_cvs.domain.schemas import CanonicalResume, EmploymentEntry, PartialDate

SHA256 = "b" * 64


def _source(content_list: list) -> object:
    return build_source_document(
        DocumentArtifacts(markdown="fallback", content_list=content_list, extractor_version="3.0.0"),
        document_id="cv-1",
        document_sha256=SHA256,
    )


def test_source_document_preserves_page_order_bbox_and_sections() -> None:
    source = _source(
        [
            {"type": "text", "text": "KỸ NĂNG", "page_idx": 0, "bbox": [10, 20, 100, 40]},
            {"type": "text", "text": "Python, FastAPI", "page_idx": 0, "bbox": [10, 50, 200, 80]},
            {"type": "text", "text": "KINH NGHIỆM", "page_idx": 1, "bbox": [10, 20, 200, 40]},
        ]
    )

    assert source.text == "KỸ NĂNG\nPython, FastAPI\nKINH NGHIỆM"
    assert [block.reading_order for block in source.blocks] == [0, 1, 2]
    assert source.blocks[1].page == 1
    assert source.blocks[1].bounding_box == (10.0, 50.0, 200.0, 80.0)
    assert source.blocks[1].section == "skills"
    assert source.blocks[2].section == "employment"


def test_source_document_reads_mineru_v2_page_structure() -> None:
    source = _source(
        [
            [
                {
                    "type": "title",
                    "content": {"title_content": [{"type": "text", "content": "Skills"}]},
                    "bbox": [10, 10, 100, 30],
                }
            ],
            [
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [{"type": "text", "content": "Python"}]
                    },
                    "bbox": [10, 40, 100, 60],
                }
            ],
        ]
    )

    assert source.text == "Skills\nPython"
    assert source.blocks[0].page == 1
    assert source.blocks[1].page == 2
    assert source.blocks[1].section == "skills"


def test_evidence_mapper_round_trips_exact_source_text() -> None:
    source = _source([{"type": "text", "text": "Skills"}, {"type": "text", "text": "Python"}])
    evidence = EvidenceMapper(source).exact_quote(evidence_id="ev-python", quote="Python")

    assert source.text[evidence.char_start : evidence.char_end] == evidence.text == "Python"
    assert evidence.source_block_id == "block-0001"
    assert evidence.reading_order == 1


def test_evidence_mapper_rejects_ambiguous_quote() -> None:
    source = _source([{"type": "text", "text": "Python"}, {"type": "text", "text": "Python"}])
    with pytest.raises(ValueError, match="exactly once"):
        EvidenceMapper(source).exact_quote(evidence_id="ev-python", quote="Python")


def test_deterministic_parser_extracts_grounded_skills_language_and_separate_pii() -> None:
    source = _source(
        [
            {"type": "title", "text": "KỸ NĂNG", "page_idx": 0, "bbox": [0, 0, 100, 10]},
            {
                "type": "text",
                "text": "Python, FastAPI, PostgreSQL, JavaScript",
                "page_idx": 0,
                "bbox": [0, 20, 500, 40],
            },
            {"type": "title", "text": "NGOẠI NGỮ", "page_idx": 1, "bbox": [0, 0, 100, 10]},
            {"type": "text", "text": "Tiếng Anh: B2", "page_idx": 1, "bbox": [0, 20, 200, 40]},
            {
                "type": "text",
                "text": "Họ và tên: Nguyễn Văn A\nNgày sinh: 04/12/1998\ncandidate@example.com | 0901 234 567",
                "page_idx": 1,
                "bbox": [0, 50, 300, 70],
            },
        ]
    )
    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    assert {claim.concept.concept_id for claim in result.resume.skills} == {
        "skill-python",
        "skill-fastapi",
        "skill-postgresql",
        "skill-javascript",
    }
    assert "skill-java" not in {claim.concept.concept_id for claim in result.resume.skills}
    assert result.resume.languages[0].level == "B2"
    assert result.identity.emails[0].value == "candidate@example.com"
    assert result.identity.phones[0].value == "0901 234 567"
    assert result.identity.full_name and result.identity.full_name.value == "Nguyễn Văn A"
    assert result.identity.date_of_birth and result.identity.date_of_birth.value == "1998-12-04"
    classifications = {item.code: item for item in result.resume.career_classifications}
    assert classifications["technology.software-engineering.backend"].is_primary is True
    assert classifications["technology.software-engineering.backend"].confidence == 0.75
    assert classifications["technology"].evidence_refs
    assert "candidate@example.com" not in result.resume.model_dump_json()
    for evidence in result.resume.evidence:
        assert source.text[evidence.char_start : evidence.char_end] == evidence.text


def test_deterministic_parser_extracts_structured_sections_with_grounded_evidence() -> None:
    source = _source(
        [
            {"type": "title", "text": "Skills"},
            {"type": "text", "text": "Python, Docker"},
            {"type": "title", "text": "Experience"},
            {
                "type": "text",
                "text": "Backend Engineer | Acme Corp | Jan 2020 - Present",
                "page_idx": 0,
                "bbox": [0, 40, 400, 60],
            },
            {"type": "text", "text": "Built Python APIs with Docker."},
            {"type": "title", "text": "Education"},
            {
                "type": "text",
                "text": "Example University | Bachelor of Computer Science | 2016 - 2020",
            },
            {"type": "title", "text": "Projects"},
            {"type": "text", "text": "Hiring Platform\nBuilt with Python"},
            {"type": "title", "text": "Certifications"},
            {"type": "text", "text": "AWS Certified Developer | 2024 - 2027"},
        ]
    )

    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    employment = result.resume.employment[0]
    assert employment.job_title == "Backend Engineer"
    assert employment.organization == "Acme Corp"
    assert employment.start_date and employment.start_date.value == "2020-01"
    assert employment.is_current is True
    assert employment.responsibilities == ["Built Python APIs with Docker."]
    assert set(employment.skill_claim_ids) == {"claim-skill-python", "claim-skill-docker"}
    assert result.resume.education[0].institution == "Example University"
    assert result.resume.education[0].degree == "Bachelor of Computer Science"
    assert result.resume.projects[0].name == "Hiring Platform"
    assert result.resume.projects[0].skill_claim_ids == ["claim-skill-python"]
    assert result.resume.certifications[0].name == "AWS Certified Developer"
    assert result.resume.certifications[0].issued_date.value == "2024"
    assert result.resume.certifications[0].expires_date.value == "2027"
    evidence_ids = {evidence.evidence_id for evidence in result.resume.evidence}
    owners = [
        *result.resume.employment,
        *result.resume.education,
        *result.resume.projects,
        *result.resume.certifications,
    ]
    assert all(owner.evidence_refs and set(owner.evidence_refs).issubset(evidence_ids) for owner in owners)


def test_deterministic_parser_keeps_degree_only_education_schema_valid() -> None:
    source = _source(
        [
            {"type": "title", "text": "Education"},
            {"type": "text", "text": "Bachelor of Science | 2016 - 2020"},
        ]
    )

    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    assert len(result.resume.education) == 1
    assert result.resume.education[0].institution == "Bachelor of Science"
    assert result.resume.education[0].degree == "Bachelor of Science"


def test_canonical_resume_rejects_dangling_evidence_reference() -> None:
    with pytest.raises(ValidationError, match="evidenceRefs"):
        CanonicalResume.model_validate(
            {
                "schemaVersion": "2.1",
                "resumeId": "cv-1",
                "documentId": "cv-1",
                "documentSha256": SHA256,
                "skills": [
                    {
                        "claimId": "claim-python",
                        "concept": {
                            "conceptId": "skill-python",
                            "scheme": "internal",
                            "taxonomyVersion": "internal-2026.1",
                            "label": "Python",
                        },
                        "rawLabel": "Python",
                        "evidenceRefs": ["missing"],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "2026", "precision": "month"},
        {"value": "2026-13", "precision": "month"},
        {"value": "2025-02-29", "precision": "day"},
    ],
)
def test_partial_date_rejects_invalid_value_or_precision(payload: dict) -> None:
    with pytest.raises(ValidationError):
        PartialDate.model_validate(payload)


def test_partial_date_order_allows_compatible_mixed_precision() -> None:
    entry = EmploymentEntry.model_validate(
        {
            "employmentId": "employment-1",
            "jobTitle": "Engineer",
            "startDate": {"value": "2025-12", "precision": "month"},
            "endDate": {"value": "2025", "precision": "year"},
        }
    )
    assert entry.end_date is not None


def test_employment_rejects_definitely_reversed_dates() -> None:
    with pytest.raises(ValidationError, match="startDate"):
        EmploymentEntry.model_validate(
            {
                "employmentId": "employment-1",
                "jobTitle": "Engineer",
                "startDate": {"value": "2026", "precision": "year"},
                "endDate": {"value": "2025-12", "precision": "month"},
            }
        )
