"""Detecting PDFs whose text layer emits one token per glyph."""

from __future__ import annotations

from src.modules.user_cvs.parsing.application.pipeline import (
    _SHREDDED_TEXT_RATIO,
    _character_shredded_ratio,
)

_SHREDDED = (
    "E D U C A T I O N\n"
    "T R A N D U O N G G I A B A O\n"
    "B A C K E N D / F U L L S T A C K D E V E L O P E R I N T E R N\n"
    "C O N T A C T\n+ 8 4 5 3 5 7 6 8 1 7\n"
    "S K I L L S\nJ a v a S p r i n g B o o t D o c k e r P o s t g r e S Q L\n"
    "E X P E R I E N C E\nB u i l t R E S T A P I s a n d C I p i p e l i n e s\n"
)

_CLEAN = (
    "EDUCATION\nUniversity of Technology, Bachelor of Computer Science 2019-2023\n"
    "SKILLS\nJava, Spring Boot, Docker, PostgreSQL, Git, REST API\n"
    "EXPERIENCE\nBackend Developer Intern at Example Company 2022-2023\n"
    "Built REST APIs and CI pipelines, optimised database queries\n"
    "PROJECTS\nDistributed job queue with Redis and Celery workers\n"
)


def test_shredded_text_is_detected() -> None:
    assert _character_shredded_ratio(_SHREDDED) >= _SHREDDED_TEXT_RATIO


def test_clean_text_is_not_flagged() -> None:
    assert _character_shredded_ratio(_CLEAN) < _SHREDDED_TEXT_RATIO


def test_short_text_is_never_flagged() -> None:
    """Initials and short fragments must not look like a shredded document."""
    assert _character_shredded_ratio("A B C\nN G U Y E N") == 0.0
    assert _character_shredded_ratio("") == 0.0


def test_spaced_heading_alone_does_not_flag_a_readable_cv() -> None:
    """Observed in the corpus: a CV whose name renders as "H Ư NG".

    Its skills still parse, so it must stay unflagged - otherwise a readable CV
    is sent for re-extraction it does not need.
    """
    text = (
        "HỌC VẤN\nĐÀO HIỆP H Ư NG\nFRESHER DEVELOPER\nTHÔNG TIN\n0911139501\n"
        "KĨ NĂNG\nC# / .NET\nASP.NET Core / Web API\nEntity Framework Core\n"
        "SQL Server / MySQL\nGit / GitHub\nHTML / CSS / JavaScript\n"
        "OOP / Design Patterns\nKINH NGHIỆM\nFresher Developer tại Công ty Ví dụ\n"
        "Xây dựng Web API và tối ưu truy vấn cơ sở dữ liệu cho hệ thống nội bộ\n"
    )
    assert _character_shredded_ratio(text) < _SHREDDED_TEXT_RATIO
