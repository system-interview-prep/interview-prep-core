import pytest
from pydantic import ValidationError

from src.modules.user_cvs.domain.schemas import CanonicalResume, EmploymentEntry, PartialDate
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, build_source_document

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
                    "content": {"paragraph_content": [{"type": "text", "content": "Python"}]},
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
                "text": (
                    "Họ và tên: Nguyễn Văn A\nNgày sinh: 04/12/1998\ncandidate@example.com | 0901 234 567"
                ),
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


def test_deterministic_parser_uses_explicit_profile_headline_for_ai_primary_classification() -> None:
    source = _source(
        [
            {"type": "title", "text": "PROFILE & RELEVANT SKILLS"},
            {"type": "text", "text": "AI Engineer Intern | NLP | GenAI | LLM/RAG"},
            {"type": "text", "text": "Python, FastAPI, PostgreSQL, NLP, GenAI, LLM, RAG"},
        ]
    )

    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    assert result.resume.profile.headline == "AI Engineer Intern | NLP | GenAI | LLM/RAG"
    primary = next(item for item in result.resume.career_classifications if item.is_primary)
    assert primary.code == "technology.artificial-intelligence"
    assert primary.evidence_refs


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


def test_deterministic_parser_regression_education_and_projects() -> None:
    source = _source(
        [
            {"type": "title", "text": "PROFILE"},
            {
                "type": "text",
                "text": "AI Engineer Intern | NLP | GenAI | LLM/RAG | Machine Learning",
            },
            {"type": "title", "text": "EDUCATION"},
            {"type": "text", "text": "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY - IUH GPA: 3.3/4.0"},
            {
                "type": "text",
                "text": "Information Technology - Software Engineering | Final-year Student | 2022 - Present",
            },
            {"type": "title", "text": "RESEARCH & AI PROJECTS"},
            {
                "type": "text",
                "text": "Career Assistant X - AI Career Guidance Platform | GitHub (Oct 2024 - Present)",
            },
            {"type": "text", "text": "VIN UNIVERSITY - AI20K Build Cohort 3 Competition"},
            {"type": "text", "text": "Tech: Python, FastAPI, PostgreSQL, LangGraph, Docker"},
            {
                "type": "text",
                "text": (
                    "AI Interview Practice Support System - Scientific Research In Progress "
                    "(Jan 2024 - Jun 2024)"
                ),
            },
            {"type": "text", "text": "Scientific Paper: A Microservice-Based Ensemble NLP System"},
            {"type": "text", "text": "Tech: Python, Flask, PostgreSQL"},
            {"type": "title", "text": "ADDITIONAL SOFTWARE & AI PROJECTS"},
            {
                "type": "text",
                "text": "HamTech - AI-powered OTT Chat Platform | GitHub (Feb 2026 - May 2026)",
            },
            {"type": "text", "text": "Tech: Google Gemini API, Kafka, Socket.io"},
            {
                "type": "text",
                "text": "IVORA - Online Wedding Invitation Platform | Website (Dec 2025 - Present)",
            },
            {"type": "text", "text": "Tech: Next.js, Cloudinary"},
        ]
    )
    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    # 1. IUH in education - exactly one grouped education entry
    assert len(result.resume.education) == 1
    edu = result.resume.education[0]
    assert edu.institution == "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY - IUH"
    assert "GPA" not in edu.institution.upper()
    assert edu.degree == "Final-year Student"
    assert edu.field_of_study == "Information Technology - Software Engineering"
    assert edu.start_date and edu.start_date.value == "2022"
    assert edu.end_date is None
    assert edu.student_status == "final_year"
    assert edu.gpa == 3.3
    assert edu.gpa_scale == 4.0
    assert len(edu.evidence_refs) == 2

    # 2. Negative assertions: Major/program/student status/GPA not institutions
    all_institutions = [entry.institution for entry in result.resume.education]
    assert "Information Technology - Software Engineering" not in all_institutions
    assert "Final-year Student" not in all_institutions
    for inst in all_institutions:
        assert "GPA" not in inst.upper()
        assert "AI20K" not in inst
        assert "Career Assistant X" not in inst
        assert "HamTech" not in inst
        assert "IVORA" not in inst
        assert "AI Interview Practice Support System" not in inst

    # 3. Projects separation and correct entity assignment
    project_names = [p.name for p in result.resume.projects]
    assert any("Career Assistant X" in name for name in project_names)
    assert any("HamTech" in name for name in project_names)
    assert any("IVORA" in name for name in project_names)
    assert any("AI Interview Practice Support System" in name for name in project_names)

    # 4. AI20K must NOT be swallowed into Career Assistant X; it is an independent ProjectEntry
    ai20k_project = next((p for p in result.resume.projects if "AI20K" in p.name), None)
    assert ai20k_project is not None
    assert "VIN UNIVERSITY - AI20K Build Cohort 3 Competition" in ai20k_project.name
    assert "Career Assistant X" not in ai20k_project.name
    career_x = next(p for p in result.resume.projects if "Career Assistant X" in p.name)
    assert "AI20K" not in career_x.name
    assert ai20k_project.evidence_refs

    skill_ids = {claim.concept.concept_id for claim in result.resume.skills}
    assert "skill-python" in skill_ids
    assert "skill-langgraph" in skill_ids
    assert "skill-natural-language-processing" in skill_ids
    assert "skill-generative-ai" in skill_ids
    assert "skill-large-language-models" in skill_ids
    assert "skill-retrieval-augmented-generation" in skill_ids
    primary = next(item for item in result.resume.career_classifications if item.is_primary)
    assert primary.code == "technology.artificial-intelligence"

    # 5. 100% of extracted claims have valid, resolvable evidence refs
    evidence_map = {ev.evidence_id: ev for ev in result.resume.evidence}
    for item in [*result.resume.education, *result.resume.projects]:
        assert item.evidence_refs
        for ref in item.evidence_refs:
            assert ref in evidence_map
            ev = evidence_map[ref]
            assert source.text[ev.char_start : ev.char_end] == ev.text


