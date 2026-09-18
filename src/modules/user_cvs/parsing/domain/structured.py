"""Deterministic, evidence-grounded extractors for structured CV sections."""

import re
import unicodedata
from collections.abc import Iterable
from hashlib import sha1

from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, SourceBlock, SourceDocument
from src.modules.user_cvs.domain.schemas import (
    CertificationEntry,
    EducationEntry,
    EmploymentEntry,
    PartialDate,
    ProjectEntry,
)

_YEAR = r"(?:19|20)\d{2}"
_MONTH_EN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_VI = r"(?:thg\s*\d{1,2}|tháng\s*\d{1,2})"
_MONTH = rf"(?:{_MONTH_EN}|{_MONTH_VI})"
_DATE = rf"(?:(?:{_MONTH})\.?\s+)?{_YEAR}"
_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to|đến)\s*(?P<end>{_DATE}|present|current|now|hiện\s*tại|nay)",
    re.IGNORECASE,
)
_MONTH_NUMBER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_INSTITUTION_RE = re.compile(
    r"\b(institution|university|college|school|academy|institute|cơ sở giáo dục|trường|đại học|học viện)\b",
    re.I,
)
_DEGREE_RE = re.compile(
    r"\b("
    r"bachelors?|masters?|ph\.?d|doctor|b\.?sc|m\.?sc|b\.?eng|m\.?eng|b\.?e|m\.?e|"
    r"b\.?s|m\.?s|mba|associate|adp|f\.?sc|intermediate|a[ -]?levels|o[ -]?levels|"
    r"pre-?engineering|computer sciences?|diploma|hsc|ssc|bba|dba|mcs|bscs|bsit|"
    r"b\.?com|m\.?com|b\.?tech|m\.?tech|b\.?a|m\.?a|f\.?a|llb|degree|graduation|"
    r"cử nhân|thạc sĩ|tiến sĩ|kỹ sư|cao đẳng|đại học|trung học|phổ thông|bằng cấp"
    r")\b",
    re.I,
)
_SECTION_TITLES = {
    "education", "academic background", "hoc van", "giao duc", "học vấn", "giáo dục",
    "projects", "selected projects", "du an", "du an tieu bieu", "dự án", "dự án tiêu biểu",
    "certifications", "certificates", "chung chi", "chứng chỉ",
}


def _stable_id(kind: str, block: SourceBlock) -> str:
    digest = sha1(f"{kind}:{block.char_start}:{block.char_end}".encode(), usedforsecurity=False)
    return f"{kind}-{digest.hexdigest()[:12]}"


def _partial_date(value: str) -> PartialDate | None:
    value = value.strip().rstrip(".")
    if value.casefold() in {"present", "current", "now", "hiện tại", "hien tai", "nay"}:
        return None
    year_match = re.search(_YEAR, value)
    if not year_match:
        return None
    month_match = re.search(r"(?:thg\s*|tháng\s*)(\d{1,2})", value, re.I)
    if month_match:
        month = int(month_match.group(1))
        if 1 <= month <= 12:
            return PartialDate(value=f"{year_match.group()}-{month:02d}", precision="month")
    month_match = re.match(r"[A-Za-z]+", value)
    if month_match:
        month = _MONTH_NUMBER.get(month_match.group().casefold()[:3])
        if month:
            return PartialDate(value=f"{year_match.group()}-{month:02d}", precision="month")
    return PartialDate(value=year_match.group(), precision="year")


def _block_evidence(mapper: EvidenceMapper, block: SourceBlock, kind: str) -> str:
    evidence_id = _stable_id(f"{kind}-evidence", block)
    return evidence_id


def _add_block_evidence(draft, mapper: EvidenceMapper, block: SourceBlock, kind: str) -> str:
    evidence_id = _block_evidence(mapper, block, kind)
    draft.evidence.setdefault(
        evidence_id,
        mapper.from_offsets(
            evidence_id=evidence_id,
            char_start=block.char_start,
            char_end=block.char_end,
        ),
    )
    return evidence_id


def _section_blocks(source: SourceDocument, section: str) -> list[SourceBlock]:
    return [block for block in source.blocks if block.section == section]


def _split_header(value: str) -> list[str]:
    # Commas are common inside job titles (for example, a department suffix).
    # Prefer explicit separators; only use commas when no stronger delimiter is
    # present in the CV header.
    separator = r"\s*(?:\||@|\bat\b)\s*" if re.search(r"\||@|\bat\b", value, re.I) else r"\s*,\s*"
    return [part.strip(" -–—|,") for part in re.split(separator, value) if part.strip(" -–—|,")]


def _is_section_title(value: str) -> bool:
    cleaned = unicodedata.normalize("NFD", value.casefold().strip())
    without_marks = "".join(c for c in cleaned if unicodedata.category(c) != "Mn")
    raw = value.casefold().strip()
    return raw in _SECTION_TITLES or without_marks in _SECTION_TITLES


def _skill_ids_for_blocks(draft, blocks: Iterable[SourceBlock]) -> list[str]:
    intervals = [(block.char_start, block.char_end) for block in blocks]
    matching_evidence = {
        evidence_id
        for evidence_id, evidence in draft.evidence.items()
        if any(start <= evidence.char_start and evidence.char_end <= end for start, end in intervals)
    }
    return [
        skill.claim_id
        for skill in draft.skills
        if matching_evidence.intersection(skill.evidence_refs)
    ]


