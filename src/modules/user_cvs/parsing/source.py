import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from src.modules.matching.schemas import EvidenceSpan
from src.workers.mineru import DocumentArtifacts


@dataclass(frozen=True)
class SourceBlock:
    block_id: str
    text: str
    page: int | None
    reading_order: int
    bounding_box: tuple[float, float, float, float] | None
    block_type: str
    char_start: int
    char_end: int
    section: str


@dataclass(frozen=True)
class SourceDocument:
    document_id: str
    document_sha256: str
    text: str
    blocks: tuple[SourceBlock, ...]


_SECTION_HEADINGS = {
    "profile": {"profile", "summary", "objective", "gioi thieu", "muc tieu nghe nghiep"},
    "skills": {"skills", "technical skills", "ky nang", "ky nang chuyen mon"},
    "employment": {
        "experience",
        "work experience",
        "employment",
        "kinh nghiem",
        "kinh nghiem lam viec",
    },
    "projects": {"projects", "selected projects", "du an", "du an tieu bieu"},
    "education": {"education", "academic background", "hoc van", "giao duc"},
    "certifications": {"certifications", "certificates", "chung chi"},
    "languages": {"languages", "language", "ngoai ngu"},
}


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    value = re.sub(r"[\t\f\v ]+", " ", value)
    return "\n".join(line.strip() for line in value.splitlines()).strip()


def _heading_key(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip()


def _section_for_heading(text: str) -> str | None:
    key = re.sub(r"\s+", " ", _heading_key(text))
    for section, headings in _SECTION_HEADINGS.items():
        if key in headings:
            return section
    return None


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _content_text(item)))
    if not isinstance(value, dict):
        return ""
    for key in (
        "text",
        "content",
        "title_content",
        "paragraph_content",
        "code_content",
        "table_body",
        "list_items",
    ):
        if key in value:
            rendered = _content_text(value[key])
            if rendered:
                return rendered
    return ""


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if not all(isinstance(item, (int, float)) for item in value):
        return None
    return float(value[0]), float(value[1]), float(value[2]), float(value[3])


def _artifact_items(content_list: list[Any]) -> list[tuple[dict[str, Any], int | None]]:
    if content_list and all(isinstance(page, list) for page in content_list):
        return [
            (item, page_index + 1)
            for page_index, page in enumerate(content_list)
            for item in page
            if isinstance(item, dict)
        ]
    return [
        (item, int(item["page_idx"]) + 1 if isinstance(item.get("page_idx"), int) else None)
        for item in content_list
        if isinstance(item, dict)
    ]


def build_source_document(
    artifacts: DocumentArtifacts, *, document_id: str, document_sha256: str
) -> SourceDocument:
    raw_items = _artifact_items(artifacts.content_list)
    if not raw_items:
        raw_items = [({"type": "markdown", "text": artifacts.markdown}, None)]

    rendered: list[tuple[str, int | None, tuple[float, float, float, float] | None, str]] = []
    for item, page in raw_items:
        text = normalize_text(_content_text(item))
        if text:
            rendered.append((text, page, _bbox(item.get("bbox")), str(item.get("type") or "text")))

    parts: list[str] = []
    blocks: list[SourceBlock] = []
    cursor = 0
    current_section = "other"
    for order, (block_text, page, bounding_box, block_type) in enumerate(rendered):
        heading_section = _section_for_heading(block_text)
        if heading_section:
            current_section = heading_section
        if parts:
            parts.append("\n")
            cursor += 1
        start = cursor
        parts.append(block_text)
        cursor += len(block_text)
        blocks.append(
            SourceBlock(
                block_id=f"block-{order:04d}",
                text=block_text,
                page=page,
                reading_order=order,
                bounding_box=bounding_box,
                block_type=block_type,
                char_start=start,
                char_end=cursor,
                section=current_section,
            )
        )
    return SourceDocument(
        document_id=document_id,
        document_sha256=document_sha256,
        text="".join(parts),
        blocks=tuple(blocks),
    )


class EvidenceMapper:
    def __init__(self, source: SourceDocument) -> None:
        self.source = source

    def from_offsets(
        self, *, evidence_id: str, char_start: int, char_end: int, section: str | None = None
    ) -> EvidenceSpan:
        if char_start < 0 or char_end > len(self.source.text) or char_end <= char_start:
            raise ValueError("evidence offsets are outside the source document")
        block = next(
            (
                item
                for item in self.source.blocks
                if item.char_start <= char_start and char_end <= item.char_end
            ),
            None,
        )
        return EvidenceSpan(
            evidenceId=evidence_id,
            documentId=self.source.document_id,
            documentSha256=self.source.document_sha256,
            section=section or (block.section if block else "other"),
            text=self.source.text[char_start:char_end],
            charStart=char_start,
            charEnd=char_end,
            page=block.page if block else None,
            sourceBlockId=block.block_id if block else None,
            readingOrder=block.reading_order if block else None,
            boundingBox=block.bounding_box if block else None,
        )

    def exact_quote(
        self, *, evidence_id: str, quote: str, section: str | None = None
    ) -> EvidenceSpan:
        normalized_quote = normalize_text(quote)
        starts = [match.start() for match in re.finditer(re.escape(normalized_quote), self.source.text)]
        if len(starts) != 1:
            raise ValueError("evidence quote must occur exactly once in the source document")
        return self.from_offsets(
            evidence_id=evidence_id,
            char_start=starts[0],
            char_end=starts[0] + len(normalized_quote),
            section=section,
        )
