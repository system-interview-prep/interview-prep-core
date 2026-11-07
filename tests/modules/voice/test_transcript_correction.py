"""Independent sentence-level regression set, not generated from the alias table.

Synthetic minimal pairs exercise false repairs even when the prompt explicitly
mentions the tempting technology. Trace regressions retain uncertain words.
These are text correction tests, not an acoustic accuracy/WER benchmark.
"""

import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from src.modules.interviews.adapters.voice.language import normalize_transcript_for_locale
from src.modules.interviews.adapters.voice.transcript_correction import correct_technical_transcript

# context, candidate ASR, approved repaired sentence
REPAIRS = [
    ("Backend Developer", "Em làm bắt em Developer.", "Em làm backend Developer."),
    ("backend", "Em ứng tuyển vị trí bắc em.", "Em ứng tuyển vị trí backend."),
    ("backend", "Em làm bắt en API.", "Em làm backend API."),
    ("useState", "Em dùng diu xét để giữ state.", "Em dùng useState để giữ state."),
    ("useState", "Em dùng you state để cập nhật giao diện.", "Em dùng useState để cập nhật giao diện."),
    ("useState", "Em sử dụng use state trong component.", "Em sử dụng useState trong component."),
    ("useEffect", "Em gọi diu effect để đồng bộ dữ liệu.", "Em gọi useEffect để đồng bộ dữ liệu."),
    ("useEffect", "Em dùng you effect khi gọi API.", "Em dùng useEffect khi gọi API."),
    ("useEffect", "Em dùng use effect để đăng ký sự kiện.", "Em dùng useEffect để đăng ký sự kiện."),
    ("useRef", "Em dùng use review để giữ DOM ref.", "Em dùng useRef để giữ DOM ref."),
    ("useRef", "Em dùng use rếp để giữ tham chiếu.", "Em dùng useRef để giữ tham chiếu."),
    ("useReducer", "Em dùng use reduce cho state phức tạp.", "Em dùng useReducer cho state phức tạp."),
    ("Redux", "Em dùng rì đắc để quản lý state chung.", "Em dùng Redux để quản lý state chung."),
    ("Redux", "Em chọn ri đắc cho ứng dụng.", "Em chọn Redux cho ứng dụng."),
    ("React", "Em học rì ác để viết giao diện.", "Em học React để viết giao diện."),
    ("React", "Em dùng react js trong dự án.", "Em dùng React.js trong dự án."),
    ("Vue.js", "Em sử dụng víu chây ét cho trang quản trị.", "Em sử dụng Vue.js cho trang quản trị."),
    ("VueJS", "Em học view js để làm giao diện.", "Em học Vue.js để làm giao diện."),
    ("Vue", "Em dùng vue js cho dự án.", "Em dùng Vue.js cho dự án."),
    ("Node.js", "Em dùng nốt chây ét ở server.", "Em dùng Node.js ở server."),
    ("NodeJS", "Em chạy node js ở server.", "Em chạy Node.js ở server."),
    ("SQL", "Em học éc quy eo để truy vấn.", "Em học SQL để truy vấn."),
    ("ORM", "Em dùng ô a em cho tầng truy cập dữ liệu.", "Em dùng ORM cho tầng truy cập dữ liệu."),
    ("API", "Em triển khai a pi ai cho ứng dụng.", "Em triển khai API cho ứng dụng."),
    ("Redis", "Em dùng rê đít để cache.", "Em dùng Redis để cache."),
    ("Docker", "Em cài đặt đô cơ trên máy.", "Em cài đặt Docker trên máy."),
    (
        "Kubernetes",
        "Em triển khai cu bơ nét để quản lý container.",
        "Em triển khai Kubernetes để quản lý container.",
    ),
    ("async/await", "Em dùng a sin a quây để xử lý lỗi.", "Em dùng async/await để xử lý lỗi."),
    ("async / await", "Em dùng async await khi gọi API.", "Em dùng async/await khi gọi API."),
    (
        "Dependency Injection",
        "Em dùng đi pen đen xi in chéc sần để truyền service.",
        "Em dùng Dependency Injection để truyền service.",
    ),
    ("TypeScript", "Em học type script để kiểm tra kiểu.", "Em học TypeScript để kiểm tra kiểu."),
    ("JavaScript", "Em viết bằng java script cho frontend.", "Em viết bằng JavaScript cho frontend."),
    ("MongoDB", "Em dùng mongo db để lưu tài liệu.", "Em dùng MongoDB để lưu tài liệu."),
    ("MySQL", "Em sử dụng my sql để lưu đơn hàng.", "Em sử dụng MySQL để lưu đơn hàng."),
    (
        "unknown thay vì any",
        "Em dùng in out làm kiểu cho response.",
        "Em dùng unknown làm kiểu cho response.",
    ),
    (
        "unknown",
        "Kiểu in out không cho phép gọi thuộc tính ngay.",
        "Kiểu unknown không cho phép gọi thuộc tính ngay.",
    ),
    ("NestJS", "Em làm với nét chây ét để viết controller.", "Em làm với NestJS để viết controller."),
    ("Next.js", "Em dùng nét chây ét với app router.", "Em dùng Next.js với app router."),
    ("NestJS Next.js", "Em dùng nét chây ét cùng pages router.", "Em dùng Next.js cùng pages router."),
    (
        "useState useEffect Redux",
        "Em dùng diu xét, you effect và rì đắc.",
        "Em dùng useState, useEffect và Redux.",
    ),
    ("React Node.js", "Em dùng react js và node js.", "Em dùng React.js và Node.js."),
    (
        "Redis",
        "Em chưa dùng rê đít; em dùng MySQL, năm 2025.",
        "Em chưa dùng Redis; em dùng MySQL, năm 2025.",
    ),
    (
        "unknown any",
        "Em dùng in out khi kiểu dữ liệu chưa rõ, khác với in in out.",
        "Em dùng unknown khi kiểu dữ liệu chưa rõ, khác với in in out.",
    ),
]

