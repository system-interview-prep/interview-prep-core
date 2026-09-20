"""Pure CSV import parsing for Question Bank staging."""

import csv
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass

from openpyxl import Workbook, load_workbook

IMPORT_COLUMNS = (
    "stable_key", "version", "canonical_locale", "canonical_text", "objective",
    "question_type", "difficulty_band", "thinking_seconds", "soft_answer_seconds",
    "hard_answer_seconds", "taxonomy_version", "target_role_ids",
    "primary_competency_id", "supporting_competency_ids", "skill_ids",
    "expected_points", "source_refs", "rubric_key", "change_summary",
)
JSON_COLUMNS = {"target_role_ids", "supporting_competency_ids", "skill_ids", "expected_points", "source_refs"}


@dataclass(frozen=True)
class ParsedImportRow:
    row_number: int
    payload: dict
    errors: list[dict]


def csv_template() -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=IMPORT_COLUMNS)
    writer.writeheader()
    return stream.getvalue().encode("utf-8")


def xlsx_template() -> bytes:
    workbook = Workbook()
    questions = workbook.active
    questions.title = "Questions"
    questions.append(IMPORT_COLUMNS)
    instructions = workbook.create_sheet("Instructions")
    instructions.append(["Question Bank Import v1"])
    instructions.append([
        "Use JSON arrays for target_role_ids, supporting_competency_ids, skill_ids, "
        "expected_points and source_refs."
    ])
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def parse_csv(content: bytes) -> list[ParsedImportRow]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must use UTF-8 encoding.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None or tuple(reader.fieldnames) != IMPORT_COLUMNS:
        raise ValueError("CSV header does not match question-bank import template.")
    return _parse_rows(enumerate(reader, start=2))


def parse_xlsx(content: bytes) -> list[ParsedImportRow]:
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except Exception as exc:
        raise ValueError("Invalid XLSX workbook.") from exc
    if "Questions" not in workbook.sheetnames:
        raise ValueError("XLSX must include a Questions worksheet.")
    worksheet = workbook["Questions"]
    header = tuple(cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1)))
    if header != IMPORT_COLUMNS:
        raise ValueError("XLSX header does not match question-bank import template.")
    rows = (
        (row_number, dict(zip(IMPORT_COLUMNS, values, strict=True)))
        for row_number, values in enumerate(worksheet.iter_rows(min_row=2, values_only=True), start=2)
    )
    return _parse_rows(rows)


def _parse_rows(rows: Iterable[tuple[int, dict]]) -> list[ParsedImportRow]:
    parsed: list[ParsedImportRow] = []
    for row_number, raw in rows:
        if not any(str("" if value is None else value).strip() for value in raw.values()):
            continue
        payload = {key: str("" if value is None else value).strip() for key, value in raw.items()}
        errors: list[dict] = []
        for key in JSON_COLUMNS:
            try:
                payload[key] = json.loads(payload[key] or "[]")
            except json.JSONDecodeError:
                errors.append({"field": key, "code": "INVALID_JSON", "severity": "ERROR"})
        for key in ("thinking_seconds", "soft_answer_seconds", "hard_answer_seconds"):
            try:
                payload[key] = int(payload[key])
            except ValueError:
                errors.append({"field": key, "code": "INVALID_INTEGER", "severity": "ERROR"})
        has_invalid_duration = (
            isinstance(payload["soft_answer_seconds"], int)
            and isinstance(payload["hard_answer_seconds"], int)
            and payload["hard_answer_seconds"] < payload["soft_answer_seconds"]
        )
        if has_invalid_duration:
            errors.append({"field": "hard_answer_seconds", "code": "HARD_BELOW_SOFT", "severity": "ERROR"})
        if not payload["primary_competency_id"]:
            errors.append({"field": "primary_competency_id", "code": "REQUIRED", "severity": "ERROR"})
        parsed.append(ParsedImportRow(row_number, payload, errors))
    return parsed
