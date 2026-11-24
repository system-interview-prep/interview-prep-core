"""Pure CSV import parsing for Question Bank staging."""
# ruff: noqa: E501

import csv
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from pydantic import ValidationError

from src.modules.question_bank.schemas import (
    DEFAULT_TAXONOMY_VERSION,
    DIFFICULTY_BANDS,
    QUESTION_TYPES,
    RubricCriterionInput,
)

IMPORT_COLUMNS = (
    "stable_key", "version", "canonical_locale", "canonical_text", "objective",
    "question_type", "difficulty_band", "thinking_seconds", "soft_answer_seconds",
    "hard_answer_seconds", "taxonomy_version", "target_role_ids",
    "primary_competency_id", "supporting_competency_ids", "skill_ids",
    "expected_points", "source_refs", "rubric_key", "change_summary",
)
# Optional trailing columns: a file with the original 19-column header stays valid.
OPTIONAL_COLUMNS = ("rubric_criteria",)
TEMPLATE_COLUMNS = (*IMPORT_COLUMNS, *OPTIONAL_COLUMNS)
JSON_COLUMNS = {
    "target_role_ids", "supporting_competency_ids", "skill_ids", "expected_points", "source_refs",
    "rubric_criteria",
}
# Inline rubric criteria are nested objects: only JSON can describe them.
JSON_ONLY_COLUMNS = {"rubric_criteria"}
EXPECTED_POINT_IMPORTANCE = ("CORE", "BONUS")
MAX_EXPECTED_POINTS = 12
_WEIGHT_TOLERANCE = Decimal("0.0001")
TEMPLATE_LOCALES = ("vi-VN", "en-US")
DEFAULT_LOCALE = "vi-VN"
# Used when an author leaves a duration blank.
DEFAULT_SECONDS = {"thinking_seconds": 30, "soft_answer_seconds": 150, "hard_answer_seconds": 240}
REQUIRED_COLUMNS = ("stable_key", "canonical_text", "objective", "question_type", "difficulty_band", "primary_competency_id")
# Template sheet names: only "Questions" is read back on upload.
QUESTIONS_SHEET = "Questions"
GUIDE_SHEET = "Hướng dẫn"
EXAMPLE_SHEET = "Ví dụ"
CATALOG_SHEET = "Danh mục"
# Concept kinds each id column accepts (mirrors QuestionBankService._MAPPING_KINDS).
CATALOG_COLUMNS = {
    "competency": "primary_competency_id, supporting_competency_ids, target_role_ids",
    "skill": "primary_competency_id, skill_ids",
    "job_role": "target_role_ids",
}
_PRIMARY_KINDS = ("competency", "skill")