# These sentences must survive exactly, despite a technology-biased prompt.
PRESERVE = [
    ("NestJS Next.js", "Em dùng nét chây ét cùng React."),
    ("Backend Developer", "Nhà trường bắt em phải nộp giấy xác nhận."),
    ("Backend Developer", "Vị trí bắt em phải làm thêm giờ."),
    ("Backend Developer", "Công việc đang làm bắt em phải tăng ca."),
    ("Backend Developer", "Em đang làm đồ án, nhà trường bắt em phải nộp giấy."),
    ("Backend Developer", "Em bắt em đi làm rồi."),
    ("Backend Developer", "Công ty bắt em developer phải nộp giấy."),
    ("useState", "When you state the requirements, I write tests."),
    ("useState", "Em dùng React, when you state requirements, we write tests."),
    ("useState", "Em dùng you state the requirements clearly trong ví dụ."),
    ("useState", "Em dùng you state the requirements clearly."),
    ("MySQL", "Em dùng React, my SQL query returns ten rows."),
    ("MySQL", "Em dùng my SQL query returns ten rows."),
    ("useState", "We use state machines to model workflows."),
    ("useState", "Em dùng use state machine để mô hình hóa nghiệp vụ."),
    ("useEffect", "You effect a change by updating the configuration."),
    ("useRef", "We use review comments to improve code."),
    ("useRef", "Em dùng React, we use review comments to improve code."),
    ("useRef", "Em dùng use review để review code cho pull request."),
    ("useRef", "Em đặt tên hàm là use review."),
    ("useReducer", "We use reduce to calculate totals."),
    ("useReducer", "Em dùng use reduce để tính tổng mảng."),
    ("useReducer", "Em dùng use reduce trên array."),
    ("Redux", "Ri Đắc là tên người bạn em."),
    ("Redux", "Rì Đắc là bạn cùng nhóm em."),
    ("React", "Rì Ác là đồng nghiệp của em."),
    ("Redis", "Em làm việc cùng anh Rê Đít."),
    ("API", "A pi ai là mã khách hàng em đang xử lý."),
    ("useState", "Em học you state trong bài ngữ pháp tiếng Anh."),
    ("React", "Em ghi nguyên văn cụm từ rì ác."),
    ("Vue.js", "Em lưu chuỗi víu chây ét trong dữ liệu kiểm thử."),
    ("Node.js", "Em dùng tên biến nốt chây ét trong mô tả bài tập."),
    ("SQL", "Em lưu chuỗi éc quy eo vào log."),
    ("ORM", "Em ghi nguyên văn ô a em trong tài liệu."),
    ("API", "Em ghi tên khách hàng là a pi ai."),
    ("Redis", "Em đặt biệt danh là rê đít."),
    ("Docker", "Đô Cơ là tên người bạn học của em."),
    ("Kubernetes", "Em lưu chuỗi cu bơ nét để so sánh kết quả STT."),
    ("async/await", "Em nhắc nguyên văn a sin a quây để thử microphone."),
    ("Dependency Injection", "Em ghi nguyên văn đi pen đen xi in chéc sần."),
    ("TypeScript", "I type script commands in the terminal."),
    ("JavaScript", "The Java script was generated by another tool."),
    ("MySQL", "My SQL query returns ten rows."),
    ("MongoDB", "Em dùng tên biến mongo db trong mô tả."),
    ("VIN University", "Virginie là tên người bạn cùng nhóm em."),
    ("VIN University", "Em tham gia dự án Virginie á."),
    ("VIN University", "Em làm với Virginie trong dự án."),
    ("unknown any", "Em dùng in out để xử lý input và output."),
    ("unknown any", "Em dùng in out cho đầu vào và đầu ra."),
    ("unknown any", "Em dùng in out."),
    ("unknown any", "Em dùng in in out để kiểm tra kiểu."),
    ("NestJS và Next.js", "Em dùng nét chây ét cho dự án."),
    ("NestJS", "Em dùng nét chây ét cùng app router."),
    ("Next.js", "Em dùng nét chây ét để viết controller."),
    ("Next.js", "Em dùng nét chây ét cho dự án."),
    ("NestJS Next.js", "Em dùng nét chây ét với React và controller."),
    ("Next.js", "Em dùng nét chây ét với React và controller."),
    ("NextJS", "Nét chây ét là biệt danh của bạn em."),
    ("useState", "Em dùng useState rồi. Nhưng you state là cụm từ trong bài tiếng Anh."),
    ("Redis", "Em dùng MySQL. rê đít."),
    ("useState", "Em dùng React; you state."),
    ("Docker", "Em dùng Redis nhưng đô cơ là biệt danh của một người."),
    ("useState", "const you state = getValue();"),
    ("useState", "Em dùng const you state = getValue(); để kiểm thử."),
    ("Redis", "Em dùng RMI để kết nối hệ thống."),
    ("Redis React Redux", "Em dùng ReactRouter v7, Redisson, Rocket và RMI."),
    ("useState", "Em dùng you statements để đặt ví dụ."),
    ("API", "Em nhận mức lương 15 triệu, không phải 50 triệu."),
    ("unknown any", "Em không biết, em chưa sử dụng hai kiểu đó."),
]


