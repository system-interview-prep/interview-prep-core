"""Vietnamese CV layout: numeric dates, "– nay", dash-separated headers, multi-word headings."""

import pytest

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.source import _section_for_heading, build_source_document

SHA256 = "a" * 64

# A PDF text layer: no blank line between paragraphs, wrapped bullet lines.
VI_CV = "\n".join(
    [
        "KỸ NĂNG",
        "Backend: Node.js, PostgreSQL, Docker",
        "KINH NGHIỆM LÀM VIỆC",
        "Backend Developer — Công ty TNHH ShopNow Việt Nam (03/2023 – nay)",
        "- Thiết kế REST API bằng Node.js phục vụ 200.000 người dùng mỗi",
        "tháng.",
        "- Tối ưu truy vấn PostgreSQL, đạt giải nội bộ 2024.",
        "Công ty Cổ phần TechViet — Junior Developer (07/2021 – 02/2023)",
        "- Viết API bằng Node.js và Docker.",
        "HỌC VẤN & CHỨNG CHỈ",
        "Cử nhân Kỹ thuật Phần mềm — Trường Đại học Công nghệ Thông tin (2017 – 2021)",
    ]
)


def _parse(text: str):
    source = build_source_document(
        DocumentArtifacts(markdown=text), document_id="cv-vi", document_sha256=SHA256
    )
    return DeterministicResumeParser().parse(source, extraction_version="test").resume


def test_every_job_is_extracted_from_a_single_pdf_block() -> None:
    jobs = _parse(VI_CV).employment

    assert [(job.job_title, job.organization) for job in jobs] == [
        ("Backend Developer", "Công ty TNHH ShopNow Việt Nam"),
        # Organization written first is swapped back into place.
        ("Junior Developer", "Công ty Cổ phần TechViet"),
    ]
    current, previous = jobs
    assert current.start_date.value == "2023-03" and current.is_current and current.end_date is None
    assert (previous.start_date.value, previous.end_date.value) == ("2021-07", "2023-02")
    # Wrapped lines are joined; a year inside a bullet does not open a new job.
    assert current.responsibilities == [
        "Thiết kế REST API bằng Node.js phục vụ 200.000 người dùng mỗi tháng.",
        "Tối ưu truy vấn PostgreSQL, đạt giải nội bộ 2024.",
    ]
    assert current.skill_claim_ids


def test_organization_only_header_takes_the_title_from_the_next_line() -> None:
    text = "\n".join(
        ["KINH NGHIỆM", "Công ty ABC (01/2020 – 12/2021)", "Kỹ sư phần mềm", "- Bảo trì hệ thống thanh toán."]
    )
    (job,) = _parse(text).employment
    assert (job.job_title, job.organization) == ("Kỹ sư phần mềm", "Công ty ABC")


@pytest.mark.parametrize(
    ("heading", "section"),
    [
        ("KINH NGHIỆM THỰC TẬP", "employment"),
        ("Kinh nghiệm thực tập", "employment"),
        ("DỰ ÁN ĐÃ THAM GIA", "projects"),
        ("CHỨNG CHỈ & NGOẠI NGỮ", "certifications"),
        ("HỌC VẤN & CHỨNG CHỈ", "education"),
        ("KỸ NĂNG MỀM", "skills"),
        # A sentence that merely starts with the phrase is not a heading.
        ("Kinh nghiệm với Docker 3 năm", None),
    ],
)
def test_multi_word_vietnamese_headings(heading: str, section: str | None) -> None:
    assert _section_for_heading(heading) == section


def test_institution_dash_suffix_is_not_split() -> None:
    text = "\n".join(["EDUCATION", "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY - IUH (2019 - 2023)"])
    (education,) = _parse(text).education
    assert education.institution.endswith("- IUH")