# (column, how to fill it, example) for the guide sheet and the header comments.
_COLUMN_GUIDE = {
    "stable_key": ("Mã riêng của câu hỏi, chữ thường không dấu, nối bằng dấu gạch ngang. Không trùng với câu đã có, kể cả câu đã gỡ.", "redis-cache-api-01"),
    "version": ("Phiên bản. Để trống sẽ là 1.0.0.", "1.0.0"),
    "canonical_locale": ("Ngôn ngữ của câu hỏi. Chọn trong danh sách. Để trống sẽ là vi-VN.", "vi-VN"),
    "canonical_text": ("Nội dung câu hỏi hỏi ứng viên.", "Bạn dùng Redis làm cache cho API như thế nào?"),
    "objective": ("Câu hỏi này kiểm tra điều gì.", "Hiểu cache-aside, TTL và vô hiệu hóa cache"),
    "question_type": (f"Loại câu hỏi. Chọn trong danh sách: {', '.join(QUESTION_TYPES)}.", "technical"),
    "difficulty_band": (f"Độ khó. Chọn trong danh sách: {', '.join(DIFFICULTY_BANDS)}.", "intermediate"),
    "thinking_seconds": (f"Số giây suy nghĩ. Để trống sẽ là {DEFAULT_SECONDS['thinking_seconds']}.", "30"),
    "soft_answer_seconds": (f"Thời gian trả lời gợi ý (giây). Để trống sẽ là {DEFAULT_SECONDS['soft_answer_seconds']}.", "150"),
    "hard_answer_seconds": (f"Thời gian trả lời tối đa (giây), không nhỏ hơn thời gian gợi ý. Để trống sẽ là {DEFAULT_SECONDS['hard_answer_seconds']}.", "240"),
    "taxonomy_version": (f"Bộ danh mục kỹ năng. Để trống sẽ là {DEFAULT_TAXONOMY_VERSION}.", ""),
    "target_role_ids": (f"Mã vị trí/năng lực nhắm tới, cách nhau bằng dấu phẩy. Xem sheet {CATALOG_SHEET}.", ""),
    "primary_competency_id": (f"Mã năng lực/kỹ năng chính. Chọn trong danh sách (sheet {CATALOG_SHEET}).", ""),
    "supporting_competency_ids": ("Mã năng lực hỗ trợ, cách nhau bằng dấu phẩy.", ""),
    "skill_ids": (f"Mã kỹ năng liên quan, cách nhau bằng dấu phẩy. Xem sheet {CATALOG_SHEET}.", ""),
    "expected_points": ("Các ý ứng viên cần nêu, mỗi ý một dòng (Alt+Enter để xuống dòng). Ghi CORE: cho ý bắt buộc, BONUS: cho ý cộng điểm; không ghi thì là CORE. Tối đa 12 ý.", "CORE: Đọc cache trước, trượt thì đọc DB\nBONUS: Chống cache stampede"),
    "source_refs": ("Nguồn tham khảo, mỗi nguồn một dòng. Có thể để trống.", "https://redis.io/docs/"),
    "rubric_key": ("Mã rubric có sẵn để chấm điểm. Có thể để trống và gắn rubric sau trong trang câu hỏi, trước khi gửi duyệt.", ""),
    "change_summary": ("Ghi chú ngắn về câu hỏi. Có thể để trống.", "Tạo mới"),
    "rubric_criteria": ("Dành cho người kỹ thuật: rubric viết dạng JSON. Thường để trống.", ""),
}


@dataclass(frozen=True)
class ParsedImportRow:
    row_number: int
    payload: dict
    errors: list[dict]


def csv_template() -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=TEMPLATE_COLUMNS)
    writer.writeheader()
    return stream.getvalue().encode("utf-8")


@dataclass(frozen=True)
class CatalogConcept:
    concept_id: str
    label: str
    kind: str


_HEADER_FONT = Font(bold=True)
_REQUIRED_FILL = PatternFill("solid", fgColor="FFF2CC")
_OPTIONAL_FILL = PatternFill("solid", fgColor="EAEFF8")
_WRAP = Alignment(wrap_text=True, vertical="top")
_TEMPLATE_ROWS = 1000