def test_deterministic_parser_project_detail_prefix_grouping() -> None:
    source = _source(
        [
            {"type": "title", "text": "PROJECTS"},
            {"type": "text", "text": "Career Assistant X"},
            {
                "type": "text",
                "text": "Description: An AI career platform designed to help software engineers.",
            },
            {"type": "text", "text": "Responsibilities: Led the full lifecycle development."},
            {
                "type": "text",
                "text": "Key features: Smart CV matching, automated mock interview simulation.",
            },
            {"type": "text", "text": "Highlights: Reduced inference latency by 40%."},
            {"type": "text", "text": "Achievements: Top 5 finalists in AI hackathon."},
            {"type": "text", "text": "Results: 10,000 active users in 3 months."},
            {"type": "text", "text": "Architecture: Event-driven microservices with Kafka."},
            {"type": "text", "text": "Environment: Python 3.12, Docker, PostgreSQL."},
            {"type": "text", "text": "Technologies: Python, LangGraph, FastAPI, PostgreSQL."},
            {"type": "text", "text": "Methods: Multi-agent coordination and self-reflection."},
        ]
    )
    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    # Exactly 1 project created - all detail blocks attached to Career Assistant X
    assert len(result.resume.projects) == 1
    proj = result.resume.projects[0]
    assert proj.name == "Career Assistant X"
    assert proj.description is not None
    assert "Description: An AI career platform" in proj.description
    assert "Responsibilities: Led the full lifecycle" in proj.description
    assert "Key features: Smart CV matching" in proj.description
    assert "Technologies: Python, LangGraph" in proj.description
    assert "Methods: Multi-agent coordination" in proj.description

    # No project named "Description: ..."
    for p in result.resume.projects:
        assert not p.name.lower().startswith("description")
        assert not p.name.lower().startswith("responsibilities")
        assert not p.name.lower().startswith("technologies")


