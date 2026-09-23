import pytest

from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser
from src.modules.job_descriptions.parsing.llm_candidate import JobDescriptionCandidate, RequirementCandidate
from src.modules.user_cvs.facade import SourceDocument
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document


def _make_source(text: str) -> SourceDocument:
    lines = text.splitlines()
    return build_source_document(
        DocumentArtifacts(
            markdown=text,
            content_list=[{"type": "text", "text": line, "page_idx": 0} for line in lines],
        ),
        document_id="jd-test",
        document_sha256="a" * 64,
    )


def test_fpt_ojt_ai_engineer_regression():
    jd_text = (
        "FPT SOFTWARE HCM\n"
        "Vị trí: OJT AI ENGINEER\n\n"
        "Mô tả công việc:\n"
        "- Tham gia nghiên cứu và phát triển các giải pháp AI\n"
        "- Xây dựng mô hình học máy và xử lý dữ liệu\n\n"
        "Yêu cầu công việc:\n"
        "- Sinh viên năm 4 hoặc mới tốt nghiệp CNTT/ngành liên quan\n"
        "- GPA >= 3.2/4.0\n"
        "- IELTS 6.0+ hoặc tương đương\n"
        "- Có kiến thức nền tảng về lập trình vững chắc\n"
        "- Có kiến thức về NLP, GenAI, LLM\n"
        "- Nghiên cứu khoa học hoặc thi AI là lợi thế lớn\n\n"
        "Quyền lợi:\n"
        "- Hỗ trợ thực tập hấp dẫn\n"
        "- Cơ hội trở thành nhân viên chính thức\n"
    )
    source = _make_source(jd_text)
    parser = DeterministicJobDescriptionParser()
    canonical = parser.parse(source, extraction_version="test-v1")

    # 1. Verify Evidence Grounding for ALL requirements
    evidence_map = {ev.evidence_id: ev for ev in canonical.evidence}
    for req in canonical.requirements:
        assert len(req.evidence_refs) >= 1
        for ref in req.evidence_refs:
            assert ref in evidence_map
            ev = evidence_map[ref]
            assert source.text[ev.char_start : ev.char_end] == ev.text
            assert req.raw_label in ev.text or ev.text in req.raw_label or ev.text == req.raw_label

    # A. Education status: Sinh viên năm 4 hoặc mới tốt nghiệp
    edu_status_reqs = [
        r for r in canonical.requirements if r.kind == "education" and r.threshold is None
    ]
    assert len(edu_status_reqs) == 2
    assert all(r.priority == "must_have" for r in edu_status_reqs)
    assert all(r.group_operator == "any_of" for r in edu_status_reqs)
    assert edu_status_reqs[0].group_id is not None
    assert edu_status_reqs[0].group_id == edu_status_reqs[1].group_id
    labels = {r.raw_label for r in edu_status_reqs}
    assert any("Sinh viên năm 4" in text_label for text_label in labels)
    assert any("mới tốt nghiệp" in text_label for text_label in labels)

    # B. GPA: GPA >= 3.2/4.0
    gpa_reqs = [r for r in canonical.requirements if r.kind == "education" and r.threshold == 3.2]
    assert len(gpa_reqs) == 1
    gpa_req = gpa_reqs[0]
    assert gpa_req.operator == "gte"
    assert gpa_req.threshold == 3.2
    assert gpa_req.scale == 4.0
    assert gpa_req.priority == "must_have"
    assert "3.2" in gpa_req.raw_label and "4.0" in gpa_req.raw_label

    # C. Language: IELTS 6.0+ hoặc tương đương
    lang_reqs = [r for r in canonical.requirements if r.kind == "language"]
    assert len(lang_reqs) == 1
    lang_req = lang_reqs[0]
    assert lang_req.credential == "IELTS"
    assert lang_req.operator == "gte"
    assert lang_req.threshold == 6.0
    assert lang_req.equivalent_allowed is True
    assert lang_req.priority == "must_have"

    # D. Programming foundation: No hallucination of Python/Java/C++
    prog_reqs = [
        r for r in canonical.requirements
        if r.kind == "skill" and r.concept is None and not r.atomic_concepts
    ]
    assert len(prog_reqs) == 1
    prog_req = prog_reqs[0]
    assert "lập trình" in prog_req.raw_label.lower()
    assert prog_req.priority == "must_have"
    # Ensure NO hallucinated concepts exist for programming foundation
    assert prog_req.concept is None

    # E. AI Knowledge: NLP, GenAI, LLM (ALL_OF)
    ai_skills = [
        r for r in canonical.requirements
        if r.kind == "skill" and r.atomic_concepts
    ]
    assert len(ai_skills) == 1
    ai_concept_ids = {concept.concept_id for concept in ai_skills[0].atomic_concepts}
    assert "skill-natural-language-processing" in ai_concept_ids
    assert "skill-generative-ai" in ai_concept_ids
    assert "skill-large-language-models" in ai_concept_ids
    assert all(r.priority == "must_have" for r in ai_skills)
    assert ai_skills[0].group_operator == "all_of"
    assert ai_skills[0].raw_label == "Có kiến thức về NLP, GenAI, LLM"

    # F. Research / Competition: NCKH hoặc thi AI là lợi thế lớn (ANY_OF, preferred)
    exp_reqs = [
        r for r in canonical.requirements
        if r.kind == "experience"
    ]
    assert len(exp_reqs) == 2
    assert all(r.priority == "preferred" for r in exp_reqs)
    assert all(r.group_operator == "any_of" for r in exp_reqs)
    assert exp_reqs[0].group_id is not None
    assert exp_reqs[0].group_id == exp_reqs[1].group_id