@pytest.mark.parametrize(("context", "spoken", "expected"), REPAIRS, ids=[r[1] for r in REPAIRS])
def test_supported_repairs(context: str, spoken: str, expected: str) -> None:
    result = correct_technical_transcript(spoken, context=context)
    assert result.text == expected
    assert result.edits
    for edit in result.edits:
        assert spoken[edit.start : edit.end] == edit.original
        assert edit.reason == "supported_usage"


@pytest.mark.parametrize(("context", "spoken"), PRESERVE, ids=[r[1] for r in PRESERVE])
def test_correct_or_ambiguous_speech_is_preserved(context: str, spoken: str) -> None:
    result = correct_technical_transcript(spoken, context=context)
    assert result.text == spoken
    assert not result.edits


@pytest.mark.parametrize(("context", "spoken", "expected"), REPAIRS)
def test_missing_or_wrong_context_does_not_invent_terms(context: str, spoken: str, expected: str) -> None:
    assert correct_technical_transcript(spoken).text == spoken
    assert correct_technical_transcript(spoken, context="Bạn nói về lương và kỳ vọng nhé?").text == spoken


@pytest.mark.parametrize(("context", "spoken", "expected"), REPAIRS)
def test_repairs_are_idempotent_and_preserve_canonical_spelling(
    context: str, spoken: str, expected: str
) -> None:
    first = correct_technical_transcript(spoken, context=context)
    assert correct_technical_transcript(first.text, context=context).text == first.text
    assert correct_technical_transcript(expected, context=context).text == expected


@pytest.mark.parametrize("wrapper", ["`{}`", '"{}"', "'{}'", "“{}”", "```typescript\n{}\n```"])
def test_quoted_literals_and_code_are_not_rewritten(wrapper: str) -> None:
    for alias, context in (("use review", "useRef"), ("bắt em", "backend"), ("in out", "unknown")):
        text = "Em dùng " + wrapper.format(alias) + " làm dữ liệu kiểm thử."
        assert correct_technical_transcript(text, context=context).text == text


@pytest.mark.parametrize(
    "text",
    [
        "Em dùng /tmp/my sql/schema.sql để kiểm thử.",
        "Em dùng https://example.org/you%20state và you_state@company.org.",
        "Em dùng use_review làm tên biến, và SQLStore để truy vấn.",
    ],
)
def test_identifiers_paths_and_urls_are_preserved(text: str) -> None:
    assert correct_technical_transcript(text, context="MySQL useState useRef SQL").text == text


def test_only_original_alias_span_changes_in_unicode_and_whitespace_variants() -> None:
    original = unicodedata.normalize("NFD", "Em chưa dùng RÊ ĐÍT ở dự án 2026; mức lương 15 triệu.")
    result = correct_technical_transcript(original, context="Redis")
    assert len(result.edits) == 1
    edit = result.edits[0]
    assert result.text == original[: edit.start] + "Redis" + original[edit.end :]
    assert unicodedata.normalize("NFC", edit.original) == "RÊ ĐÍT"
    assert correct_technical_transcript("Em dùng a\tpi\nai để gọi service.", context="API").text == (
        "Em dùng a\tpi\nai để gọi service."
    )  # Crossing a sentence/fragment boundary is not evidence to repair.


