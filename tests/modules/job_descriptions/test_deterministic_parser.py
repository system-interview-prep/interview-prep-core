import pytest

from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document


def _requirement_concept_ids(parsed) -> set[str]:
    return {
        concept.concept_id
        for item in parsed.requirements
        for concept in ([item.concept] if item.concept else item.atomic_concepts)
    }


def _requirements_by_concept(parsed) -> dict[str, object]:
    return {
        concept.concept_id: item
        for item in parsed.requirements
        for concept in ([item.concept] if item.concept else item.atomic_concepts)
    }


def test_parser_builds_grounded_canonical_backend_job_description() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Job Title: Backend Engineer", "page_idx": 0},
                {"type": "text", "text": "Location: Ho Chi Minh City", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- 2 years of experience with Java and Spring Boot", "page_idx": 0},
                {"type": "text", "text": "- FastAPI is preferred", "page_idx": 0},
                {"type": "text", "text": "Responsibilities", "page_idx": 0},
                {"type": "text", "text": "- Build reliable backend APIs", "page_idx": 0},
                {"type": "text", "text": "Benefits", "page_idx": 0},
                {"type": "text", "text": "- Flexible working hours", "page_idx": 0},
            ],
        ),
        document_id="jd-1",
        document_sha256="a" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    assert parsed.job_title == "Backend Engineer"
    assert parsed.location == "Ho Chi Minh City"
    assert _requirement_concept_ids(parsed) == {
        "skill-java", "skill-spring-boot", "skill-fastapi"
    }
    assert parsed.requirements[0].minimum_experience_months == 24
    assert {
        concept_id: item.priority
        for concept_id, item in _requirements_by_concept(parsed).items()
    } == {
        "skill-java": "must_have", "skill-spring-boot": "must_have", "skill-fastapi": "preferred"
    }
    assert parsed.responsibilities[0].text == "Build reliable backend APIs"
    assert parsed.benefits[0].text == "Flexible working hours"
    assert parsed.career_classifications[0].code == "technology.software-engineering.backend"
    evidence_ids = {item.evidence_id for item in parsed.evidence}
    assert all(set(item.evidence_refs).issubset(evidence_ids) for item in parsed.requirements)


def test_parser_extracts_job_metadata_and_keeps_requirement_section_isolated() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Position: Senior Platform Engineer", "page_idx": 0},
                {"type": "text", "text": "Location: Da Nang", "page_idx": 0},
                {"type": "text", "text": "Full-time hybrid role", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- 3 years of experience with Python", "page_idx": 0},
                {"type": "text", "text": "Benefits", "page_idx": 0},
                {"type": "text", "text": "- Docker training budget", "page_idx": 0},
            ],
        ),
        document_id="jd-2",
        document_sha256="b" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    assert parsed.job_title == "Senior Platform Engineer"
    assert parsed.seniority == "senior"
    assert parsed.employment_type == "full_time"
    assert parsed.work_mode == "hybrid"
    assert parsed.location == "Da Nang"
    assert [item.concept.concept_id for item in parsed.requirements if item.concept] == ["skill-python"]
    assert parsed.requirements[0].minimum_experience_months == 36
    assert "Docker training budget" not in {item.raw_label for item in parsed.requirements}
    assert parsed.benefits[0].text == "Docker training budget"


def test_parser_does_not_assign_experience_or_priority_across_requirement_lines() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- Java is preferred", "page_idx": 0},
                {"type": "text", "text": "- 2 years of experience with Spring Boot", "page_idx": 0},
            ],
        ),
        document_id="jd-3",
        document_sha256="c" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")
    requirements = _requirements_by_concept(parsed)
    assert requirements["skill-java"].priority == "preferred"
    assert requirements["skill-java"].minimum_experience_months is None
    assert requirements["skill-spring-boot"].priority == "must_have"
    assert requirements["skill-spring-boot"].minimum_experience_months == 24


def test_parser_drops_layout_section_metadata_without_label_allowlist() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Backend Developer", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- Python", "page_idx": 0},
                {"type": "text", "text": "Preferred Qualifications", "page_idx": 0},
                {"type": "text", "text": "- FastAPI is a plus", "page_idx": 0},
                {"type": "text", "text": "Applicant context", "page_idx": 0},
                {"type": "text", "text": "Domain context", "page_idx": 0},
                {"type": "text", "text": "Product area", "page_idx": 0},
                {"type": "text", "text": "Benefits", "page_idx": 0},
            ],
        ),
        document_id="jd-structural-labels",
        document_sha256="d" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    assert [item.raw_label for item in parsed.requirements] == ["Python", "FastAPI"]