def xlsx_template(catalog: Iterable[CatalogConcept] = ()) -> bytes:
    """Workbook an author fills without knowing the import format.

    Only the "Questions" sheet is read back; the guide, the examples and the
    concept catalogue (from the active taxonomy) live on their own sheets so
    they are never imported by mistake.
    """
    catalog = sorted(catalog, key=lambda item: (item.kind not in _PRIMARY_KINDS, item.kind, item.label.lower()))
    workbook = Workbook()
    questions = workbook.active
    questions.title = QUESTIONS_SHEET
    _write_header(questions)
    for column in TEMPLATE_COLUMNS:
        cell = questions.cell(row=1, column=TEMPLATE_COLUMNS.index(column) + 1)
        cell.comment = Comment(_COLUMN_GUIDE[column][0], "Intervia")
    _add_dropdowns(questions, catalog)

    guide = workbook.create_sheet(GUIDE_SHEET)
    guide.append(["Cách nhập câu hỏi từ tệp"])
    guide["A1"].font = Font(bold=True, size=14)
    for step in (
        f"1. Điền ở sheet {QUESTIONS_SHEET}, mỗi câu hỏi một dòng, bắt đầu từ dòng 2. Không đổi tên sheet và không sửa dòng tiêu đề.",
        "2. Cột tô vàng là bắt buộc. Cột tô xanh có thể để trống, hệ thống tự điền giá trị mặc định.",
        f"3. Xem sheet {EXAMPLE_SHEET} để biết một dòng hoàn chỉnh; sheet đó không được nhập.",
        f"4. Mã năng lực, kỹ năng, vị trí lấy ở sheet {CATALOG_SHEET}.",
        "5. Lưu tệp với tên tùy ý, rồi tải lên ở trang Nhập câu hỏi từ tệp. Câu hỏi được tạo ở dạng bản nháp; mở từng câu và bấm Gửi duyệt.",
    ):
        guide.append([step])
    guide.append([])
    guide.append(["Cột", "Bắt buộc", "Cách điền", "Ví dụ"])
    for cell in guide[guide.max_row]:
        cell.font = _HEADER_FONT
    for column in TEMPLATE_COLUMNS:
        how, example = _COLUMN_GUIDE[column]
        guide.append([column, "Có" if column in REQUIRED_COLUMNS else "Không", how, example])
        for cell in guide[guide.max_row]:
            cell.alignment = _WRAP
    for letter, width in (("A", 28), ("B", 10), ("C", 80), ("D", 45)):
        guide.column_dimensions[letter].width = width

    examples = workbook.create_sheet(EXAMPLE_SHEET)
    _write_header(examples)
    for row in _example_rows(catalog):
        examples.append([row.get(column, "") for column in TEMPLATE_COLUMNS])
        for cell in examples[examples.max_row]:
            cell.alignment = _WRAP

    concepts = workbook.create_sheet(CATALOG_SHEET)
    concepts.append(["Mã", "Tên", "Loại", "Dùng cho cột"])
    for cell in concepts[1]:
        cell.font = _HEADER_FONT
    for item in catalog:
        concepts.append([item.concept_id, item.label, item.kind, CATALOG_COLUMNS.get(item.kind, "")])
    for letter, width in (("A", 40), ("B", 40), ("C", 14), ("D", 55)):
        concepts.column_dimensions[letter].width = width
    concepts.freeze_panes = "A2"

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _write_header(worksheet) -> None:
    worksheet.append(TEMPLATE_COLUMNS)
    for index, column in enumerate(TEMPLATE_COLUMNS, start=1):
        cell = worksheet.cell(row=1, column=index)
        cell.font = _HEADER_FONT
        cell.fill = _REQUIRED_FILL if column in REQUIRED_COLUMNS else _OPTIONAL_FILL
        wide = column in ("canonical_text", "objective", "expected_points")
        worksheet.column_dimensions[cell.column_letter].width = 50 if wide else max(16, len(column) + 4)
    worksheet.freeze_panes = "A2"


def _add_dropdowns(worksheet, catalog: list[CatalogConcept]) -> None:
    def column_range(column: str) -> str:
        letter = worksheet.cell(row=1, column=TEMPLATE_COLUMNS.index(column) + 1).column_letter
        return f"{letter}2:{letter}{_TEMPLATE_ROWS}"

    lists = {
        "canonical_locale": f'"{",".join(TEMPLATE_LOCALES)}"',
        "question_type": f'"{",".join(QUESTION_TYPES)}"',
        "difficulty_band": f'"{",".join(DIFFICULTY_BANDS)}"',
    }
    primary_count = sum(item.kind in _PRIMARY_KINDS for item in catalog)
    if primary_count:
        # Catalogue rows are sorted so competency/skill concepts come first.
        lists["primary_competency_id"] = f"'{CATALOG_SHEET}'!$A$2:$A${primary_count + 1}"
    for column, formula in lists.items():
        validation = DataValidation(type="list", formula1=formula, allow_blank=True)
        validation.error = "Chọn một giá trị trong danh sách."
        validation.errorTitle = "Giá trị không hợp lệ"
        worksheet.add_data_validation(validation)
        validation.add(column_range(column))


