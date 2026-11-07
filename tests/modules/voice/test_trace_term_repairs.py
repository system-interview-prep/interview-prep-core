"""Trace-derived repairs and independent collision sentences; no audio accuracy claim."""

import unicodedata

import pytest

from src.modules.interviews.adapters.voice.language import normalize_transcript_for_locale
from src.modules.interviews.adapters.voice.transcript_correction import correct_technical_transcript

REPAIRS = [
    ("NestJS Next.js", "Em dùng netJS để viết controller.", "Em dùng NestJS để viết controller."),
    (
        "NestJS Next.js",
        "Trong netJS em viết controller để nhận request.",
        "Trong NestJS em viết controller để nhận request.",
    ),
    (
        "NestJS Next.js",
        "Em dùng net js cùng dependency injection.",
        "Em dùng NestJS cùng dependency injection.",
    ),
    ("NestJS Next.js", "Em dùng netJS với app router.", "Em dùng Next.js với app router."),
    ("NestJS Next.js", "Em dùng netJS với AppRouter.", "Em dùng Next.js với AppRouter."),
    ("NestJS Next.js", "Em dùng net js cùng pages router.", "Em dùng Next.js cùng pages router."),
    (
        "NestJS Next.js",
        "Em dùng netJS có controller và không dùng React.",
        "Em dùng NestJS có controller và không dùng React.",
    ),
    (
        "unknown any",
        "Em dùng inl khi dữ liệu chưa được xác định.",
        "Em dùng unknown khi dữ liệu chưa được xác định.",
    ),
    (
        "unknown",
        "Kiểu inl không cho phép gọi thuộc tính ngay.",
        "Kiểu unknown không cho phép gọi thuộc tính ngay.",
    ),
    (
        "unknown",
        "Với in out em phải kiểm tra trước khi gọi thuộc tính.",
        "Với unknown em phải kiểm tra trước khi gọi thuộc tính.",
    ),
    ("unknown", "Còn inl em cần kiểm tra kiểu trước.", "Còn unknown em cần kiểm tra kiểu trước."),
    (
        "unknown",
        "Với in out, em kiểm tra trước khi gọi thuộc tính.",
        "Với unknown, em kiểm tra trước khi gọi thuộc tính.",
    ),
    (
        "unknown",
        "Em dùng in out khi kiểu chưa rõ, với in out em kiểm tra thuộc tính.",
        "Em dùng unknown khi kiểu chưa rõ, với unknown em kiểm tra thuộc tính.",
    ),
    ("PostgreSQL", "Em dùng postgre sql để lưu câu hỏi.", "Em dùng PostgreSQL để lưu câu hỏi."),
    ("PostgreSQL", "Em dùng post gre sql cho cơ sở dữ liệu.", "Em dùng PostgreSQL cho cơ sở dữ liệu."),
    ("PostgreSQL", "Em chọn post gre s q l để lưu lịch sử.", "Em chọn PostgreSQL để lưu lịch sử."),
    ("PostgreSQL", "Em dùng pốt gre sql ở dự án 2026.", "Em dùng PostgreSQL ở dự án 2026."),
    ("Spring Boot", "Em dùng SpringBoot và Java.", "Em dùng Spring Boot và Java."),
    ("SpringBoot", "Em dùng spring bút để viết API.", "Em dùng Spring Boot để viết API."),
    (
        "React useEffect",
        "Em dùng vue effect trong component React để đồng bộ dữ liệu.",
        "Em dùng useEffect trong component React để đồng bộ dữ liệu.",
    ),
    ("type guard", "Em dùng type gard để kiểm tra kiểu.", "Em dùng type guard để kiểm tra kiểu."),
    ("type guard", "Em dùng type gua để kiểm tra dữ liệu.", "Em dùng type guard để kiểm tra dữ liệu."),
    ("API boundary", "Ở api bao đờ ri em kiểm tra dữ liệu.", "Ở API boundary em kiểm tra dữ liệu."),
    (
        "API boundary",
        "Em dùng a pi ai bao đờ ri để kiểm tra schema.",
        "Em dùng API boundary để kiểm tra schema.",
    ),
]


