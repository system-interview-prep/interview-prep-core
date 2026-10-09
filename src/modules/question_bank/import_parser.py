"""Pure CSV import parsing for Question Bank staging."""

import csv
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass

from openpyxl import Workbook, load_workbook

from src.modules.question_bank.schemas import (
    DEFAULT_TAXONOMY_VERSION,
    DIFFICULTY_BANDS,
    QUESTION_TYPES,
)

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
    instructions.append([f"question_type: one of {', '.join(QUESTION_TYPES)}."])
    instructions.append([f"difficulty_band: one of {', '.join(DIFFICULTY_BANDS)}."])
    instructions.append([
        f"taxonomy_version defaults to {DEFAULT_TAXONOMY_VERSION}. skill_ids map as TARGET_SKILL, "
        "target_role_ids (career codes) as TARGET_ROLE. rubric_key names an existing rubric."
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
        normalize_row_payload(payload)
        errors.extend(error for error in validate_row_payload(payload) if error not in errors)
        parsed.append(ParsedImportRow(row_number, payload, errors))
    return parsed


def normalize_row_payload(payload: dict) -> None:
    """Apply the same normalisation the authoring API applies."""
    payload["stable_key"] = str(payload.get("stable_key") or "").strip().lower()
    payload["version"] = str(payload.get("version") or "").strip() or "1.0.0"
    taxonomy_version = str(payload.get("taxonomy_version") or "").strip()
    payload["taxonomy_version"] = taxonomy_version or DEFAULT_TAXONOMY_VERSION
    for key in ("question_type", "difficulty_band"):
        payload[key] = str(payload.get(key) or "").strip().lower()


def validate_row_payload(payload: dict) -> list[dict]:
    """Semantic row checks shared by staging, row patches and commit."""
    errors: list[dict] = []

    def error(field: str, code: str) -> None:
        errors.append({"field": field, "code": code, "severity": "ERROR"})

    for key in ("stable_key", "canonical_locale", "canonical_text", "objective", "primary_competency_id"):
        if not str(payload.get(key) or "").strip():
            error(key, "REQUIRED")
    if not 3 <= len(str(payload.get("stable_key") or "")) <= 180:
        error("stable_key", "INVALID_LENGTH")
    if len(str(payload.get("version") or "")) > 32:
        error("version", "INVALID_LENGTH")
    if payload.get("question_type") not in QUESTION_TYPES:
        error("question_type", "INVALID_VALUE")
    if payload.get("difficulty_band") not in DIFFICULTY_BANDS:
        error("difficulty_band", "INVALID_VALUE")
    for key in JSON_COLUMNS:
        if not isinstance(payload.get(key, []), list):
            error(key, "INVALID_JSON")
    seconds = {}
    for key in ("thinking_seconds", "soft_answer_seconds", "hard_answer_seconds"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            error(key, "INVALID_INTEGER")
        else:
            seconds[key] = value
    if (
        "soft_answer_seconds" in seconds
        and "hard_answer_seconds" in seconds
        and seconds["hard_answer_seconds"] < seconds["soft_answer_seconds"]
    ):
        error("hard_answer_seconds", "HARD_BELOW_SOFT")
    return errors