def test_non_vietnamese_locale_never_rewrites_real_english() -> None:
    text = "You state the requirements, then we use review comments."
    assert normalize_transcript_for_locale(text, locale="en-US", context="useState useRef") == text


@pytest.mark.parametrize(
    "text",
    [
        "Yeah, ạ.",
        "Yes 2",
        "Hello world là chuỗi kiểm thử.",
        "Hello là tên hàm của em.",
        "Đây là HelloKitty, không phải lời chào.",
        "yes/no là hai giá trị.",
    ],
)
def test_filler_normalization_does_not_delete_words_or_numbers(text: str) -> None:
    assert normalize_transcript_for_locale(text, locale="vi-VN") == text


def test_homophone_abstention_is_explainable_and_never_partially_repaired() -> None:
    result = correct_technical_transcript("Em dùng in in out làm kiểu dữ liệu.", context="unknown any")
    assert result.text == "Em dùng in in out làm kiểu dữ liệu."
    assert len(result.decisions) == 1
    assert result.decisions[0].reason == "unresolved_homophone"


@pytest.mark.parametrize(
    ("context", "text", "reason"),
    [
        ("Redis", "Em dùng `rê đít` để thử.", "protected_text"),
        ("useState", "Em dùng tên biến you state.", "literal_or_name"),
        ("useState", "const you state = getValue();", "code_or_identifier"),
        ("backend", "Vị trí bắt em phải làm việc thêm.", "ordinary_vietnamese"),
        ("backend", "Nhà trường bắt em nộp giấy.", "ordinary_vietnamese"),
        ("VIN University", "Em tham gia dự án Virginie.", "proper_name_requires_confirmation"),
        ("unknown", "Em dùng in out cho input và output.", "input_output_meaning"),
        ("unknown", "Em dùng in out.", "insufficient_type_evidence"),
        ("useState", "Em dùng use state machine.", "state_machine_meaning"),
        ("useRef", "Em dùng use review để review code.", "review_meaning"),
        ("useReducer", "Em dùng use reduce cho mảng.", "array_reduce_meaning"),
        ("useState", "When you state requirements, we test.", "possible_english_sentence"),
        ("Redis", "Em thích món rê đít.", "insufficient_local_evidence"),
        ("SQL", "Em dùng rê đít.", "missing_context"),
        ("NestJS Next.js", "Em dùng nét chây ét.", "ambiguous_context"),
        ("Next.js", "Em dùng nét chây ét.", "insufficient_disambiguation"),
        ("Redis", "Rê Đít là bạn cùng nhóm em.", "ordinary_entity_meaning"),
        ("any unknown", "Em dùng in in out làm kiểu.", "unresolved_homophone"),
    ],
)
def test_abstention_records_the_actual_reason(context: str, text: str, reason: str) -> None:
    result = correct_technical_transcript(text, context=context)
    assert not result.edits
    assert len(result.decisions) == 1
    assert result.decisions[0].reason == reason


def test_empty_and_non_vietnamese_results_have_no_proposals() -> None:
    assert correct_technical_transcript("").decisions == ()
    assert (
        correct_technical_transcript("We use review comments", context="useRef", locale="en").decisions == ()
    )


def test_case_and_spacing_repairs_do_not_change_the_rest_of_the_sentence() -> None:
    assert correct_technical_transcript("EM DÙNG RÊ ĐÍT CHO API.", context="Redis").text == (
        "EM DÙNG Redis CHO API."
    )
    assert correct_technical_transcript("Em dùng a   pi\tai để gọi service.", context="API").text == (
        "Em dùng API để gọi service."
    )


def test_similar_technology_names_do_not_authorize_a_different_term() -> None:
    context = "Bạn dùng SQLAlchemy, useStateful, ReactRouter, backendless và NextJSTool?"
    spoken = "Em dùng éc quy eo, you state, rì ác, bắt em Developer và nét chây ét."
    assert correct_technical_transcript(spoken, context=context).text == spoken


def test_standalone_voice_worker_import_uses_the_same_filter() -> None:
    adapter_dir = Path(__file__).parents[3] / "src/modules/interviews/adapters/voice"
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); import language; "
        "assert language.normalize_transcript_for_locale("
        "'Em d\\u00f9ng use state.', locale='vi-VN', context='useState') == 'Em d\\u00f9ng useState.'; "
        "assert language.normalize_transcript_for_locale("
        "'When you state requirements.', locale='en-US', context='useState') "
        "== 'When you state requirements.'"
    )
    subprocess.run([sys.executable, "-I", "-c", script, str(adapter_dir)], check=True, capture_output=True)