def test_deterministic_parser_negative_education_major_and_gpa_guards() -> None:
    # Test that major/student status/GPA lines never become fake institutions
    source = _source(
        [
            {"type": "title", "text": "EDUCATION"},
            {
                "type": "text",
                "text": "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY - IUH GPA: 3.3/4.0",
            },
            {
                "type": "text",
                "text": "Information Technology - Software Engineering | Final-year Student | 2022 - Present",
            },
            # Edge case: standalone major/gpa block should not create fake institutions
            {"type": "text", "text": "GPA: 3.8/4.0"},
        ]
    )
    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    assert len(result.resume.education) == 1
    edu = result.resume.education[0]
    assert edu.institution == "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY - IUH"
    assert "GPA" not in edu.institution.upper()
    assert edu.institution != "Information Technology - Software Engineering"
    assert edu.institution != "Final-year Student"
    assert edu.field_of_study == "Information Technology - Software Engineering"
    assert edu.degree == "Final-year Student"

    for e in result.resume.education:
        assert "GPA" not in e.institution.upper()
        assert e.institution != "Information Technology - Software Engineering"
        assert e.institution != "Final-year Student"


def test_deterministic_parser_negative_heading_and_label_guards() -> None:
    source = _source(
        [
            {"type": "title", "text": "EDUCATION"},
            {
                "type": "text",
                "text": "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY | Bachelor of Science | 2020 - 2024",
            },
            # Spurious / misclassified lines that should not become education entries
            {"type": "text", "text": "Career Assistant X"},
            {"type": "text", "text": "• Implemented backend using Python and Docker"},
            {"type": "title", "text": "PROJECTS"},
            {"type": "text", "text": "Career Assistant X\nBuilt with Python"},
            {"type": "title", "text": "RESEARCH"},
            {"type": "text", "text": "AI Interview Practice Support System\nNLP research paper"},
        ]
    )
    result = DeterministicResumeParser().parse(source, extraction_version="mineru-3.0.0")

    # 1. Real education parses correctly
    assert len(result.resume.education) == 1
    edu = result.resume.education[0]
    assert "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY" in edu.institution
    assert edu.degree == "Bachelor of Science"

    # 2. Heading PROJECTS is not an institution
    # 3. Heading RESEARCH is not a degree
    # 4. Project name Career Assistant X is not a university
    all_institutions = [e.institution for e in result.resume.education]
    all_degrees = [e.degree for e in result.resume.education if e.degree]
    assert "PROJECTS" not in [inst.upper() for inst in all_institutions]
    assert "RESEARCH" not in [deg.upper() for deg in all_degrees]
    assert "Career Assistant X" not in all_institutions


def test_deterministic_parser_rollback_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.config import get_settings
    from src.modules.user_cvs.parsing.domain.source import (
        _section_for_heading,
        is_section_regex_v2_enabled,
    )

    settings = get_settings()

    # When flag is True (default)
    monkeypatch.setattr(settings, "parser_section_regex_v2_enabled", True)
    assert is_section_regex_v2_enabled() is True
    assert _section_for_heading("RESEARCH & AI PROJECTS") == "projects"
    assert _section_for_heading("ADDITIONAL SOFTWARE & AI PROJECTS") == "projects"

    # When flag is False (instant rollback)
    monkeypatch.setattr(settings, "parser_section_regex_v2_enabled", False)
    assert is_section_regex_v2_enabled() is False
    # In legacy headings, compound phrases like "RESEARCH & AI PROJECTS" are not recognized
    assert _section_for_heading("RESEARCH & AI PROJECTS") is None
    # Standard legacy headings still work
    assert _section_for_heading("PROJECTS") == "projects"
    assert _section_for_heading("EDUCATION") == "education"
