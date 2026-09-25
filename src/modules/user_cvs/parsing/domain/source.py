import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from src.modules.user_cvs.domain.schemas import EvidenceSpan
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts


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
    "profile": {
        "profile",
        "summary",
        "objective",
        "career objective",
        "professional summary",
        "about me",
        "personal summary",
        "executive summary",
        "overview",
        "gioi thieu",
        "muc tieu nghe nghiep",
        "muc tieu",
        "thong tin ca nhan",
        "thong tin chung",
        "tong quan",
        "ve toi",
    },
    "skills": {
        "skills",
        "technical skills",
        "skills tools",
        "skills and tools",
        "skills technologies",
        "technologies",
        "tech stack",
        "programming skills",
        "core competencies",
        "technical competencies",
        "key skills",
        "profile relevant skills",
        "profile and relevant skills",
        "relevant skills",
        "ky nang",
        "ky nang chuyen mon",
        "ky nang ky thuat",
        "chuyen mon",
    },
    "employment": {
        "experience",
        "work experience",
        "employment",
        "employment history",
        "work history",
        "professional experience",
        "career history",
        "relevant experience",
        "practical experience",
        "working experience",
        "work background",
        "kinh nghiem",
        "kinh nghiem lam viec",
        "qua trinh lam viec",
        "qua trinh cong tac",
        "lich su lam viec",
    },
    "projects": {
        "projects",
        "selected projects",
        "personal projects",
        "academic projects",
        "key projects",
        "notable projects",
        "featured projects",
        "software projects",
        "technical projects",
        "side projects",
        "additional projects",
        "additional software ai projects",
        "additional software and ai projects",
        "research ai projects",
        "research and ai projects",
        "ai projects",
        "data science projects",
        "machine learning projects",
        "software ai projects",
        "notable ai projects",
        "du an",
        "du an tieu bieu",
        "du an ca nhan",
        "du an noi bat",
        "du an phan mem",
        "du an thuc te",
    },
    "research": {
        "research",
        "scientific research",
        "academic research",
        "research development",
        "research experience",
        "research projects",
        "research publications",
        "nghien cuu",
        "nghien cuu khoa hoc",
        "de tai nghien cuu",
    },
    "publications": {
        "publications",
        "scientific publications",
        "papers",
        "published papers",
        "conference papers",
        "journal papers",
        "bai bao khoa hoc",
        "cong bo khoa hoc",
        "cong trinh nghien cuu",
    },
    "competitions": {
        "competitions",
        "competitions awards",
        "competitions and awards",
        "awards",
        "honors awards",
        "honors and awards",
        "awards achievements",
        "awards and achievements",
        "achievements",
        "hackathons",
        "contests",
        "cuoc thi",
        "giai thuong",
        "thanh tich",
        "giai thuong thanh tich",
        "khen thuong",
    },
    "education": {
        "education",
        "academic background",
        "educational background",
        "education training",
        "education and training",
        "education qualifications",
        "education and qualifications",
        "academic qualifications",
        "education history",
        "hoc van",
        "trinh do hoc van",
        "giao duc",
        "dao tao",
        "hoc van dao tao",
        "qua trinh hoc tap",
    },
    "certifications": {
        "certifications",
        "certificates",
        "licenses certifications",
        "licenses and certifications",
        "credentials",
        "professional certifications",
        "chung chi",
        "chung chi nghe nghiep",
        "chung chi chuyen mon",
        "bang cap chung chi",
    },
    "languages": {
        "languages",
        "language",
        "language skills",
        "language proficiency",
        "foreign languages",
        "ngoai ngu",
        "trinh do ngoai ngu",
        "ngon ngu",
    },
}


def _repair_utf8_mojibake(value: str) -> str:
    """Recover UTF-8 text which an upstream extractor decoded as Windows-1252.

    MinerU artifacts occasionally contain strings such as ``MÃ´ táº£``.  Repair only
    when the conversion succeeds and removes the characteristic corruption markers;
    ordinary Vietnamese/English text is left untouched.
    """
    markers = ("Ã", "Â", "Ä", "á»")
    if not any(marker in value for marker in markers):
        return value
    try:
        repaired = value.encode("cp1252").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return value
    return (
        repaired
        if sum(marker in repaired for marker in markers) < sum(marker in value for marker in markers)
        else value
    )


def normalize_text(value: str) -> str:
    value = _repair_utf8_mojibake(value)
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    value = re.sub(r"[\t\f\v ]+", " ", value)
    return "\n".join(line.strip() for line in value.splitlines()).strip()


def _heading_key(value: str) -> str:
    cleaned = re.sub(r"^[#\s\-*•=]+", "", value)
    cleaned = re.sub(r"^(?:[0-9]+|[ivxlcdm]+)[\s.)\-:]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = unicodedata.normalize("NFD", cleaned.casefold())
    cleaned = "".join(char for char in cleaned if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", cleaned).strip()


_LEGACY_SECTION_HEADINGS = {
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


def is_section_regex_v2_enabled() -> bool:
    try:
        from src.core.config import get_settings

        return bool(get_settings().parser_section_regex_v2_enabled)
    except Exception:
        return True


def _section_for_heading(text: str) -> str | None:
    if not is_section_regex_v2_enabled():
        legacy_norm = unicodedata.normalize("NFD", text.casefold())
        legacy_norm = "".join(char for char in legacy_norm if unicodedata.category(char) != "Mn")
        legacy_key = re.sub(r"[^a-z0-9 ]+", " ", legacy_norm).strip()
        legacy_key = re.sub(r"\s+", " ", legacy_key)
        for section, headings in _LEGACY_SECTION_HEADINGS.items():
            if legacy_key in headings:
                return section
        return None

    stripped = text.strip()
    if not stripped or len(stripped) > 80 or len(stripped.splitlines()) > 2:
        return None
    if re.match(r"^[-*•–—]\s+", stripped):
        return None
    if any(delim in stripped for delim in (" | ", " - ", " – ", " — ", "http://", "https://", "github.com")):
        return None
    if re.search(r":\s*\S+", stripped):
        return None

    key = re.sub(r"\s+", " ", _heading_key(stripped))
    if not key or len(key.split()) > 7:
        return None

    # Numbered items such as "project 1", "project 2", "institution 1" are items, not section titles
    if re.search(r"^(?:project|du an|institution|company|organization)\s+(?:\d+|[a-z]\b)", key):
        return None

    for section, headings in _SECTION_HEADINGS.items():
        if key in headings:
            return section

    words = set(key.split())
    project_keywords = {"experience", "portfolio", "history", "works", "research", "software", "ai"}
    if words & {"projects", "du an"} or (words & {"project"} and words & project_keywords):
        return "projects"
    if words & {"research", "nghien cuu", "publications", "papers", "bai bao"}:
        return "research"
    if words & {"competitions", "awards", "hackathons", "cuoc thi", "giai thuong"}:
        return "competitions"
    if words & {"education", "academic", "hoc van", "giao duc", "dao tao"}:
        return "education"
    if words & {"experience", "employment", "kinh nghiem"}:
        return "employment"
    if words & {"certifications", "certificates", "chung chi"}:
        return "certifications"
    if words & {"languages", "language", "ngoai ngu"}:
        return "languages"
    if words & {"skills", "technologies", "ky nang"}:
        return "skills"

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
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", artifacts.markdown or "") if p.strip()]
        if not paragraphs and artifacts.markdown and artifacts.markdown.strip():
            paragraphs = [artifacts.markdown.strip()]
        raw_items = [({"type": "markdown", "text": p}, None) for p in paragraphs]

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