def _example_rows(catalog: list[CatalogConcept]) -> list[dict]:
    def pick(*words: str, kinds: tuple[str, ...] = ("skill",)) -> str:
        for word in words:
            for item in catalog:
                if item.kind in kinds and word in item.concept_id.lower():
                    return item.concept_id
        fallback = next((item.concept_id for item in catalog if item.kind in kinds), "")
        return fallback or (words[0] if words else "")

    redis, docker = pick("redis"), pick("docker")
    return [
        {
            "stable_key": "vi-du-redis-cache-01", "version": "1.0.0", "canonical_locale": "vi-VN",
            "canonical_text": "Bạn sẽ dùng Redis làm cache cho một API đọc nhiều như thế nào?",
            "objective": "Hiểu cache-aside, TTL và cách vô hiệu hóa cache",
            "question_type": "technical", "difficulty_band": "intermediate",
            "thinking_seconds": 30, "soft_answer_seconds": 150, "hard_answer_seconds": 240,
            "primary_competency_id": redis, "skill_ids": redis,
            "expected_points": "CORE: Đọc cache trước, trượt thì đọc DB rồi ghi lại cache\nCORE: Đặt TTL phù hợp với dữ liệu\nBONUS: Chống cache stampede",
            "source_refs": "https://redis.io/docs/latest/develop/use/patterns/",
            "change_summary": "Tạo mới",
        },
        {
            "stable_key": "vi-du-docker-image-01", "canonical_text": "Image và container trong Docker khác nhau thế nào?",
            "objective": "Phân biệt image (bản mẫu) và container (bản đang chạy)",
            "question_type": "conceptual", "difficulty_band": "foundational",
            "primary_competency_id": docker, "skill_ids": docker,
            "expected_points": "Image là bản mẫu chỉ đọc\nContainer là một bản chạy của image\nBONUS: Nêu được layer và cache khi build",
        },
    ]


def parse_csv(content: bytes) -> list[ParsedImportRow]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must use UTF-8 encoding.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None or not _header_ok(tuple(reader.fieldnames)):
        raise ValueError("CSV header does not match question-bank import template.")
    return _parse_rows(enumerate(reader, start=2))


def _header_ok(header: tuple) -> bool:
    return header in (IMPORT_COLUMNS, TEMPLATE_COLUMNS)


def parse_xlsx(content: bytes) -> list[ParsedImportRow]:
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except Exception as exc:
        raise ValueError("Invalid XLSX workbook.") from exc
    if "Questions" not in workbook.sheetnames:
        raise ValueError("XLSX must include a Questions worksheet.")
    worksheet = workbook["Questions"]
    header = tuple(cell.value for cell in next(worksheet.iter_rows(min_row=1, max_row=1)))
    # Trailing empty header cells (a template opened and saved in Excel) are not columns.
    while header and header[-1] in (None, ""):
        header = header[:-1]
    if not _header_ok(header):
        raise ValueError("XLSX header does not match question-bank import template.")
    rows = (
        (row_number, dict(zip(header, values, strict=False)))
        for row_number, values in enumerate(
            worksheet.iter_rows(min_row=2, max_col=len(header), values_only=True), start=2
        )
    )
    return _parse_rows(rows)


def _parse_rows(rows: Iterable[tuple[int, dict]]) -> list[ParsedImportRow]:
    parsed: list[ParsedImportRow] = []
    for row_number, raw in rows:
        if not any(str("" if value is None else value).strip() for value in raw.values()):
            continue
        payload = {key: str("" if value is None else value).strip() for key, value in raw.items()}
        for key in OPTIONAL_COLUMNS:
            payload.setdefault(key, "")
        errors: list[dict] = []
        for key in JSON_COLUMNS:
            try:
                payload[key] = _list_cell(key, payload[key])
            except json.JSONDecodeError:
                errors.append({"field": key, "code": "INVALID_JSON", "severity": "ERROR"})
        for key, default in DEFAULT_SECONDS.items():
            try:
                payload[key] = _seconds_cell(payload[key], default)
            except ValueError:
                errors.append({"field": key, "code": "INVALID_INTEGER", "severity": "ERROR"})
        normalize_row_payload(payload)
        errors.extend(error for error in validate_row_payload(payload) if error not in errors)
        parsed.append(ParsedImportRow(row_number, payload, errors))
    return parsed


def _list_cell(key: str, text: str) -> list:
    """A list column: a JSON array, or the plain form an author types in Excel.

    IDs and sources are separated by commas or new lines; expected points are
    one per line, optionally prefixed with ``CORE:`` or ``BONUS:``.
    """
    if not text:
        return []
    if key in JSON_ONLY_COLUMNS or text[0] in "[{":
        return json.loads(text)
    if key == "expected_points":
        return [_point_line(line) for line in text.splitlines() if line.strip()]
    separators = "\n" if key == "source_refs" else ",\n;"
    for separator in separators[1:]:
        text = text.replace(separator, separators[0])
    return [item.strip() for item in text.split(separators[0]) if item.strip()]