# =========================================================================
# 10 NEGATIVE TESTS
# =========================================================================


def test_negative_1_java_or_kotlin_disjunction():
    """1. 'Java or Kotlin' => ANY_OF, không phải Java AND Kotlin."""
    jd_text = (
        "Yêu cầu công việc:\n"
        "- Thành thạo Java or Kotlin\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    skills = [r for r in canonical.requirements if r.kind == "skill"]
    assert len(skills) == 1
    assert skills[0].group_operator == "any_of"
    concepts = {concept.concept_id for concept in skills[0].atomic_concepts}
    assert concepts == {"skill-java", "skill-kotlin"}


def test_negative_2_python_and_sql_conjunction():
    """2. 'Python and SQL' => ALL_OF nếu context thể hiện cùng bắt buộc."""
    jd_text = (
        "Requirements:\n"
        "- Strong proficiency in Python and SQL\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    skills = [r for r in canonical.requirements if r.kind == "skill"]
    assert len(skills) == 1
    assert skills[0].group_operator == "all_of"
    concepts = {concept.concept_id for concept in skills[0].atomic_concepts}
    assert concepts == {"skill-python", "skill-sql"}


def test_negative_3_years_of_experience():
    """3. '3 years of Java' => operator gte, duration 36 months, exact source evidence."""
    jd_text = (
        "Requirements:\n"
        "- At least 3 years of Java experience\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    skills = [r for r in canonical.requirements if r.kind == "skill"]
    assert len(skills) == 1
    java_req = skills[0]
    assert java_req.concept.concept_id == "skill-java"
    assert java_req.minimum_experience_months == 36
    assert java_req.operator == "gte"
    ev = canonical.evidence[0]
    assert source.text[ev.char_start : ev.char_end] == ev.text
    assert ev.text == "Java"


def test_negative_4_python_preferred():
    """4. 'Python preferred' => nice_to_have / preferred."""
    jd_text = (
        "Requirements:\n"
        "- Python preferred\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    skills = [r for r in canonical.requirements if r.kind == "skill"]
    assert len(skills) == 1
    assert skills[0].concept.concept_id == "skill-python"
    assert skills[0].priority == "preferred"


def test_negative_5_willing_to_learn():
    """5. 'Willing to learn Python' => không được tạo required Python experience."""
    jd_text = (
        "Requirements:\n"
        "- Willing to learn Python\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    # Must NOT create a required skill for Python
    required_skills = [
        r for r in canonical.requirements
        if r.kind == "skill" and r.priority == "must_have"
    ]
    assert len(required_skills) == 0

    # Any created requirement must be preferred / kind other
    for r in canonical.requirements:
        assert r.priority == "preferred"


def test_negative_6_language_equivalent_allowed():
    """6. 'IELTS 6.0 or equivalent' => không được drop 'equivalent'."""
    jd_text = (
        "Qualifications:\n"
        "- IELTS 6.0 or equivalent\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    lang_reqs = [r for r in canonical.requirements if r.kind == "language"]
    assert len(lang_reqs) == 1
    req = lang_reqs[0]
    assert req.credential == "IELTS"
    assert req.threshold == 6.0
    assert req.equivalent_allowed is True


def test_negative_7_strong_programming_foundation_no_hallucination():
    """7. 'Strong programming foundation' => không hallucinate Java/Python/C++."""
    jd_text = (
        "Requirements:\n"
        "- Strong programming foundation\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    skills = [r for r in canonical.requirements if r.kind == "skill"]
    assert len(skills) == 1
    assert skills[0].concept is None
    assert "programming foundation" in skills[0].raw_label.lower()


def test_negative_8_benefit_text_containing_tech_not_requirement():
    """8. Benefit text chứa công nghệ => không được biến thành requirement."""
    jd_text = (
        "Job Description:\n"
        "Position: Backend Developer\n\n"
        "Requirements:\n"
        "- Solid knowledge of SQL\n\n"
        "Benefits:\n"
        "- Free Python training course and company laptop\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    # Benefits must contain the Python training course
    assert any("Python" in b.text for b in canonical.benefits)

    # Requirements must NOT contain Python
    req_labels = [r.raw_label for r in canonical.requirements]
    assert not any("Python" in lbl for lbl in req_labels)
    req_concepts = [r.concept.concept_id for r in canonical.requirements if r.concept]
    assert "skill-python" not in req_concepts


def test_negative_9_company_location_not_requirement():
    """9. Company marketing/location text => không được biến thành requirement."""
    jd_text = (
        "Company: Tech Corp Vietnam\n"
        "Location: Ho Chi Minh City\n\n"
        "Requirements:\n"
        "- Bachelor degree in Computer Science\n"
    )
    source = _make_source(jd_text)
    canonical = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    for req in canonical.requirements:
        assert "Tech Corp" not in req.raw_label
        assert "Ho Chi Minh City" not in req.raw_label


@pytest.mark.asyncio
async def test_negative_10_llm_candidate_without_exact_quote_rejected():
    """10. LLM candidate không có exact quote => reject."""
    jd_text = (
        "Requirements:\n"
        "- Proficiency in Java\n"
    )
    source = _make_source(jd_text)
    parser = HybridJobDescriptionParser()
    baseline = DeterministicJobDescriptionParser().parse(source, extraction_version="test")

    # Fabricate candidate with a quote that DOES NOT exist in jd_text
    candidate = JobDescriptionCandidate(
        requirements=[
            RequirementCandidate(
                value="Expert in Rust programming",
                quote="Must have 10 years of Rust programming",  # Hallucinated quote
                kind="skill",
                priority="must_have",
            )
        ]
    )

    merged = parser._merge(baseline, source, candidate)
    # The hallucinated Rust requirement must NOT be in merged requirements
    assert not any("Rust" in r.raw_label for r in merged.requirements)
    # A warning must have been recorded
    assert any(w.code == "llm_claim_rejected" for w in merged.parsing.warnings)
