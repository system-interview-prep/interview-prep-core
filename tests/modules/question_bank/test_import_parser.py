import io

import pytest
from openpyxl import load_workbook

from src.modules.question_bank.import_parser import (
    IMPORT_COLUMNS,
    TEMPLATE_COLUMNS,
    CatalogConcept,
    csv_template,
    parse_csv,
    parse_xlsx,
    rubric_criteria,
    xlsx_template,
)


def valid_row() -> str:
    values = {key: "x" for key in IMPORT_COLUMNS}
    values.update({
        "thinking_seconds": "0", "soft_answer_seconds": "60", "hard_answer_seconds": "120",
        "primary_competency_id": "comp", "target_role_ids": "[]",
        "supporting_competency_ids": "[]", "skill_ids": "[]", "expected_points": "[]",
        "source_refs": "[]", "question_type": "technical", "difficulty_band": "intermediate",
        "stable_key": "python.gil",
    })
    return ",".join(f'"{values[key]}"' for key in IMPORT_COLUMNS)


def test_template_has_canonical_header() -> None:
    assert csv_template().decode().splitlines()[0].split(",") == list(TEMPLATE_COLUMNS)


def _csv_with(header: tuple[str, ...], **overrides: str) -> bytes:
    import csv as csv_module

    values = {key: "x" for key in header}
    values.update({
        "thinking_seconds": "30", "soft_answer_seconds": "150", "hard_answer_seconds": "240",
        "primary_competency_id": "skill-redis", "target_role_ids": "[]",
        "supporting_competency_ids": "[]", "skill_ids": "[]", "expected_points": "[]",
        "source_refs": "[]", "question_type": "technical", "difficulty_band": "intermediate",
        "stable_key": "redis.cache", "rubric_key": "",
    })
    if "rubric_criteria" in header:
        values["rubric_criteria"] = "[]"
    values.update(overrides)
    stream = io.StringIO(newline="")
    writer = csv_module.DictWriter(stream, fieldnames=header)
    writer.writeheader()
    writer.writerow(values)
    return stream.getvalue().encode("utf-8")


def _criterion(key: str, weight: float) -> dict:
    return {
        "stableKey": key, "name": key, "description": key, "weight": weight,
        "anchors": [{"level": level, "description": f"m{level}"} for level in range(4)],
    }


def test_original_header_without_rubric_column_is_still_accepted() -> None:
    row = parse_csv(_csv_with(IMPORT_COLUMNS))[0]
    assert row.errors == []
    assert row.payload["rubric_criteria"] == []


def test_expected_points_are_normalized_for_storage() -> None:
    points = (
        '[{"description": " Đọc cache trước "}, '
        '{"description": "Chống stampede", "importance": "bonus"}, "Đặt TTL"]'
    )
    row = parse_csv(_csv_with(TEMPLATE_COLUMNS, expected_points=points))[0]
    assert row.errors == []
    assert row.payload["expected_points"] == [
        {"stable_key": "point-1", "description": "Đọc cache trước", "importance": "CORE"},
        {"stable_key": "point-2", "description": "Chống stampede", "importance": "BONUS"},
        {"stable_key": "point-3", "description": "Đặt TTL", "importance": "CORE"},
    ]


def test_invalid_expected_points_are_reported() -> None:
    points = '[{"description": ""}, {"description": "a", "importance": "CRITICAL"}]'
    codes = {e["code"] for e in parse_csv(_csv_with(TEMPLATE_COLUMNS, expected_points=points))[0].errors}
    assert codes == {"INVALID_EXPECTED_POINT", "INVALID_EXPECTED_POINT_IMPORTANCE"}


def test_inline_rubric_is_validated() -> None:
    import json

    good = json.dumps([_criterion("co-che", 0.5), _criterion("ttl", 0.3), _criterion("danh-doi", 0.2)])
    row = parse_csv(_csv_with(TEMPLATE_COLUMNS, rubric_criteria=good))[0]
    assert row.errors == []
    assert [c.stable_key for c in rubric_criteria(row.payload)] == ["co-che", "ttl", "danh-doi"]

    bad_weights = json.dumps([_criterion("a", 0.5), _criterion("b", 0.3)])
    codes = {e["code"] for e in parse_csv(_csv_with(TEMPLATE_COLUMNS, rubric_criteria=bad_weights))[0].errors}
    assert codes == {"RUBRIC_WEIGHTS_NOT_ONE"}

    three_anchors = [_criterion("a", 1.0)]
    three_anchors[0]["anchors"] = three_anchors[0]["anchors"][:3]
    row = parse_csv(_csv_with(TEMPLATE_COLUMNS, rubric_criteria=json.dumps(three_anchors)))[0]
    codes = {e["code"] for e in row.errors}
    assert codes == {"INVALID_RUBRIC"}

    one = json.dumps([_criterion("a", 1.0)])
    both = parse_csv(_csv_with(TEMPLATE_COLUMNS, rubric_key="rubric-x", rubric_criteria=one))[0]
    assert {e["code"] for e in both.errors} == {"RUBRIC_KEY_AND_CRITERIA"}


def test_csv_parser_reports_json_and_duration_errors() -> None:
    invalid = valid_row().replace('"120"', '"10"').replace('"[]"', '"[bad-json"', 1)
    content = (",".join(IMPORT_COLUMNS) + "\n" + invalid).encode()
    errors = parse_csv(content)[0].errors
    assert {error["code"] for error in errors} == {"INVALID_JSON", "HARD_BELOW_SOFT"}