def test_parser_handles_vietnamese_game_jd_sections_title_and_priority() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Unity Developer - Game Mobile [Hà Nội]", "page_idx": 0},
                {"type": "text", "text": "MÔ TẢ CÔNG VIỆC", "page_idx": 0},
                {"type": "text", "text": "- Phát triển nội dung Mobile Game", "page_idx": 0},
                {"type": "text", "text": "o Sử dụng Unity Engine", "page_idx": 0},
                {"type": "text", "text": "Yêu cầu ứng viên", "page_idx": 0},
                {"type": "text", "text": "- Có ít nhất 2 năm kinh nghiệm lập trình Game với Unity", "page_idx": 0},
                {"type": "text", "text": "• Hiểu về cấu trúc JSON và giao thức HTTP", "page_idx": 0},
                {"type": "text", "text": "• Có kinh nghiệm sử dụng Git", "page_idx": 0},
                {"type": "text", "text": "ƯU TIÊN", "page_idx": 0},
                {"type": "text", "text": "• Có kinh nghiệm phát triển plugin Android hoặc iOS cho Unity", "page_idx": 0},
                {"type": "text", "text": "Quyền lợi", "page_idx": 0},
                {"type": "text", "text": "- Hỗ trợ chi phí học ngoại ngữ", "page_idx": 0},
                {"type": "text", "text": "Địa điểm làm việc", "page_idx": 0},
                {"type": "text", "text": "- Hà Nội: Tầng 12A", "page_idx": 0},
            ],
        ),
        document_id="jd-vn-1",
        document_sha256="d" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")
    requirements = _requirements_by_concept(parsed)
    unity_must_have = next(
        item
        for item in parsed.requirements
        if item.concept and item.concept.concept_id == "skill-unity" and item.priority == "must_have"
    )

    assert parsed.job_title == "Unity Developer - Game Mobile [Hà Nội]"
    assert parsed.location == "Hà Nội"
    assert parsed.responsibilities[0].text == "Phát triển nội dung Mobile Game"
    assert parsed.benefits[0].text == "Hỗ trợ chi phí học ngoại ngữ"
    assert unity_must_have.minimum_experience_months == 24
    assert requirements["skill-json"].priority == "must_have"
    assert requirements["skill-android"].priority == "preferred"
    assert requirements["skill-ios"].priority == "preferred"
    assert parsed.career_classifications[0].code == "technology.game-development"


def test_source_document_repairs_common_mineru_utf8_mojibake() -> None:
    source = build_source_document(
        DocumentArtifacts(markdown="fallback", content_list=[{"type": "text", "text": "MÃ´ táº£ cÃ´ng viá»‡c", "page_idx": 0}]),
        document_id="jd-vn-encoding",
        document_sha256="e" * 64,
    )

    assert source.text == "Mô tả công việc"



def test_parser_handles_decorated_vietnamese_headings_and_ai_requirements() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "[FPT SOFTWARE HCM] TUYỂN DỤNG OJT AI ENGINEER", "page_idx": 0},
                {"type": "text", "text": "QUYỀN LỢI DÀNH CHO BẠN:", "page_idx": 0},
                {"type": "text", "text": "- Làm việc cùng các chuyên gia AI và Data Scientist", "page_idx": 0},
                {"type": "text", "text": "YÊU CẦU ỨNG TUYỂN:", "page_idx": 0},
                {"type": "text", "text": "- Sinh viên năm 4, mới tốt nghiệp ngành CNTT hoặc liên quan", "page_idx": 0},
                {"type": "text", "text": "- Có kiến thức cơ bản về Xử lý ngôn ngữ tự nhiên (NLP), GenAI, Mô hình ngôn ngữ lớn (LLM).", "page_idx": 0},
            ],
        ),
        document_id="jd-fpt-ai-1",
        document_sha256="f" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    assert parsed.job_title == "[FPT SOFTWARE HCM] TUYỂN DỤNG OJT AI ENGINEER"
    assert [item.text for item in parsed.benefits] == ["Làm việc cùng các chuyên gia AI và Data Scientist"]
    assert any("Sinh viên năm 4" in item.raw_label for item in parsed.requirements)
    assert _requirement_concept_ids(parsed) == {
        "skill-natural-language-processing", "skill-generative-ai", "skill-large-language-models"
    }
    assert parsed.career_classifications[0].code == "technology.artificial-intelligence"
    evidence_ids = {item.evidence_id for item in parsed.evidence}
    assert all(set(item.evidence_refs).issubset(evidence_ids) for item in [*parsed.requirements, *parsed.benefits])


