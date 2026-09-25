"""Deterministic, evidence-grounded extractors for structured CV sections."""

import re
import unicodedata
from collections.abc import Iterable
from hashlib import sha1

from src.modules.user_cvs.domain.schemas import (
    CertificationEntry,
    EducationEntry,
    EmploymentEntry,
    PartialDate,
    ProjectEntry,
)
from src.modules.user_cvs.parsing.domain.source import (
    EvidenceMapper,
    SourceBlock,
    SourceDocument,
    _section_for_heading,
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
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_INSTITUTION_RE = re.compile(
    r"\b("
    r"institution|university|college|school|academy|institute|"
    r"cơ sở giáo dục|trường|đại học|học viện|cao đẳng|"
    r"iuh|hcmus|hust|uit|vnu|bk|bkhn|hcmut|uet|dut|ptit|neu|ftu"
    r")\b",
    re.I,
)
_DEGREE_RE = re.compile(
    r"\b("
    r"bachelors?|masters?|ph\.?d|doctor|b\.?sc|m\.?sc|b\.?eng|m\.?eng|b\.?e|m\.?e|"
    r"b\.?s|m\.?s|mba|associate|adp|f\.?sc|intermediate|a[ -]?levels|o[ -]?levels|"
    r"pre-?engineering|computer sciences?|diploma|hsc|ssc|bba|dba|mcs|bscs|bsit|"
    r"b\.?com|m\.?com|b\.?tech|m\.?tech|b\.?a|m\.?a|f\.?a|llb|degree|graduation|"
    r"cử nhân|thạc sĩ|tiến sĩ|kỹ sư|cao đẳng|đại học|trung học|phổ thông|bằng cấp|"
    r"student|undergraduate|sinh viên|năm cuối|final-year"
    r")\b",
    re.I,
)
_ACADEMIC_RE = re.compile(
    r"\b(gpa|cpa|major|minor|chuyên ngành|ngành|khoa|khoá|niên khoá|luận văn|thesis)\b",
    re.IGNORECASE,
)
_SECTION_TITLES = {
    "education",
    "academic background",
    "hoc van",
    "giao duc",
    "học vấn",
    "giáo dục",
    "projects",
    "selected projects",
    "du an",
    "du an tieu bieu",
    "dự án",
    "dự án tiêu biểu",
    "research",
    "scientific research",
    "nghien cuu",
    "nghiên cứu",
    "competitions",
    "awards",
    "cuoc thi",
    "cuộc thi",
    "giai thuong",
    "giải thưởng",
    "publications",
    "bai bao",
    "bài báo",
    "certifications",
    "certificates",
    "chung chi",
    "chứng chỉ",
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
    stripped = value.strip()
    if not stripped:
        return False
    if _section_for_heading(stripped) is not None:
        return True
    cleaned = unicodedata.normalize("NFD", stripped.casefold())
    without_marks = "".join(c for c in cleaned if unicodedata.category(c) != "Mn")
    raw = stripped.casefold()
    return raw in _SECTION_TITLES or without_marks in _SECTION_TITLES


def _skill_ids_for_blocks(draft, blocks: Iterable[SourceBlock]) -> list[str]:
    intervals = [(block.char_start, block.char_end) for block in blocks]
    matching_evidence = {
        evidence_id
        for evidence_id, evidence in draft.evidence.items()
        if any(start <= evidence.char_start and evidence.char_end <= end for start, end in intervals)
    }
    return [skill.claim_id for skill in draft.skills if matching_evidence.intersection(skill.evidence_refs)]


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
            header_text = header.text[:start_pos].strip(" -–—|,")
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


def _clean_institution(value: str) -> str:
    cleaned = re.sub(
        r"\s*(?:[-–—|,]|\b)?\s*(?:GPA|CPA|Điểm|Diem)\s*[:=]?\s*\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?",
        "",
        value,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*(?:[-–—|,]|\b)\s*(?:GPA|CPA)\b.*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" -–—|,")


_INVALID_INSTITUTION_RE = re.compile(
    r"^(?:"
    r"information technology|software engineering|computer science|"
    r"information technology\s*[-–—|]\s*software engineering|"
    r"final-year student|student|sinh viên|năm cuối|"
    r"gpa.*|cpa.*|"
    r"projects?|dự án|research|nghiên cứu|education|học vấn"
    r")$",
    re.IGNORECASE,
)


class EducationExtractor:
    """Extract dated education entries while preserving ambiguous labels as evidence."""

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft) -> None:
        edu_blocks = _section_blocks(source, "education")
        valid_blocks: list[SourceBlock] = []
        for block in edu_blocks:
            text = block.text.strip()
            if not text or _is_section_title(text):
                continue
            has_edu_signal = bool(
                _INSTITUTION_RE.search(text) or _DEGREE_RE.search(text) or _ACADEMIC_RE.search(text)
            )
            if not has_edu_signal:
                continue
            is_project_or_research = bool(
                re.search(
                    r"\b(projects?|dự án|research|nghiên cứu|publications?|competitions?)\b",
                    text,
                    re.I,
                )
            )
            if is_project_or_research and not _INSTITUTION_RE.search(text):
                continue
            valid_blocks.append(block)

        # Group blocks by institution anchor:
        # Blocks before the first institution attach to it. Subsequent blocks attach to preceding institution.
        inst_indices = [i for i, b in enumerate(valid_blocks) if _INSTITUTION_RE.search(b.text)]
        groups: list[list[SourceBlock]] = []
        if inst_indices:
            for idx, inst_i in enumerate(inst_indices):
                start = 0 if idx == 0 else inst_i
                end = inst_indices[idx + 1] if idx + 1 < len(inst_indices) else len(valid_blocks)
                groups.append(valid_blocks[start:end])
        elif valid_blocks:
            groups.append(valid_blocks)

        for group in groups:
            inst_block = next((b for b in group if _INSTITUTION_RE.search(b.text)), group[0])
            match_inst = _DATE_RANGE_RE.search(inst_block.text)
            inst_label_raw = (
                inst_block.text[: match_inst.start()].strip(" -–—|,")
                if match_inst
                else inst_block.text.strip()
            )
            inst_parts = _split_header(inst_label_raw)
            inst_part = next(
                (p for p in inst_parts if _INSTITUTION_RE.search(p)),
                inst_parts[0] if inst_parts else inst_block.text,
            )
            institution = _clean_institution(inst_part)

            degree: str | None = None
            field_of_study: str | None = None
            start_date = None
            end_date = None
            student_status = None
            gpa = None
            gpa_scale = None

            for b in group:
                normalized = b.text.casefold()
                if re.search(r"\bfinal[ -]?year\b|\bn[aă]m cu[oố]i\b", normalized):
                    student_status = "final_year"
                elif re.search(r"\brecent graduate\b|\bm[oớ]i t[oố]t nghi[eệ]p\b", normalized):
                    student_status = "recent_graduate"
                elif re.search(r"\bstudent\b|\bsinh vi[eê]n\b", normalized):
                    student_status = student_status or "student"
                gpa_match = re.search(
                    r"\b(?:GPA|CPA)\s*[:=]?\s*(?P<value>\d+(?:\.\d+)?)"
                    r"(?:\s*/\s*(?P<scale>\d+(?:\.\d+)?))?",
                    b.text,
                    re.IGNORECASE,
                )
                if gpa_match:
                    candidate_gpa = float(gpa_match.group("value"))
                    candidate_scale = float(
                        gpa_match.group("scale") or (4.0 if candidate_gpa <= 4.0 else 10.0)
                    )
                    if 0 < candidate_gpa <= candidate_scale:
                        gpa, gpa_scale = candidate_gpa, candidate_scale
                match = _DATE_RANGE_RE.search(b.text)
                if match:
                    if not start_date:
                        start_date = _partial_date(match.group("start"))
                    if not end_date:
                        end_date = _partial_date(match.group("end"))
                else:
                    year_match = re.search(r"\b((?:19|20)\d{2})\b", b.text)
                    if year_match and not start_date:
                        start_date = _partial_date(year_match.group(1))

                text_without_date = b.text[: match.start()].strip(" -–—|,") if match else b.text.strip()
                parts = _split_header(text_without_date)
                for part in parts:
                    part_cleaned = _clean_institution(part)
                    if not part_cleaned:
                        continue
                    if _DEGREE_RE.search(part_cleaned):
                        if not degree:
                            degree = part_cleaned
                    elif _ACADEMIC_RE.search(part_cleaned) or re.search(
                        r"\b(engineering|technology|science|computer|information|software|"
                        r"kỹ thuật|công nghệ|khoa học|tin học|phần mềm)\b",
                        part_cleaned,
                        re.I,
                    ):
                        if part_cleaned != institution and part_cleaned != degree:
                            if not field_of_study:
                                field_of_study = part_cleaned

            # Never allow major, program, student-status, or GPA text to become institution
            if not _INSTITUTION_RE.search(institution) or _INVALID_INSTITUTION_RE.match(institution):
                if degree and not _INVALID_INSTITUTION_RE.match(degree):
                    institution = degree
                else:
                    continue

            institution = _clean_institution(institution)
            if _INVALID_INSTITUTION_RE.match(institution):
                continue

            refs = [_add_block_evidence(draft, mapper, b, "education") for b in group]
            draft.education.append(
                EducationEntry(
                    educationId=_stable_id("education", group[0]),
                    institution=institution,
                    degree=degree,
                    fieldOfStudy=field_of_study,
                    startDate=start_date,
                    endDate=end_date,
                    studentStatus=student_status,
                    gpa=gpa,
                    gpaScale=gpa_scale,
                    evidenceRefs=refs,
                )
            )


_PROJECT_DETAIL_PREFIX_RE = re.compile(
    r"^(?:"
    r"description|mo ta|mô tả|chi tiết(?: dự án)?|chi tiet(?: du an)?|"
    r"responsibilities|trách nhiệm|nhiệm vụ|trach nhiem|nhiem vu|"
    r"key features|features|tính năng(?: chính| nổi bật)?|tinh nang(?: chinh| noi bat)?|"
    r"highlights|điểm nổi bật|diem noi bat|"
    r"achievements|thành tựu|thành tích|thanh tuu|thanh tich|"
    r"results|kết quả|ket qua|"
    r"architecture|kiến trúc(?: hệ thống)?|kien truc(?: he thong)?|"
    r"environment|môi trường|moi truong|"
    r"tech|technologies|technology|tech stack|công nghệ(?: sử dụng)?|cong nghe(?: su dung)?|"
    r"role|vai trò|position|vị trí|vai tro|vi tri|"
    r"methods|methodology|phương pháp|phuong phap|"
    r"tools|công cụ|cong cu|"
    r"scientific paper|paper|bài báo|publication|bai bao|"
    r"award|awards|giải thưởng|giai thuong|prize"
    r")\s*[:\-–—]",
    re.IGNORECASE,
)


def _is_project_header(block_text: str) -> bool:
    stripped = block_text.strip()
    if not stripped or _is_section_title(stripped):
        return False
    if re.match(r"^[-*•–—+]\s+", stripped):
        return False
    without_bullet = re.sub(r"^[-*•–—+]\s*", "", stripped)
    if _PROJECT_DETAIL_PREFIX_RE.search(without_bullet):
        return False
    return True


class ProjectExtractor:
    """Group project headings with their detail blocks and extract reviewable projects."""

    def extract(self, source: SourceDocument, mapper: EvidenceMapper, draft) -> None:
        project_sections = {"projects", "research", "competitions", "publications"}
        blocks = [b for b in source.blocks if b.section in project_sections]

        groups: list[list[SourceBlock]] = []
        current_group: list[SourceBlock] = []
        for block in blocks:
            if _is_section_title(block.text):
                continue
            if _is_project_header(block.text):
                if current_group:
                    groups.append(current_group)
                current_group = [block]
            else:
                if current_group:
                    current_group.append(block)
                else:
                    current_group = [block]
        if current_group:
            groups.append(current_group)

        for group in groups:
            header = group[0]
            lines = [line.strip("•- ") for line in header.text.splitlines() if line.strip("•- ")]
            if not lines or lines[0].casefold() in _SECTION_TITLES:
                continue
            if re.search(r"\bwithout a declared project name\b", lines[0], re.I):
                continue

            name_raw = lines[0]
            if _PROJECT_DETAIL_PREFIX_RE.search(name_raw):
                continue
            name_clean = re.sub(r"\s*\((?:19|20)\d{2}[^)]*\)$", "", name_raw).strip()
            name_clean = re.sub(r"\s*(?:\||\b(?:github|website)\b).*$", "", name_clean, flags=re.I).strip(
                " -–—|,"
            )
            if not name_clean:
                name_clean = name_raw

            match = _DATE_RANGE_RE.search(header.text)
            start_date = None
            end_date = None
            if match:
                start_date = _partial_date(match.group("start"))
                end_date = _partial_date(match.group("end"))
            else:
                year_match = re.search(r"\b((?:19|20)\d{2})\b", header.text)
                if year_match:
                    start_date = _partial_date(year_match.group(1))

            desc_parts = lines[1:] + [b.text for b in group[1:]]
            description = "\n".join(desc_parts) if desc_parts else None

            refs = [_add_block_evidence(draft, mapper, b, "project") for b in group]

            draft.projects.append(
                ProjectEntry(
                    projectId=_stable_id("project", header),
                    name=name_clean,
                    description=description,
                    startDate=start_date,
                    endDate=end_date,
                    skillClaimIds=_skill_ids_for_blocks(draft, group),
                    evidenceRefs=refs,
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