class EmploymentExtractor:
    """Extract dated experience entries and their following responsibility blocks."""

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft) -> None:
        blocks = _section_blocks(source, "employment")
        starts = []
        for index, block in enumerate(blocks):
            if _is_section_title(block.text):
                continue
            match = _DATE_RANGE_RE.search(block.text)
            if match is not None:
                starts.append((index, match.start(), match.group("start"), match.group("end")))
            else:
                single_match = re.search(r"\b((?:19|20)\d{2})\b", block.text)
                if single_match is not None:
                    starts.append(
                        (
                            index,
                            single_match.start(),
                            single_match.group(1),
                            single_match.group(1),
                        )
                    )
        for position, (start_index, start_pos, start_val, end_val) in enumerate(starts):
            end_index = starts[position + 1][0] if position + 1 < len(starts) else len(blocks)
            entry_blocks = blocks[start_index:end_index]
            header = blocks[start_index]
            header_text = header.text[: start_pos].strip(" -–—|,")
            parts = _split_header(header_text)
            if not parts:
                continue
            if len(parts) == 1 and re.search(r"\b(organization|tổ chức|company|công ty)\b", parts[0], re.I):
                continue
            refs = [_add_block_evidence(draft, mapper, block, "employment") for block in entry_blocks]
            draft.employment.append(
                EmploymentEntry(
                    employmentId=_stable_id("employment", header),
                    jobTitle=parts[0],
                    organization=parts[1] if len(parts) > 1 else None,
                    startDate=_partial_date(start_val),
                    endDate=_partial_date(end_val),
                    isCurrent=_partial_date(end_val) is None,
                    responsibilities=[block.text for block in entry_blocks[1:]],
                    skillClaimIds=_skill_ids_for_blocks(draft, entry_blocks),
                    evidenceRefs=refs,
                )
            )


class EducationExtractor:
    """Extract dated education entries while preserving ambiguous labels as evidence."""

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft) -> None:
        for block in _section_blocks(source, "education"):
            if _is_section_title(block.text):
                continue
            match = _DATE_RANGE_RE.search(block.text)
            label = block.text[: match.start()].strip(" -–—|,") if match else block.text.strip()
            parts = _split_header(label)
            if not parts:
                continue
            if len(parts) == 1:
                if _DEGREE_RE.search(parts[0]) and not _INSTITUTION_RE.search(parts[0]):
                    institution, degree = None, parts[0]
                else:
                    institution, degree = parts[0], None
            else:
                deg_idx = next((i for i, part in enumerate(parts) if _DEGREE_RE.search(part)), None)
                inst_idx = next((i for i, part in enumerate(parts) if _INSTITUTION_RE.search(part)), None)
                if deg_idx is not None and inst_idx is not None and deg_idx != inst_idx:
                    institution, degree = parts[inst_idx], parts[deg_idx]
                elif deg_idx is not None:
                    institution = next((p for i, p in enumerate(parts) if i != deg_idx), parts[0])
                    degree = parts[deg_idx]
                elif inst_idx is not None:
                    degree = next((p for i, p in enumerate(parts) if i != inst_idx), parts[1])
                    institution = parts[inst_idx]
                else:
                    institution, degree = parts[0], parts[1]

            ref = _add_block_evidence(draft, mapper, block, "education")
            draft.education.append(
                EducationEntry(
                    educationId=_stable_id("education", block),
                    institution=institution,
                    degree=degree,
                    startDate=_partial_date(match.group("start")) if match else None,
                    endDate=_partial_date(match.group("end")) if match else None,
                    evidenceRefs=[ref],
                )
            )


class ProjectExtractor:
    """Treat each non-heading project block as an independently reviewable project."""

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft) -> None:
        for block in _section_blocks(source, "projects"):
            lines = [line.strip("•- ") for line in block.text.splitlines() if line.strip("•- ")]
            if not lines or _is_section_title(lines[0]):
                continue
            if re.search(r"\bwithout a declared project name\b", lines[0], re.I):
                continue
            ref = _add_block_evidence(draft, mapper, block, "project")
            draft.projects.append(
                ProjectEntry(
                    projectId=_stable_id("project", block),
                    name=lines[0],
                    description="\n".join(lines[1:]) or None,
                    skillClaimIds=_skill_ids_for_blocks(draft, [block]),
                    evidenceRefs=[ref],
                )
            )


class CertificationExtractor:
    """Extract one certification claim from each source block in the certification section."""

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft) -> None:
        for block in _section_blocks(source, "certifications"):
            name = block.text.strip("•- ")
            if not name or _is_section_title(name):
                continue
            if re.search(r"^(no\s+certifications?|none|n/a|không\s+có)", name, re.I):
                continue
            match = _DATE_RANGE_RE.search(name)
            ref = _add_block_evidence(draft, mapper, block, "certification")
            draft.certifications.append(
                CertificationEntry(
                    certificationId=_stable_id("certification", block),
                    name=name[: match.start()].strip(" -–—|,") if match else name,
                    issuedDate=_partial_date(match.group("start")) if match else None,
                    expiresDate=_partial_date(match.group("end")) if match else None,
                    evidenceRefs=[ref],
                )
            )
