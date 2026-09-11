from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document


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
    assert {item.concept.concept_id for item in parsed.requirements if item.concept} == {
        "skill-java", "skill-spring-boot", "skill-fastapi"
    }
    assert parsed.requirements[0].minimum_experience_months == 24
    assert {item.concept.concept_id: item.priority for item in parsed.requirements} == {
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
    requirements = {item.concept.concept_id: item for item in parsed.requirements if item.concept}
    assert requirements["skill-java"].priority == "preferred"
    assert requirements["skill-java"].minimum_experience_months is None
    assert requirements["skill-spring-boot"].priority == "must_have"
    assert requirements["skill-spring-boot"].minimum_experience_months == 24


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
    requirements = {item.concept.concept_id: item for item in parsed.requirements if item.concept}
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
    assert {item.concept.concept_id for item in parsed.requirements if item.concept} == {
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
    assert {item.concept.concept_id for item in parsed.requirements if item.concept} == {
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
