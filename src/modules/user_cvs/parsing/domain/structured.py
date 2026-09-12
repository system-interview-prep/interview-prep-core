"""Deterministic, evidence-grounded extractors for structured CV sections."""

import re
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
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE = rf"(?:(?:{_MONTH})\.?\s+)?{_YEAR}"
_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to)\s*(?P<end>{_DATE}|present|current|now)",
    re.IGNORECASE,
)
_MONTH_NUMBER = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DEGREE_RE = re.compile(
    r"\b("
    r"bachelors?|masters?|ph\.?d|doctor|b\.?sc|m\.?sc|b\.?eng|b\.?e|"
    r"b\.?s|m\.?s|mba|associate|adp|f\.?sc|intermediate|a[ -]?levels|"
    r"pre-?engineering|computer sciences"
    r")\b",
    re.I,
)
_SECTION_TITLES = {
    "education", "academic background", "hoc van", "giao duc",
    "projects", "selected projects", "du an", "du an tieu bieu",
    "certifications", "certificates", "chung chi",
}


def _stable_id(kind: str, block: SourceBlock) -> str:
    digest = sha1(f"{kind}:{block.char_start}:{block.char_end}".encode(), usedforsecurity=False)
    return f"{kind}-{digest.hexdigest()[:12]}"


def _partial_date(value: str) -> PartialDate | None:
    value = value.strip().rstrip(".")
    if value.casefold() in {"present", "current", "now"}:
        return None
    year_match = re.search(_YEAR, value)
    if not year_match:
        return None
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
    return value.casefold().strip() in _SECTION_TITLES


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
        starts = [(index, _DATE_RANGE_RE.search(block.text)) for index, block in enumerate(blocks)]
        starts = [(index, match) for index, match in starts if match is not None]
        for position, (start_index, match) in enumerate(starts):
            end_index = starts[position + 1][0] if position + 1 < len(starts) else len(blocks)
            entry_blocks = blocks[start_index:end_index]
            header = blocks[start_index]
            header_text = header.text[: match.start()].strip(" -–—|,")
            parts = _split_header(header_text)
            if not parts:
                continue
            refs = [_add_block_evidence(draft, mapper, block, "employment") for block in entry_blocks]
            draft.employment.append(
                EmploymentEntry(
                    employmentId=_stable_id("employment", header),
                    jobTitle=parts[0],
                    organization=parts[1] if len(parts) > 1 else None,
                    startDate=_partial_date(match.group("start")),
                    endDate=_partial_date(match.group("end")),
                    isCurrent=_partial_date(match.group("end")) is None,
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
            degree_index = next((i for i, part in enumerate(parts) if _DEGREE_RE.search(part)), None)
            degree = parts[degree_index] if degree_index is not None else None
            institution = next(
                (part for i, part in enumerate(parts) if i != degree_index),
                parts[0],
            )
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