def test_parser_accepts_ocr_variant_of_vietnamese_requirement_heading() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "QUY\u00caN L\u1ee2I D\u00c0NH CHO B\u1ea0N:", "page_idx": 0},
                {"type": "text", "text": "- M\u00f4i tr\u01b0\u1eddng l\u00e0m vi\u1ec7c t\u1ed1t", "page_idx": 0},
                {"type": "text", "text": "Y\u00caU C\u1ea6U \u00daNG TUY\u00caN:", "page_idx": 0},
                {"type": "text", "text": "- Bi\u1ebft NLP v\u00e0 GenAI", "page_idx": 0},
                {"type": "text", "text": "C\u00c1CH TH\u1ee8C \u1ee8NG TUY\u00caN:", "page_idx": 0},
                {"type": "text", "text": "- \u0110i\u1ec1n form \u1ee9ng tuy\u1ec3n", "page_idx": 0},
            ],
        ),
        document_id="jd-ocr-heading-1",
        document_sha256="a" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    assert [item.text for item in parsed.benefits] == ["M\u00f4i tr\u01b0\u1eddng l\u00e0m vi\u1ec7c t\u1ed1t"]
    assert _requirement_concept_ids(parsed) == {
        "skill-natural-language-processing", "skill-generative-ai"
    }
    assert not any("form" in item.raw_label for item in parsed.requirements)


def test_parser_prefers_later_task_section_and_does_not_infer_manager_from_a_task() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Unity Developer", "page_idx": 0},
                {"type": "text", "text": "M\u00f4 t\u1ea3 c\u00f4ng vi\u1ec7c", "page_idx": 0},
                {"type": "text", "text": "- Gi\u1edbi thi\u1ec7u c\u00f4ng ty", "page_idx": 0},
                {"type": "text", "text": "M\u00f4 t\u1ea3 c\u00f4ng vi\u1ec7c", "page_idx": 0},
                {"type": "text", "text": "Ph\u00e1t tri\u1ec3n Mobile Game", "page_idx": 0},
                {"type": "text", "text": "Qu\u1ea3n l\u00fd third-party SDK", "page_idx": 0},
                {"type": "text", "text": "Y\u00eau c\u1ea7u", "page_idx": 0},
                {"type": "text", "text": "- Unity v\u00e0 AI", "page_idx": 0},
            ],
        ),
        document_id="jd-unity-sections-1",
        document_sha256="b" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    assert [item.text for item in parsed.responsibilities] == [
        "Ph\u00e1t tri\u1ec3n Mobile Game", "Qu\u1ea3n l\u00fd third-party SDK"
    ]
    assert parsed.seniority is None
    assert parsed.work_mode is None
    assert parsed.career_classifications[0].code == "technology.game-development"


@pytest.mark.parametrize(
    ("text", "expected_priority"),
    [
        # Vietnamese preferred cues
        ("Biết Docker là điểm cộng", "preferred"),
        ("Kubernetes là lợi thế", "preferred"),
        ("Kỹ năng Python là ưu tiên", "preferred"),
        ("Được ưu tiên nếu có kinh nghiệm React", "preferred"),
        ("Ưu tiên ứng viên biết Docker", "preferred"),
        ("Có chứng chỉ AWS là lợi thế lớn", "preferred"),
        ("Docker là điểm cộng lớn", "preferred"),
        # Vietnamese must-have cues
        ("Git là bắt buộc", "must_have"),
        ("Yêu cầu bắt buộc: 3 năm kinh nghiệm", "must_have"),
        ("Phải có kinh nghiệm Spring Boot", "must_have"),
        # English preferred cues
        ("Docker experience is preferred", "preferred"),
        ("Python is a plus", "preferred"),
        ("Kubernetes is nice to have", "preferred"),
        ("Knowledge of Go is desirable", "preferred"),
        ("AWS is an advantage", "preferred"),
        ("Cloud experience is advantageous", "preferred"),
        ("AWS certification is preferred", "preferred"),
        # English must-have cues
        ("Kubernetes is required", "must_have"),
        ("Python is mandatory", "must_have"),
        ("Must have 2 years of experience", "must_have"),
        ("AWS certification is required", "must_have"),
        # Negation with contrast (explicit preferred cue after negation)
        ("AWS certification is not required, but is preferred", "preferred"),
        ("Chứng chỉ không bắt buộc, nhưng là điểm cộng", "preferred"),
    ],
)
def test_detect_priority_table_driven(text: str, expected_priority: str) -> None:
    from src.modules.job_descriptions.parsing.deterministic import _detect_priority

    assert _detect_priority(text, section_priority="must_have") == expected_priority


