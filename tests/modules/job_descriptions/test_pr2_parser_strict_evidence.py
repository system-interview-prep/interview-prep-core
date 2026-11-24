from unittest.mock import patch
import pytest

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser
from src.modules.user_cvs.facade import SourceDocument


from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document


def _doc(lines: list[str]) -> SourceDocument:
    return build_source_document(
        DocumentArtifacts(
            markdown="\n".join(lines),
            content_list=[{"type": "text", "text": line, "page_idx": 0} for line in lines],
        ),
        document_id="jd-test",
        document_sha256="a" * 64,
    )


# ==============================================================================
# A & B: COMPANY EXTRACTION & FALSE POSITIVES
# ==============================================================================

def test_a_extracts_company_from_nstage_header_slogan():
    lines = [
        "NSTAGE : Fun Lives On",
        "- NSTAGE thông qua việc vận hành các game live service.",
        "MÔ TẢ CÔNG VIỆC",
        "- Lập trình game",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.company_name == "NSTAGE"
    # Verify evidence span exists
    evidence_texts = [e.text for e in parsed.evidence]
    assert any("NSTAGE : Fun Lives On" in t for t in evidence_texts)


def test_b_avoids_false_positive_company_from_reserved_headers():
    lines = [
        "Địa điểm: Hà Nội",
        "Yêu cầu: Thành thạo Unity",
        "Quyền lợi: Hỗ trợ ăn trưa",
        "Thời gian làm việc: 08:00 - 17:00",
        "Mức lương: Thỏa thuận",
        "MÔ TẢ CÔNG VIỆC",
        "- Lập trình game",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    # None of these should become company_name!
    assert parsed.company_name is None


def test_extracts_company_from_explicit_label():
    lines = [
        "Công ty: VNG Corporation",
        "Vị trí: Backend Engineer",
        "MÔ TẢ CÔNG VIỆC",
        "- Xây dựng dịch vụ",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.company_name == "VNG Corporation"


# ==============================================================================
# C & D: EXPERIENCE EXTRACTION (STRICT, NO SENIORITY INFERENCE)
# ==============================================================================

def test_c_experience_at_least_2_years_does_not_infer_seniority():
    lines = [
        "Vị trí: Unity Developer",
        "YÊU CẦU ỨNG VIÊN",
        "- Có ít nhất 2 năm kinh nghiệm lập trình Game với Unity",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.experience_min_years == 2
    assert parsed.experience_max_years is None
    # Strictly NO over-inference of seniority: 2 years != mid
    assert parsed.seniority is None


def test_d_experience_range_2_to_4_years():
    lines = [
        "Vị trí: Game Developer",
        "YÊU CẦU ỨNG VIÊN",
        "- 2-4 năm kinh nghiệm phát triển phần mềm",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.experience_min_years == 2
    assert parsed.experience_max_years == 4
    assert parsed.seniority is None


def test_experience_from_3_to_5_years():
    lines = [
        "Vị trí: Developer",
        "YÊU CẦU ỨNG VIÊN",
        "- Từ 3 đến 5 năm kinh nghiệm làm việc",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.experience_min_years == 3
    assert parsed.experience_max_years == 5


def test_experience_inequality_over_5_years_keeps_numeric_null():
    # Correction 2: "trên 5 năm" must NOT be canonicalized to minYears=5.
    # Keep numeric range null, preserve experience_raw and evidence.
    lines = [
        "Vị trí: Solution Architect",
        "YÊU CẦU ỨNG VIÊN",
        "- Trên 5 năm kinh nghiệm kiến trúc hệ thống",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.experience_min_years is None
    assert parsed.experience_max_years is None
    assert parsed.experience_raw == "- Trên 5 năm kinh nghiệm kiến trúc hệ thống"
    assert any("Trên 5 năm kinh nghiệm" in e.text for e in parsed.evidence)


def test_experience_inequality_under_2_years_keeps_numeric_null():
    # Correction 2: "dưới 2 năm" must NOT be canonicalized to maxYears=2.
    lines = [
        "Vị trí: Junior Engineer",
        "YÊU CẦU ỨNG VIÊN",
        "- Dưới 2 năm kinh nghiệm",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.experience_min_years is None
    assert parsed.experience_max_years is None
    assert parsed.experience_raw == "- Dưới 2 năm kinh nghiệm"
    assert any("Dưới 2 năm kinh nghiệm" in e.text for e in parsed.evidence)


# ==============================================================================
# E: SENIORITY EXTRACTION & FALSE POSITIVE AVOIDANCE
# ==============================================================================

def test_e_senior_in_title_extracts_seniority():
    lines = [
        "Vị trí: Senior Unity Developer",
        "MÔ TẢ CÔNG VIỆC",
        "- Phát triển game",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.seniority == "senior"


def test_seniority_fresher_and_junior_distinct():
    parser = DeterministicJobDescriptionParser()

    parsed_fresher = parser.parse(_doc(["Vị trí: Fresher Python Developer"]), extraction_version="test")
    assert parsed_fresher.seniority == "fresher"

    parsed_junior = parser.parse(_doc(["Vị trí: Junior Python Developer"]), extraction_version="test")
    assert parsed_junior.seniority == "junior"


def test_avoids_false_positive_seniority_from_other_roles():
    lines = [
        "Vị trí: Unity Developer",
        "YÊU CẦU ỨNG VIÊN",
        "- Phối hợp và làm việc với Senior Manager và Team Lead trong dự án",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    # The position being hired is Unity Developer, NOT Senior Manager
    assert parsed.seniority is None


# ==============================================================================
# F: EMPLOYMENT TYPE (NO INFERENCE FROM WORKING HOURS)
# ==============================================================================

def test_f_working_hours_do_not_infer_employment_type():
    lines = [
        "Vị trí: Developer",
        "Thời gian làm việc",
        "Thứ 2 - Thứ 6, 08:00 - 17:00",
        "MÔ TẢ CÔNG VIỆC",
        "- Phát triển sản phẩm",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.employment_type is None


def test_explicit_employment_type_extracted():
    lines = [
        "Vị trí: Developer",
        "Hình thức làm việc: Toàn thời gian",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.employment_type == "full_time"


# ==============================================================================
# G: WORK MODE (NO INFERENCE FROM OFFICE ADDRESS)
# ==============================================================================

def test_g_office_address_does_not_infer_work_mode():
    lines = [
        "Vị trí: Unity Developer",
        "Địa điểm làm việc:",
        "- Hà Nội: Tầng 12A, Tòa Hapulico Center Building, số 1 Nguyễn Huy Tường",
        "Quyền lợi:",
        "- Cung cấp đồ ăn nhẹ cho nhân viên tại văn phòng",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.location == "Hà Nội"
    assert parsed.work_mode is None  # NOT inferred to on_site!


def test_explicit_work_mode_extracted():
    lines = [
        "Vị trí: Developer",
        "Chế độ: Hybrid",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.work_mode == "hybrid"


# ==============================================================================
# H, I, J: SALARY EXTRACTION (NUMERIC, NEGOTIABLE, COMPETITIVE)
# ==============================================================================

def test_h_salary_numeric_20_to_30_million_per_month():
    lines = [
        "Vị trí: Frontend Developer",
        "Mức lương: 20 - 30 triệu/tháng",
        "MÔ TẢ CÔNG VIỆC",
        "- Lập trình web",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.salary_min == 20_000_000
    assert parsed.salary_max == 30_000_000
    assert parsed.salary_currency == "VND"
    assert parsed.salary_period == "month"
    # Correction 1: Numeric salary does NOT imply non-negotiable! It remains None unless explicitly stated.
    assert parsed.salary_negotiable is None
    assert parsed.salary_raw is not None


def test_salary_numeric_usd():
    lines = [
        "Position: Backend Engineer",
        "Salary: $1,000 - $2,000 / month",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.salary_min == 1000
    assert parsed.salary_max == 2000
    assert parsed.salary_currency == "USD"
    assert parsed.salary_period == "month"
    assert parsed.salary_negotiable is None


def test_salary_numeric_with_explicit_fixed():
    lines = [
        "Position: Backend Engineer",
        "Mức lương: 25 triệu cố định / tháng",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.salary_min == 25_000_000
    assert parsed.salary_negotiable is False


def test_salary_numeric_with_explicit_negotiable():
    lines = [
        "Position: Backend Engineer",
        "Mức lương: 25 - 35 triệu/tháng (có thể thỏa thuận)",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.salary_min == 25_000_000
    assert parsed.salary_max == 35_000_000
    assert parsed.salary_negotiable is True


def test_i_salary_negotiable_is_true():
    lines = [
        "Vị trí: Product Manager",
        "Mức lương: Lương thỏa thuận theo năng lực",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.salary_min is None
    assert parsed.salary_max is None
    assert parsed.salary_currency is None
    assert parsed.salary_period is None
    assert parsed.salary_negotiable is True


def test_j_salary_competitive_is_not_negotiable():
    lines = [
        "Vị trí: Unity Developer - Game Mobile",
        "Mức lương: Thu Nhập Cạnh Tranh, Hỗ Trợ Chi Phí",
    ]
    parser = DeterministicJobDescriptionParser()
    parsed = parser.parse(_doc(lines), extraction_version="test")

    assert parsed.salary_min is None
    assert parsed.salary_max is None
    assert parsed.salary_currency is None
    assert parsed.salary_period is None
    # CRITICAL: "competitive / cạnh tranh" is NOT negotiable!
    assert parsed.salary_negotiable is None
    assert parsed.salary_currency is None
    assert parsed.salary_period is None
    # CRITICAL: "competitive / cạnh tranh" is NOT negotiable!
    assert parsed.salary_negotiable is None


# ==============================================================================
# HYBRID PARSER & EVIDENCE INTEGRITY
# ==============================================================================

class FakeModelClient:
    def __init__(self, output: str) -> None:
        self.output = output
        self.enabled = True

    async def generate(self, request):
        del request
        return self.output


@pytest.mark.asyncio
async def test_hybrid_preserves_baseline_facts_and_grounds_company():
    import json
    doc = _doc([
        "NSTAGE : Fun Lives On",
        "Vị trí: Senior Unity Developer",
        "Mức lương: 20 - 30 triệu/tháng",
        "YÊU CẦU ỨNG VIÊN",
        "- Có ít nhất 2 năm kinh nghiệm lập trình Game với Unity",
    ])

    llm_payload = {
        "jobTitle": {"value": "Senior Unity Developer", "quote": "Senior Unity Developer"},
        "companyName": {"value": "NSTAGE", "quote": "NSTAGE : Fun Lives On"},
        "responsibilities": [],
        "requirements": [],
        "benefits": [],
    }

    hybrid = HybridJobDescriptionParser(client=FakeModelClient(json.dumps(llm_payload)))
    parsed = await hybrid.parse(doc, extraction_version="test")

    assert parsed.company_name == "NSTAGE"
    assert parsed.seniority == "senior"
    assert parsed.experience_min_years == 2
    assert parsed.salary_min == 20_000_000
    assert parsed.salary_max == 30_000_000
    assert parsed.salary_currency == "VND"