def test_csv_parser_rejects_wrong_header() -> None:
    with pytest.raises(ValueError, match="header"):
        parse_csv(b"stable_key\nfoo\n")


def test_plain_list_cells_are_accepted_without_json() -> None:
    row = parse_csv(_csv_with(
        TEMPLATE_COLUMNS,
        skill_ids="skill-redis, skill-cache;skill-api",
        target_role_ids="",
        expected_points="CORE: Đọc cache trước\nbonus: Chống stampede\nĐặt TTL: theo dữ liệu",
        source_refs="https://redis.io/docs, phần 2\nhttps://example.com",
    ))[0]
    assert row.errors == []
    assert row.payload["skill_ids"] == ["skill-redis", "skill-cache", "skill-api"]
    assert row.payload["target_role_ids"] == []
    assert row.payload["expected_points"] == [
        {"stable_key": "point-1", "description": "Đọc cache trước", "importance": "CORE"},
        {"stable_key": "point-2", "description": "Chống stampede", "importance": "BONUS"},
        {"stable_key": "point-3", "description": "Đặt TTL: theo dữ liệu", "importance": "CORE"},
    ]
    assert row.payload["source_refs"] == ["https://redis.io/docs, phần 2", "https://example.com"]


def test_blank_optional_cells_take_defaults() -> None:
    row = parse_csv(_csv_with(
        TEMPLATE_COLUMNS, version="", canonical_locale="", taxonomy_version="",
        thinking_seconds="", soft_answer_seconds="", hard_answer_seconds="",
    ))[0]
    assert row.errors == []
    assert row.payload["version"] == "1.0.0"
    assert row.payload["canonical_locale"] == "vi-VN"
    seconds = ("thinking_seconds", "soft_answer_seconds", "hard_answer_seconds")
    assert [row.payload[key] for key in seconds] == [30, 150, 240]


def test_fractional_or_text_seconds_are_rejected() -> None:
    row = parse_csv(_csv_with(TEMPLATE_COLUMNS, soft_answer_seconds="60.5", thinking_seconds="abc"))[0]
    assert {(e["field"], e["code"]) for e in row.errors} == {
        ("soft_answer_seconds", "INVALID_INTEGER"),
        ("thinking_seconds", "INVALID_INTEGER"),
    }


def test_xlsx_template_guides_the_author_without_importing_examples() -> None:
    catalog = [
        CatalogConcept("skill-redis", "Redis", "skill"),
        CatalogConcept("role-backend", "Backend", "job_role"),
        CatalogConcept("comp-backend", "Backend engineering", "competency"),
    ]
    content = xlsx_template(catalog)
    workbook = load_workbook(io.BytesIO(content))
    assert workbook.sheetnames == ["Questions", "Hướng dẫn", "Ví dụ", "Danh mục"]
    # Competency/skill concepts come first so the primary dropdown covers exactly them.
    assert [row[0] for row in workbook["Danh mục"].iter_rows(min_row=2, values_only=True)] == [
        "comp-backend", "skill-redis", "role-backend",
    ]
    formulas = {dv.formula1 for dv in workbook["Questions"].data_validations.dataValidation}
    assert "'Danh mục'!$A$2:$A$3" in formulas
    assert workbook["Ví dụ"]["M2"].value == "skill-redis"  # primary_competency_id
    assert parse_xlsx(content) == []  # an untouched template imports nothing

    # The example rows are valid when pasted into the Questions sheet.
    for values in workbook["Ví dụ"].iter_rows(min_row=2, values_only=True):
        workbook["Questions"].append(values)
    stream = io.BytesIO()
    workbook.save(stream)
    parsed = parse_xlsx(stream.getvalue())
    assert [row.errors for row in parsed] == [[], []]
    assert parsed[1].payload["expected_points"][2]["importance"] == "BONUS"


def test_xlsx_template_and_parser_round_trip() -> None:
    workbook = load_workbook(io.BytesIO(xlsx_template()))
    assert workbook.sheetnames == ["Questions", "Hướng dẫn", "Ví dụ", "Danh mục"]
    worksheet = workbook["Questions"]
    worksheet.append([
        "Key", "1", "vi-VN", "Question", "Objective", "CONCEPTUAL", "Intermediate", 0, 60,
        120, "v1", "[]", "comp", "[]", "[]", "[]", "[]", "rubric", "",
    ])
    stream = io.BytesIO()
    workbook.save(stream)
    parsed = parse_xlsx(stream.getvalue())
    assert parsed[0].payload["stable_key"] == "key"
    assert parsed[0].payload["question_type"] == "conceptual"
    assert parsed[0].payload["difficulty_band"] == "intermediate"
    assert parsed[0].errors == []


def test_csv_parser_rejects_vocabulary_the_selector_cannot_match() -> None:
    """P2 filters out any difficulty outside foundational/intermediate/advanced."""
    invalid = valid_row().replace('"intermediate"', '"MEDIUM"').replace('"technical"', '"QUIZ"')
    content = (",".join(IMPORT_COLUMNS) + "\n" + invalid).encode()
    errors = parse_csv(content)[0].errors
    assert {(error["field"], error["code"]) for error in errors} == {
        ("difficulty_band", "INVALID_VALUE"),
        ("question_type", "INVALID_VALUE"),
    }