def test_priority_section_vs_sentence_precedence() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Software Engineer", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- Biết Docker là điểm cộng", "page_idx": 0},
                {"type": "text", "text": "- AWS certification is not required, but is preferred", "page_idx": 0},
                {"type": "text", "text": "- Git là bắt buộc", "page_idx": 0},
                {"type": "text", "text": "Preferred", "page_idx": 0},
                {"type": "text", "text": "- Python", "page_idx": 0},
            ],
        ),
        document_id="jd-pri-precedence-1",
        document_sha256="e" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    # Sentence cue "điểm cộng" overrides section default "must_have" -> "preferred"
    docker_req = next(r for r in parsed.requirements if r.concept and r.concept.concept_id == "skill-docker")
    assert docker_req.priority == "preferred"

    # Negation with preferred contrast -> "preferred"
    aws_req = next(r for r in parsed.requirements if "AWS" in r.raw_label)
    assert aws_req.priority == "preferred"

    # Explicit mandatory cue "bắt buộc" -> "must_have"
    git_req = next(r for r in parsed.requirements if r.concept and r.concept.concept_id == "skill-git")
    assert git_req.priority == "must_have"

    # Section default "preferred" is preserved when no line-level override
    python_req = next(r for r in parsed.requirements if r.concept and r.concept.concept_id == "skill-python")
    assert python_req.priority == "preferred"


def test_negated_requirements_not_extracted() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Backend Developer", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- AWS certification is not required", "page_idx": 0},
                {"type": "text", "text": "- Certification is not mandatory", "page_idx": 0},
                {"type": "text", "text": "- Chứng chỉ không bắt buộc", "page_idx": 0},
                {"type": "text", "text": "- Không yêu cầu kinh nghiệm trước đó", "page_idx": 0},
                {"type": "text", "text": "- AWS certification is not required and may be learned after joining", "page_idx": 0},
                {"type": "text", "text": "- Git là bắt buộc", "page_idx": 0},
            ],
        ),
        document_id="jd-neg-test-1",
        document_sha256="f" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    # None of the pure negated items should be extracted as requirements
    labels = [r.raw_label.lower() for r in parsed.requirements]
    assert not any("aws certification is not required" in l for l in labels)
    assert not any("certification is not mandatory" in l for l in labels)
    assert not any("chứng chỉ không bắt buộc" in l for l in labels)
    assert not any("không yêu cầu kinh nghiệm" in l for l in labels)
    assert not any("may be learned after joining" in l for l in labels)

    # Git là bắt buộc is a valid requirement
    git_req = next(r for r in parsed.requirements if r.concept and r.concept.concept_id == "skill-git")
    assert git_req.priority == "must_have"