def _point_line(line: str) -> dict:
    head, _, rest = line.partition(":")
    if rest and head.strip().upper() in EXPECTED_POINT_IMPORTANCE:
        return {"description": rest.strip(), "importance": head.strip().upper()}
    return {"description": line.strip()}


def _seconds_cell(text: str, default: int) -> int:
    if not text:
        return default
    value = Decimal(text) if text.replace(".", "", 1).isdigit() else None
    # Excel stores numbers as floats ("60.0"); only whole seconds are valid.
    if value is None or value != value.to_integral_value():
        raise ValueError(text)
    return int(value)


def normalize_row_payload(payload: dict) -> None:
    """Apply the same normalisation the authoring API applies."""
    payload["stable_key"] = str(payload.get("stable_key") or "").strip().lower()
    payload["version"] = str(payload.get("version") or "").strip() or "1.0.0"
    payload["canonical_locale"] = str(payload.get("canonical_locale") or "").strip() or DEFAULT_LOCALE
    taxonomy_version = str(payload.get("taxonomy_version") or "").strip()
    payload["taxonomy_version"] = taxonomy_version or DEFAULT_TAXONOMY_VERSION
    for key in ("question_type", "difficulty_band"):
        payload[key] = str(payload.get(key) or "").strip().lower()
    points = payload.get("expected_points")
    if isinstance(points, list):
        payload["expected_points"] = [_normalize_point(item, index) for index, item in enumerate(points)]


def _normalize_point(item: object, index: int) -> object:
    """One expected point as the authoring contract stores it; non-objects stay for the validator."""
    if isinstance(item, str):
        item = {"description": item}
    if not isinstance(item, dict):
        return item
    key = str(item.get("stable_key") or item.get("stableKey") or f"point-{index + 1}").strip().lower()
    return {
        "stable_key": key,
        "description": str(item.get("description") or "").strip(),
        "importance": str(item.get("importance") or "CORE").strip().upper(),
    }


def _expected_point_errors(points: object) -> list[str]:
    if not isinstance(points, list):
        return []
    if len(points) > MAX_EXPECTED_POINTS:
        return ["TOO_MANY_EXPECTED_POINTS"]
    codes: list[str] = []
    for point in points:
        if not isinstance(point, dict) or not point.get("description"):
            codes.append("INVALID_EXPECTED_POINT")
        elif point.get("importance") not in EXPECTED_POINT_IMPORTANCE:
            codes.append("INVALID_EXPECTED_POINT_IMPORTANCE")
        elif len(point["stable_key"]) > 120:
            codes.append("INVALID_EXPECTED_POINT")
    keys = [point.get("stable_key") for point in points if isinstance(point, dict)]
    if len(set(keys)) != len(keys):
        codes.append("DUPLICATE_EXPECTED_POINT_KEY")
    return list(dict.fromkeys(codes))


def rubric_criteria(payload: dict) -> list[RubricCriterionInput]:
    """The inline rubric of a row (empty when the row has none); raises ValueError when invalid."""
    raw = payload.get("rubric_criteria") or []
    if not isinstance(raw, list):
        raise ValueError("INVALID_RUBRIC")
    try:
        criteria = [RubricCriterionInput.model_validate(item) for item in raw]
    except (ValidationError, TypeError) as exc:
        raise ValueError("INVALID_RUBRIC") from exc
    if len({item.stable_key for item in criteria}) != len(criteria):
        raise ValueError("DUPLICATE_RUBRIC_CRITERION")
    if criteria and abs(sum(item.weight for item in criteria) - Decimal(1)) > _WEIGHT_TOLERANCE:
        raise ValueError("RUBRIC_WEIGHTS_NOT_ONE")
    return criteria


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
    for code in _expected_point_errors(payload.get("expected_points", [])):
        error("expected_points", code)
    if isinstance(payload.get("rubric_criteria", []), list) and payload.get("rubric_criteria"):
        if str(payload.get("rubric_key") or "").strip():
            error("rubric_criteria", "RUBRIC_KEY_AND_CRITERIA")
        try:
            rubric_criteria(payload)
        except ValueError as exc:
            error("rubric_criteria", str(exc))
    return errors