PRESERVE = [
    ("backend", "Công việc để làm bắt em."),  # Actual truncated interim from the previous trace.
    ("backend", "Công việc đang làm bắt em phải chia thời gian giữa học và thực tập."),
    ("backend", "Nhà trường bắt em phải nộp giấy xác nhận."),
    ("backend", "Nhà trường backend phải nộp giấy xác nhận."),  # ASR already chose a valid technical word.
    ("React useEffect useRef", "Em dùng useEffect để giữ tham chiếu đến ô nhập liệu."),
    ("Next.js Node.js", "Em dùng node.js để xây dựng giao diện với React."),
    ("Redis", "Em có thử regex cho dữ liệu được đọc nhiều."),
    ("SQL MySQL", "Em viết HTML để kiểm tra dữ liệu."),
    ("Kubernetes", "Em mới tìm hiểu component."),
    ("PostgreSQL Vue.js", "Em thiết kế các bản bootstrap Vue để lưu câu hỏi."),
    ("PostgreSQL", "Em lưu lịch sử hội thoại trong database view."),
    ("backend API", "Em phụ trách PNBA bằng Spring Boot."),
    ("SSR", "Em có thử r-itt-r cho một trang sản phẩm."),
    ("unknown any", "Em dùng in in out làm kiểu dữ liệu."),
    ("unknown", "Em dùng in out cho input và output."),
    ("unknown", "Với in out em kiểm tra đầu vào và đầu ra."),
    ("unknown", "Em dùng inl để gửi báo cáo."),
    ("unknown", "Em dùng INL để kiểm tra kiểu."),
    ("unknown", "Em dùng tên biến inl làm kiểu dữ liệu."),
    ("unknown", "const inl = getData();"),
    ("unknown", "Em dùng `inl` làm kiểu dữ liệu."),
    ("NestJS Next.js", "Em dùng netJS để viết API."),
    ("NestJS", "Em dùng netJS với React."),
    ("NestJS Next.js", "Em dùng net js cùng React."),
    ("NestJS Next.js", "Em dùng netJS có controller và React."),
    ("NestJS Next.js", "Em dùng netJS nhưng chưa dùng React."),
    ("NestJS Next.js", "Em dùng netJS và không có controller."),
    ("NestJS Next.js", "Em dùng netJS không có controller và chưa dùng React."),
    ("NestJS Next.js", "Em dùng net.js để viết controller."),  # Also a possible filename.
    ("NestJS Next.js", "Em dùng tên package netJS để viết controller."),
    ("NestJS Next.js", "Em dùng `netJS` với app router."),
    ("useEffect", "Em dùng vue effect để đồng bộ dữ liệu."),  # Needs React evidence in candidate speech.
    ("useEffect React", "Em dùng vue effect trong component để đồng bộ dữ liệu."),
    ("useEffect React", "Em dùng vue effect nhưng chưa dùng React."),
    ("useEffect React", "Em dùng Vue để dựng component."),
    ("Spring Boot", "Em dùng spring bút là tên biến."),
    ("Spring Boot", "Em dùng `SpringBoot` làm tên package."),
    ("PostgreSQL", "Em dùng /tmp/postgre.sql để thử dữ liệu."),
    ("PostgreSQL", "We use postgre sql for our reports."),
    ("type guard", "Em dùng tên hàm type gard để kiểm tra kiểu."),
    ("API boundary", "Em dùng api bao đờ ri cho phần giao diện."),
]


@pytest.mark.parametrize("context,spoken,expected", REPAIRS)
def test_trace_variants_require_context_and_keep_original_offsets(context, spoken, expected):
    result = correct_technical_transcript(spoken, context=context)
    assert result.text == expected
    assert result.edits
    for edit in result.edits:
        assert spoken[edit.start : edit.end] == edit.original
    assert correct_technical_transcript(spoken).text == spoken
    assert correct_technical_transcript(spoken, context="Mức lương và lịch làm việc").text == spoken
    assert correct_technical_transcript(result.text, context=context).text == expected


@pytest.mark.parametrize("context,spoken", PRESERVE)
def test_valid_words_literals_and_ambiguous_outputs_survive(context, spoken):
    assert correct_technical_transcript(spoken, context=context).text == spoken


def test_interim_tail_waits_for_right_context_and_keeps_ordinary_completion():
    partial = "Em đang làm bắt em."
    result = correct_technical_transcript(partial, context="backend", is_final=False)
    assert result.text == partial
    assert result.decisions[0].reason == "unstable_interim_tail"
    continuation = "Em đang làm bắt em phải nộp giấy."
    assert correct_technical_transcript(continuation, context="backend", is_final=False).text == continuation
    assert correct_technical_transcript(continuation, context="backend").text == continuation
    assert correct_technical_transcript(partial, context="backend").text == "Em đang làm backend."


def test_interim_repairs_only_the_supported_prefix_not_an_incomplete_tail():
    raw = "Em dùng netJS có controller và dependency injection, em dùng inl"
    result = correct_technical_transcript(raw, context="NestJS Next.js unknown", is_final=False)
    assert result.text == "Em dùng NestJS có controller và dependency injection, em dùng inl"
    assert all(edit.original != "inl" for edit in result.edits)


def test_trace_unknown_occurrences_are_both_supported_without_global_replace():
    raw = (
        "Em dùng in out khi kiểu chưa rõ, với in out em kiểm tra thuộc tính. "
        "Em dùng in out cho input và output."
    )
    result = correct_technical_transcript(raw, context="TypeScript unknown any")
    assert (
        result.text == "Em dùng unknown khi kiểu chưa rõ, với unknown em kiểm tra thuộc tính. "
        "Em dùng in out cho input và output."
    )
    assert len(result.edits) == 2


def test_unicode_repairs_preserve_negation_numbers_and_unrelated_text():
    raw = unicodedata.normalize("NFD", "Em chưa dùng pốt gre sql năm 2026; lương 15 triệu.")
    result = correct_technical_transcript(raw, context="PostgreSQL")
    edit = result.edits[0]
    assert result.text == raw[: edit.start] + "PostgreSQL" + raw[edit.end :]


def test_language_normalizer_exposes_the_same_interim_policy():
    raw = "Em dùng postgre sql."
    assert normalize_transcript_for_locale(raw, locale="vi-VN", context="PostgreSQL", is_final=False) == raw
    assert normalize_transcript_for_locale(raw, locale="vi-VN", context="PostgreSQL") == "Em dùng PostgreSQL."
    assert normalize_transcript_for_locale(raw, locale="en-US", context="PostgreSQL") == raw