def test_special_edge_case_jd_edge_pre_gold_011_negation() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Data Analyst", "page_idx": 0},
                {"type": "text", "text": "A cloud certification is not required; candidates may learn it after joining.", "page_idx": 0},
                {"type": "text", "text": "Candidate constraints", "page_idx": 0},
                {"type": "text", "text": "- Python", "page_idx": 0},
                {"type": "text", "text": "- SQL", "page_idx": 0},
                {"type": "text", "text": "- 3 years of relevant experience", "page_idx": 0},
                {"type": "text", "text": "- Bachelor's degree in Computer Science", "page_idx": 0},
                {"type": "text", "text": "- English at B2 level", "page_idx": 0},
                {"type": "text", "text": "- AWS Certified Cloud Practitioner", "page_idx": 0},
                {"type": "text", "text": "- authorization to work in Vietnam", "page_idx": 0},
                {"type": "text", "text": "- fintech domain knowledge", "page_idx": 0},
                {"type": "text", "text": "- clear written communication", "page_idx": 0},
                {"type": "text", "text": "- Docker", "page_idx": 0},
            ],
        ),
        document_id="jd-edge-pre-gold-011",
        document_sha256="1" * 64,
    )

    parsed = DeterministicJobDescriptionParser().parse(source, extraction_version="mineru-test")

    # AWS certification must NOT appear in extracted requirements
    assert not any("AWS" in r.raw_label or "Practitioner" in r.raw_label for r in parsed.requirements)
    assert not any(r.concept and r.concept.concept_id == "skill-aws" for r in parsed.requirements)

    # Expected 9 valid constraints remain
    assert len(parsed.requirements) == 9


def test_generic_document_level_negation_scenarios() -> None:
    parser = DeterministicJobDescriptionParser()

    # Scenario 2: "AWS certification is not required, but Azure certification is required"
    src2 = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Cloud Engineer", "page_idx": 0},
                {"type": "text", "text": "AWS certification is not required.", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- AWS certification", "page_idx": 0},
                {"type": "text", "text": "- Azure certification is required", "page_idx": 0},
            ],
        ),
        document_id="jd-neg-scen-2",
        document_sha256="2" * 64,
    )
    p2 = parser.parse(src2, extraction_version="mineru-test")
    assert not any("AWS" in r.raw_label for r in p2.requirements)
    azure_req = next(r for r in p2.requirements if "Azure" in r.raw_label)
    assert azure_req.priority == "must_have"

    # Scenario 3: "Certification is not required, but AWS certification is preferred"
    src3 = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Cloud Engineer", "page_idx": 0},
                {"type": "text", "text": "Certification is not required.", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- AWS certification is preferred", "page_idx": 0},
            ],
        ),
        document_id="jd-neg-scen-3",
        document_sha256="3" * 64,
    )
    p3 = parser.parse(src3, extraction_version="mineru-test")
    aws_req = next(r for r in p3.requirements if "AWS" in r.raw_label)
    assert aws_req.priority == "preferred"

    # Scenario 4 & 5: "Python is not required, but SQL is required"
    src4 = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Data Engineer", "page_idx": 0},
                {"type": "text", "text": "Python is not required.", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- Python", "page_idx": 0},
                {"type": "text", "text": "- SQL is required", "page_idx": 0},
            ],
        ),
        document_id="jd-neg-scen-4",
        document_sha256="4" * 64,
    )
    p4 = parser.parse(src4, extraction_version="mineru-test")
    assert not any("Python" in r.raw_label or (r.concept and r.concept.concept_id == "skill-python") for r in p4.requirements)
    sql_req = next(r for r in p4.requirements if "SQL" in r.raw_label or (r.concept and r.concept.concept_id == "skill-sql"))
    assert sql_req.priority == "must_have"

    # Scenario 6: "English certification is not required; English communication is required"
    src6 = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Developer", "page_idx": 0},
                {"type": "text", "text": "English certification is not required.", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- English communication is required", "page_idx": 0},
            ],
        ),
        document_id="jd-neg-scen-6",
        document_sha256="6" * 64,
    )
    p6 = parser.parse(src6, extraction_version="mineru-test")
    eng_req = next(r for r in p6.requirements if "English" in r.raw_label or "english" in r.raw_label.lower())
    assert eng_req.priority == "must_have"

    # Scenario 7: Prevent false suppression - generic negative statement does not suppress unrelated requirements
    src7 = build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Software Engineer", "page_idx": 0},
                {"type": "text", "text": "Cloud certification is not required.", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "- Python", "page_idx": 0},
                {"type": "text", "text": "- SQL", "page_idx": 0},
                {"type": "text", "text": "- Docker", "page_idx": 0},
            ],
        ),
        document_id="jd-neg-scen-7",
        document_sha256="7" * 64,
    )
    p7 = parser.parse(src7, extraction_version="mineru-test")
    req_labels = {r.raw_label for r in p7.requirements}
    assert "Python" in req_labels
    assert "SQL" in req_labels
    assert "Docker" in req_labels

