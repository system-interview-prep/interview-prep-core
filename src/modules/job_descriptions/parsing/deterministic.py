import logging
import re
import unicodedata
from datetime import UTC, datetime
from hashlib import sha1
from typing import Any

from src.modules.job_descriptions.domain.schemas import (
    CanonicalJobDescription,
    GroundedJobText,
    JobRequirement,
)
from src.modules.taxonomy.facade import classify_career
from src.modules.user_cvs.facade import EvidenceMapper, SourceDocument
from src.modules.user_cvs.schemas import CareerClassification, ParsingMetadata, TaxonomyRef

logger = logging.getLogger(__name__)

_PRIORITY_NEGATION_RE = re.compile(
    r"\b(?:"
    r"not\s+(?:strictly\s+)?required|is\s+not\s+required|not\s+mandatory|no\s+requirement|"
    r"not\s+needed|not\s+compulsory|not\s+a\s+requirement|"
    r"khong\s+bat\s+buoc|khong\s+yeu\s+cau|khong\s+can\s+thiet|khong\s+doi\s+hoi"
    r")\b",
    re.I,
)

_PRIORITY_PREFERRED_RE = re.compile(
    r"\b(?:"
    r"(?:la\s+)?diem\s+cong(?:\s+lon)?|"
    r"(?:la\s+)?loi\s+the(?:\s+lon)?|"
    r"(?:la\s+|duoc\s+)?uu\s+tien(?:\s+ung\s+vien)?|"
    r"co\s+loi\s+the|"
    r"khuyen\s+khich|"
    r"(?:is\s+)?preferred|"
    r"(?:is\s+)?(?:a\s+)?plus|"
    r"nice\s+to\s+have|"
    r"desirable|"
    r"(?:is\s+)?(?:an\s+)?advantage(?:ous)?|"
    r"optional"
    r")\b",
    re.I,
)

_PRIORITY_MUST_HAVE_RE = re.compile(
    r"\b(?:"
    r"(?:la\s+)?bat\s+buoc(?:\s+co)?|"
    r"yeu\s+cau\s+bat\s+buoc|"
    r"phai\s+co|"
    r"(?:is\s+)?mandatory|"
    r"must(?:\s+have)?|"
    r"compulsory|"
    r"required"
    r")\b",
    re.I,
)


_CONTRAST_PREFERRED_RE = re.compile(
    r"\b(?:but|however|nhung|song)\b.*?\b(?:preferred|plus|nice\s+to\s+have|advantage|diem\s+cong|loi\s+the|uu\s+tien)\b",
    re.I,
)

_CONCEPT_EQUIVS = {
    "cert": "__CERT__",
    "certified": "__CERT__",
    "certification": "__CERT__",
    "certificate": "__CERT__",
    "practitioner": "__CERT__",
    "chung": "__CERT__",
    "chi": "__CERT__",
    "bang": "__DEGREE__",
    "cap": "__DEGREE__",
    "degree": "__DEGREE__",
    "bachelor": "__DEGREE__",
    "master": "__DEGREE__",
    "phd": "__DEGREE__",
    "kinh": "__EXP__",
    "nghiem": "__EXP__",
    "experience": "__EXP__",
    "exp": "__EXP__",
}

_FILLER_WORDS = {
    "a", "an", "the", "any", "moi", "tat", "ca", "truoc", "do", "co", "is", "are",
    "for", "of", "in", "with", "ve", "to", "and", "or",
}

_NEG_TOPIC_PATTERNS = [
    # Format 1: <topic> is not required / không bắt buộc
    re.compile(
        r"(?:^|[.;\n])\s*(?:(?:a|an|the)\s+)?(?P<topic>[a-z0-9\s\+\#\-]+?)\s+"
        r"(?:is|are|will\s+be)?\s*"
        r"(?:not\s+(?:strictly\s+)?required|not\s+mandatory|not\s+needed|not\s+compulsory|not\s+a\s+requirement|khong\s+bat\s+buoc|khong\s+yeu\s+cau)\b",
        re.I,
    ),
    # Format 2: không yêu cầu / no requirement for <topic>
    re.compile(
        r"\b(?:not\s+require(?:s)?|do\s+not\s+require|does\s+not\s+require|no\s+requirement\s+for|khong\s+yeu\s+cau|khong\s+bat\s+buoc)\s+"
        r"(?:(?:a|an|the|any)\s+)?(?P<topic>[a-z0-9\s\+\#\-]+?)(?=$|[;,.]|\s+(?:and|but|may|candidates|ung\s+vien))",
        re.I,
    ),
]

_POSITIVE_OVERRIDE_RE = re.compile(
    r"\b(?:"
    r"preferred|is\s+preferred|plus|a\s+plus|is\s+a\s+plus|nice\s+to\s+have|advantage|"
    r"diem\s+cong|loi\s+the|uu\s+tien|"
    r"required|mandatory|must|must\s+have|bat\s+buoc"
    r")\b",
    re.I,
)


def _normalize_tokens(text: str) -> set[str]:
    words = _key(text).split()
    tokens: set[str] = set()
    for w in words:
        if w in _FILLER_WORDS:
            continue
        mapped = _CONCEPT_EQUIVS.get(w, w)
        tokens.add(mapped)
    return tokens


def _extract_negated_topics(text: str) -> list[set[str]]:
    topics: list[set[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        k = _key(line)
        for pat in _NEG_TOPIC_PATTERNS:
            for m in pat.finditer(k):
                t = m.group("topic").strip()
                t = re.sub(r"^(?:a|an|the|any|moi|tat\s+ca)\s+", "", t).strip()
                toks = _normalize_tokens(t)
                if toks:
                    topics.append(toks)
    return topics


def _has_positive_override(candidate_value: str) -> bool:
    return bool(_POSITIVE_OVERRIDE_RE.search(_key(candidate_value)))


def _is_negated_by_document(candidate_value: str, doc_negated_topics: list[set[str]]) -> bool:
    if _has_positive_override(candidate_value):
        return False
    cand_toks = _normalize_tokens(candidate_value)
    for topic_toks in doc_negated_topics:
        # Conservative principle: all non-filler concept tokens of negated topic must be matched
        if topic_toks and topic_toks.issubset(cand_toks):
            return True
    return False


def _detect_priority(value: str, section_priority: str = "must_have") -> str:
    key_text = _key(value)
    is_neg = bool(_PRIORITY_NEGATION_RE.search(key_text))
    is_contrast_pref = bool(_CONTRAST_PREFERRED_RE.search(key_text))
    is_pref = bool(_PRIORITY_PREFERRED_RE.search(key_text))
    is_must = bool(_PRIORITY_MUST_HAVE_RE.search(key_text)) and not is_neg

    if is_contrast_pref:
        return "preferred"

    if is_pref and is_must:
        logger.warning(
            "Ambiguous conflicting priority cues in requirement: %r. Defaulting to section priority %r.",
            value,
            section_priority,
        )
        return section_priority

    if is_pref:
        return "preferred"

    if is_must:
        return "must_have"

    return section_priority

_SKILLS = {
    "skill-unity": ("Unity", ("unity", "unity engine")),
    "skill-java": ("Java", ("java",)),
    "skill-spring-boot": ("Spring Boot", ("spring boot",)),
    "skill-python": ("Python", ("python",)),
    "skill-fastapi": ("FastAPI", ("fastapi",)),
    "skill-react": ("React", ("react", "reactjs")),
    "skill-javascript": ("JavaScript", ("javascript",)),
    "skill-typescript": ("TypeScript", ("typescript",)),
    "skill-docker": ("Docker", ("docker",)),
    "skill-kubernetes": ("Kubernetes", ("kubernetes", "k8s")),
    "skill-aws": ("AWS", ("aws",)),
    "skill-git": ("Git", ("git",)),
    "skill-json": ("JSON", ("json",)),
    "skill-sql": ("SQL", ("sql",)),
    "skill-http": ("HTTP", ("http",)),
    "skill-android": ("Android", ("android",)),
    "skill-ios": ("iOS", ("ios",)),
    "skill-artificial-intelligence": ("Artificial Intelligence", ("artificial intelligence", "ai")),
    "skill-machine-learning": ("Machine Learning", ("machine learning", "ml")),
    "skill-natural-language-processing": (
        "Natural Language Processing",
        ("natural language processing", "nlp"),
    ),
    "skill-generative-ai": ("Generative AI", ("generative ai", "genai")),
    "skill-large-language-models": (
        "Large Language Models",
        ("large language model", "large language models", "llm"),
    ),
    "skill-kotlin": ("Kotlin", ("kotlin",)),
    "skill-cplusplus": ("C++", ("c++", "cpp")),
}


def resolve_known_skill_concepts(
    text: str,
    *,
    taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None,
    taxonomy_version: str = "internal-2026.1",
) -> list[TaxonomyRef]:
    """Resolve only explicit known skill aliases present in grounded text.

    This is shared by parsing and legacy compatibility adapters.  It is
    deliberately lexical and closed-world: unknown text never creates a
    taxonomy concept.
    """
    source = taxonomy or _SKILLS
    matches: list[tuple[int, int, TaxonomyRef]] = []
    for concept_id, (label, aliases) in source.items():
        for alias in aliases:
            match = re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.I)
            if match:
                matches.append(
                    (
                        match.start(),
                        match.end(),
                        TaxonomyRef(
                            conceptId=concept_id,
                            scheme="internal",
                            taxonomyVersion=taxonomy_version,
                            label=label,
                        ),
                    )
                )
                break
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2].concept_id))
    resolved: list[TaxonomyRef] = []
    seen: set[str] = set()
    for _, _, concept in matches:
        if concept.concept_id not in seen:
            resolved.append(concept)
            seen.add(concept.concept_id)
    return resolved

_GPA_RE = re.compile(
    r"\b(?:GPA|CPA|Điểm\s*(?:GPA)?)\s*(?:>=|>=|≥|tối\s*thiểu|từ|\s*:\s*)?\s*(?P<thresh>\d+(?:\.\d+)?)\s*(?:/|\s*trên\s*)(?P<scale>\d+(?:\.\d+)?)",
    re.I,
)
_GPA_STANDALONE_RE = re.compile(
    r"\b(?:GPA|CPA)\s*(?:>=|>=|≥|tối\s*thiểu|từ|\s*:\s*)\s*(?P<thresh>\d+(?:\.\d+)?)",
    re.I,
)
_LANG_CERT_RE = re.compile(
    r"\b(?P<cred>IELTS|TOEIC|TOEFL|JLPT|HSK)\s*(?:>=|>=|≥|từ|\s*:\s*)?\s*(?P<thresh>\d+(?:\.\d+)?|\b[A-C][1-2]\b|\bN[1-5]\b)\s*(?:\+)?",
    re.I,
)
_EQUIV_RE = re.compile(
    r"\b(?:hoặc\s*tương\s*đương|or\s*equivalent|tương\s*đương|equivalent)\b",
    re.I,
)
_PROGRAMMING_FOUNDATION_RE = re.compile(
    r"\b(?:kiến thức nền tảng (?:về )?lập trình|"
    r"nền tảng lập trình (?:vững chắc|tốt|cơ bản)|"
    r"programming foundation|software (?:engineering )?fundamentals|coding fundamentals)\b",
    re.I,
)
_STUDENT_STATUS_DISJUNCTION = re.compile(
    r"(?P<opt1>sinh viên\s+năm\s+(?:4|cuối)|final-year\s+student|undergraduate)\s*"
    r"(?:,|\s+hoặc|\s+or|/)\s*"
    r"(?P<opt2>mới\s+tốt\s+nghiệp(?:[^,\n.]*)|recent\s+graduate(?:[^,\n.]*)|fresh\s+graduate(?:[^,\n.]*))",
    re.I,
)
_RESEARCH_COMPETITION_DISJUNCTION = re.compile(
    r"(?P<opt1>(?:từng\s+tham\s+gia\s+)?nghiên\s+cứu\s+khoa\s+học|scientific\s+research)\s*"
    r"(?:,|\s+hoặc|\s+or|/)\s*"
    r"(?P<opt2>(?:từng\s+)?(?:tham\s+gia\s+)?(?:cuộc\s+thi\s+AI|thi\s+AI)|AI\s+competition|hackathon)",
    re.I,
)

PARSER_VERSION = "deterministic-jd-v4"
_HEADERS = {
    "requirements": {
        "requirements",
        "qualifications",
        "minimum qualifications",
        "basic qualifications",
        "required qualifications",
        "required skills",
        "skills and qualifications",
        "education and experience",
        "what you bring",
        "what were looking for",
        "what we are looking for",
        "candidate requirements",
        "candidate constraints",
        "technical constraints",
        "constraints",
        "candidate profile",
        "profile",
        "what you need",
        "what you will bring",
        "who you are",
        "ideal candidate",
        "yeu cau",
        "yeu cau ung vien",
        "yeu cau ung tuyen",
        "yeu cau cong viec",
        "rang buoc ung vien",
        "tieu chuan ung vien",
        "tieu chuan",
        "ky nang can co",
    },
    "preferred": {"preferred", "nice to have", "plus", "bonus", "uu tien"},
    "responsibilities": {
        "responsibilities",
        "key responsibilities",
        "responsibilities and duties",
        "duties",
        "what you will do",
        "what youll do",
        "what you ll do",
        "the role",
        "your role",
        "about the role",
        "about the job",
        "job description",
        "mo ta cong viec",
        "trach nhiem",
        "nhiem vu",
        "vai tro",
    },
    "benefits": {
        "benefits",
        "benefits and perks",
        "compensation and benefits",
        "what we offer",
        "perks",
        "why join us",
        "quyen loi",
        "phuc loi",
        "dai ngo",
        "che do dai ngo",
    },
    "location": {"location", "dia diem", "dia diem lam viec"},
    # This is a section boundary only.  Without it, a requirements section can
    # accidentally consume application instructions at the end of a Vietnamese JD.
    "application": {"how to apply", "cach thuc ung tuyen", "cach ung tuyen"},
}


def _key(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = value.replace("đ", "d")
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip()


def _evidence_id(kind: str, start: int, end: int) -> str:
    return f"ev-jd-{kind}-{sha1(f'{start}:{end}'.encode(), usedforsecurity=False).hexdigest()[:12]}"


def _lines(text: str) -> list[tuple[int, str]]:
    return [(match.start(), match.group().strip()) for match in re.finditer(r"(?m)^.*$", text)]


def _heading(line: str) -> str | None:
    line = re.sub(r"^(?:[-•o#*]+\s*)+", "", line).rstrip(":").strip()
    key = _key(line)
    # Recruitment posts commonly decorate a heading (e.g. "Yêu cầu ứng tuyển"
    # or "Quyền lợi dành cho bạn").  Match the known heading as a complete
    # leading phrase, never as an arbitrary substring in normal prose.
    return next(
        (
            kind
            for kind, values in _HEADERS.items()
            if any(key == value or key.startswith(f"{value} ") for value in values)
        ),
        None,
    )


def _ranges(text: str) -> dict[str, tuple[int, int]]:
    headings = [
        (offset, kind) for offset, line in _lines(text) if (kind := _heading(line)) and kind != "preferred"
    ]
    result: dict[str, tuple[int, int]] = {}
    for index, (start, kind) in enumerate(headings):
        # A company introduction can reuse "Mô tả công việc" before the actual
        # task list.  The later heading is the structured section candidates
        # must review, so retain the last occurrence.
        if kind in {"requirements", "responsibilities", "benefits"}:
            result[kind] = (start, headings[index + 1][0] if index + 1 < len(headings) else len(text))
    return result


def _bullet(line: str) -> tuple[int, str] | None:
    match = re.match(r"\s*(?:[-•o*]+\s*)+(?P<value>.+?)\s*$", line)
    if not match or not (value := match.group("value").strip()):
        return None
    return match.start("value"), value


class DeterministicJobDescriptionParser:
    """Evidence-grounded English/Vietnamese JD parser; no inferred LLM claims."""

    def __init__(
        self,
        taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        taxonomy_version: str = "internal-2026.1",
    ) -> None:
        self._taxonomy = taxonomy or _SKILLS
        self._taxonomy_version = taxonomy_version

    def parse(
        self, source: SourceDocument, *, extraction_version: str, artifact_key: str | None = None
    ) -> CanonicalJobDescription:
        mapper, evidence, ranges = EvidenceMapper(source), {}, _ranges(source.text)
        requirements = self._requirements(source, mapper, evidence, ranges.get("requirements"))
        title = self._title(source, ranges)
        company_name, _ = self._company_info(source, mapper, evidence)
        exp_min, exp_max, exp_raw = self._experience_range(source, mapper, evidence)
        sal_min, sal_max, sal_curr, sal_period, sal_neg, sal_raw = self._salary_info(
            source, mapper, evidence
        )

        return CanonicalJobDescription(
            schemaVersion="1.0",
            jobTitle=title,
            companyName=company_name,
            careerClassifications=self._classifications(requirements, title),
            seniority=self._seniority(title or "", source.text),
            employmentType=self._employment_type(source.text),
            workMode=self._work_mode(source.text),
            location=self._location(source),
            experienceMinYears=exp_min,
            experienceMaxYears=exp_max,
            experienceRaw=exp_raw,
            salaryMin=sal_min,
            salaryMax=sal_max,
            salaryCurrency=sal_curr,
            salaryPeriod=sal_period,
            salaryNegotiable=sal_neg,
            salaryRaw=sal_raw,
            responsibilities=self._texts(
                source, mapper, evidence, ranges.get("responsibilities"), "responsibility"
            ),
            requirements=requirements,
            benefits=self._texts(source, mapper, evidence, ranges.get("benefits"), "benefit"),
            evidence=list(evidence.values()),
            parsing=ParsingMetadata(
                parserVersion=PARSER_VERSION,
                extractionVersion=extraction_version,
                parsedAt=datetime.now(UTC),
                status="review_required",
                sourceArtifactKey=artifact_key,
            ),
        )

    def _requirements(self, source, mapper, evidence, section):
        if not section:
            return []
        start, end, priority, result = *section, "must_have", []
        doc_negated_topics = _extract_negated_topics(source.text)

        for offset, line in _lines(source.text[start:end]):
            if _heading(line) == "preferred":
                priority = "preferred"
                continue
            bullet = _bullet(line)
            # Production JDs frequently put an entire qualification paragraph
            # under a heading rather than using bullets.  Preserve that
            # paragraph as one evidence-grounded requirement instead of
            # silently dropping it.
            if bullet:
                relative_start, value = bullet
            elif line and not _heading(line):
                relative_start, value = 0, line.strip()
            else:
                continue

            key_text = _key(value)
            is_negated_line = bool(_PRIORITY_NEGATION_RE.search(key_text))
            has_preferred_contrast = bool(_CONTRAST_PREFERRED_RE.search(key_text))

            # Negated requirements without explicit preferred contrast are not requirements
            if is_negated_line and not has_preferred_contrast:
                continue

            # Document-level negation (conservative concept-matched suppression)
            if doc_negated_topics and _is_negated_by_document(value, doc_negated_topics):
                continue

            # Priority detection
            line_priority = _detect_priority(value, priority)
            is_preferred_line = line_priority == "preferred"

            absolute_start = start + offset + relative_start
            absolute_end = absolute_start + len(value)

            # Negative check: "Willing to learn"
            is_willing_to_learn = bool(
                re.search(r"\b(?:willing to learn|sẵn sàng học|yêu thích học hỏi)\b", value, re.I)
            )
            if is_willing_to_learn:
                ref = _evidence_id("requirement", absolute_start, absolute_end)
                evidence[ref] = mapper.from_offsets(
                    evidence_id=ref, char_start=absolute_start, char_end=absolute_end
                )
                result.append(
                    JobRequirement(
                        requirementId=f"req-other-{absolute_start}",
                        kind="other",
                        priority="preferred",
                        rawLabel=value,
                        groupOperator="atomic",
                        evidenceRefs=[ref],
                    )
                )
                continue

            # 1. GPA requirement
            gpa_match = _GPA_RE.search(value) or _GPA_STANDALONE_RE.search(value)
            if gpa_match:
                thresh_val = float(gpa_match.group("thresh"))
                scale_val = (
                    float(gpa_match.group("scale"))
                    if "scale" in gpa_match.groupdict() and gpa_match.group("scale")
                    else (4.0 if thresh_val <= 4.0 else 10.0)
                )
                op = "gte"
                if ">" in value and ">=" not in value and "≥" not in value:
                    op = "gt"
                ref = _evidence_id("requirement", absolute_start, absolute_end)
                evidence[ref] = mapper.from_offsets(
                    evidence_id=ref, char_start=absolute_start, char_end=absolute_end
                )
                result.append(
                    JobRequirement(
                        requirementId=f"req-education-gpa-{absolute_start}",
                        kind="education",
                        priority=line_priority,
                        operator=op,
                        threshold=thresh_val,
                        scale=scale_val,
                        rawLabel=value,
                        groupOperator="atomic",
                        evidenceRefs=[ref],
                    )
                )
                continue

            # 2. Language certificate requirement
            lang_match = _LANG_CERT_RE.search(value)
            if lang_match:
                cred = lang_match.group("cred").upper()
                raw_thresh = lang_match.group("thresh")
                try:
                    thresh = float(raw_thresh)
                except ValueError:
                    thresh = None
                equiv = bool(_EQUIV_RE.search(value))
                ref = _evidence_id("requirement", absolute_start, absolute_end)
                evidence[ref] = mapper.from_offsets(
                    evidence_id=ref, char_start=absolute_start, char_end=absolute_end
                )
                result.append(
                    JobRequirement(
                        requirementId=f"req-language-{absolute_start}",
                        kind="language",
                        priority=line_priority,
                        credential=cred,
                        operator="gte",
                        threshold=thresh,
                        equivalentAllowed=equiv,
                        rawLabel=value,
                        groupOperator="atomic",
                        evidenceRefs=[ref],
                    )
                )
                continue

            # 3. Student status disjunction (e.g. "Sinh viên năm 4 hoặc mới tốt nghiệp")
            student_match = _STUDENT_STATUS_DISJUNCTION.search(value)
            if student_match:
                group_id = f"req-group-any-{absolute_start}"
                opt1_start = absolute_start + student_match.start("opt1")
                opt1_end = absolute_start + student_match.end("opt1")
                ref1 = _evidence_id("requirement", opt1_start, opt1_end)
                evidence[ref1] = mapper.from_offsets(
                    evidence_id=ref1, char_start=opt1_start, char_end=opt1_end
                )
                result.append(
                    JobRequirement(
                        requirementId=f"req-education-{opt1_start}",
                        kind="education",
                        priority=line_priority,
                        groupId=group_id,
                        groupOperator="any_of",
                        rawLabel=source.text[opt1_start:opt1_end],
                        evidenceRefs=[ref1],
                    )
                )

                opt2_start = absolute_start + student_match.start("opt2")
                opt2_end = absolute_start + student_match.end("opt2")
                ref2 = _evidence_id("requirement", opt2_start, opt2_end)
                evidence[ref2] = mapper.from_offsets(
                    evidence_id=ref2, char_start=opt2_start, char_end=opt2_end
                )
                result.append(
                    JobRequirement(
                        requirementId=f"req-education-{opt2_start}",
                        kind="education",
                        priority=line_priority,
                        groupId=group_id,
                        groupOperator="any_of",
                        rawLabel=source.text[opt2_start:opt2_end],
                        evidenceRefs=[ref2],
                    )
                )
                continue

            # 4. Research / Competition disjunction
            rc_match = _RESEARCH_COMPETITION_DISJUNCTION.search(value)
            if rc_match:
                group_id = f"req-group-any-{absolute_start}"
                rc_priority = "preferred" if is_preferred_line or priority == "preferred" else line_priority

                opt1_start = absolute_start + rc_match.start("opt1")
                opt1_end = absolute_start + rc_match.end("opt1")
                ref1 = _evidence_id("requirement", opt1_start, opt1_end)
                evidence[ref1] = mapper.from_offsets(
                    evidence_id=ref1, char_start=opt1_start, char_end=opt1_end
                )
                result.append(
                    JobRequirement(
                        requirementId=f"req-experience-{opt1_start}",
                        kind="experience",
                        priority=rc_priority,
                        groupId=group_id,
                        groupOperator="any_of",
                        rawLabel=source.text[opt1_start:opt1_end],
                        evidenceRefs=[ref1],
                    )
                )

                opt2_start = absolute_start + rc_match.start("opt2")
                opt2_end = absolute_start + rc_match.end("opt2")
                ref2 = _evidence_id("requirement", opt2_start, opt2_end)
                evidence[ref2] = mapper.from_offsets(
                    evidence_id=ref2, char_start=opt2_start, char_end=opt2_end
                )
                result.append(
                    JobRequirement(
                        requirementId=f"req-experience-{opt2_start}",
                        kind="experience",
                        priority=rc_priority,
                        groupId=group_id,
                        groupOperator="any_of",
                        rawLabel=source.text[opt2_start:opt2_end],
                        evidenceRefs=[ref2],
                    )
                )
                continue

            # 5. General programming foundation without specific technology
            prog_match = _PROGRAMMING_FOUNDATION_RE.search(value)
            if prog_match:
                has_known_tech = any(
                    any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", value, re.I) for alias in aliases)
                    for aliases in (t[1] for t in self._taxonomy.values())
                )
                if not has_known_tech:
                    ref = _evidence_id("requirement", absolute_start, absolute_end)
                    evidence[ref] = mapper.from_offsets(
                        evidence_id=ref, char_start=absolute_start, char_end=absolute_end
                    )
                    result.append(
                        JobRequirement(
                            requirementId=f"req-skill-{absolute_start}",
                            kind="skill",
                            concept=None,  # DO NOT hallucinate Python / Java / C++
                            priority=line_priority,
                            rawLabel=value,
                            groupOperator="atomic",
                            evidenceRefs=[ref],
                        )
                    )
                    continue

            # 6. Generic certification, education, or language
            is_cert = bool(
                re.search(
                    r"\b(?:certified|certification|certificate|chứng chỉ|practitioner)\b",
                    value,
                    re.I,
                )
            )
            is_edu = bool(
                re.search(
                    r"\b(?:bachelor|master|phd|degree|b\.s|m\.s|university|college|"
                    r"cử nhân|thạc sĩ|tiến sĩ|đại học|cao đẳng|tốt nghiệp)\b",
                    value,
                    re.I,
                )
            )
            is_lang = bool(
                re.search(
                    r"\b(?:english|tiếng anh|ielts|toeic|toefl|b1|b2|c1|c2|"
                    r"japanese|tiếng nhật|jlpt|n1|n2|n3|chinese|tiếng trung)\b",
                    value,
                    re.I,
                )
            )
            if is_cert or is_edu or is_lang:
                ref = _evidence_id("requirement", absolute_start, absolute_end)
                evidence[ref] = mapper.from_offsets(
                    evidence_id=ref, char_start=absolute_start, char_end=absolute_end
                )
                kind = "education" if is_edu else ("language" if is_lang else "other")
                result.append(
                    JobRequirement(
                        requirementId=f"req-{kind}-{absolute_start}",
                        kind=kind,
                        priority=line_priority,
                        rawLabel=value,
                        minimumExperienceMonths=self._experience_months(value),
                        groupOperator="atomic",
                        evidenceRefs=[ref],
                    )
                )
                continue

            # 7. Check taxonomy skills
            found_skills = []
            for concept_id, (label, aliases) in self._taxonomy.items():
                for alias in aliases:
                    for match in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", value, re.I):
                        found_skills.append((concept_id, label, match.start(), match.end()))
                        break

            if found_skills:
                found_skills.sort(key=lambda x: x[2])
                unique_skills = []
                last_end = -1
                for c_id, lbl, s_start, s_end in found_skills:
                    if s_start >= last_end:
                        unique_skills.append((c_id, lbl, s_start, s_end))
                        last_end = s_end

                if len(unique_skills) > 1:
                    has_disjunction = bool(re.search(r"\b(?:hoặc|hay|or)\b", value, re.I))
                    group_op = "any_of" if has_disjunction else "all_of"
                    group_id = f"req-group-{('any' if has_disjunction else 'all')}-{absolute_start}"
                else:
                    group_op = "atomic"
                    group_id = None

                exp_months = self._experience_months(value)
                op_val = "gte" if exp_months is not None else None

                if len(unique_skills) > 1:
                    ref = _evidence_id("requirement", absolute_start, absolute_end)
                    evidence[ref] = mapper.from_offsets(
                        evidence_id=ref, char_start=absolute_start, char_end=absolute_end
                    )
                    result.append(
                        JobRequirement(
                            requirementId=group_id,
                            kind="skill",
                            priority=line_priority,
                            rawLabel=value,
                            atomicConcepts=[
                                TaxonomyRef(
                                    conceptId=c_id,
                                    scheme="internal",
                                    taxonomyVersion=self._taxonomy_version,
                                    label=lbl,
                                )
                                for c_id, lbl, _, _ in unique_skills
                            ],
                            minimumExperienceMonths=exp_months,
                            operator=op_val,
                            groupId=group_id,
                            groupOperator=group_op,
                            evidenceRefs=[ref],
                        )
                    )
                    continue

                for c_id, lbl, s_start, s_end in unique_skills:
                    skill_start = absolute_start + s_start
                    skill_end = absolute_start + s_end
                    ref = _evidence_id("requirement", skill_start, skill_end)
                    evidence[ref] = mapper.from_offsets(
                        evidence_id=ref, char_start=skill_start, char_end=skill_end
                    )
                    result.append(
                        JobRequirement(
                            requirementId=f"req-{c_id}-{skill_start}",
                            kind="skill",
                            priority=line_priority,
                            concept=TaxonomyRef(
                                conceptId=c_id,
                                scheme="internal",
                                taxonomyVersion=self._taxonomy_version,
                                label=lbl,
                            ),
                            rawLabel=source.text[skill_start:skill_end],
                            minimumExperienceMonths=exp_months,
                            operator=op_val,
                            groupId=group_id,
                            groupOperator=group_op,
                            evidenceRefs=[ref],
                        )
                    )
                continue

            # 8. Fallback
            ref = _evidence_id("requirement", absolute_start, absolute_end)
            evidence[ref] = mapper.from_offsets(
                evidence_id=ref, char_start=absolute_start, char_end=absolute_end
            )
            exp_months = self._experience_months(value)
            kind = (
                "experience"
                if (exp_months is not None or re.search(r"\b(?:experience|kinh nghiệm)\b", value, re.I))
                else "other"
            )
            op_val = "gte" if exp_months is not None else None
            result.append(
                JobRequirement(
                    requirementId=f"req-{kind}-{absolute_start}",
                    kind=kind,
                    priority=line_priority,
                    rawLabel=value,
                    minimumExperienceMonths=exp_months,
                    operator=op_val,
                    groupOperator="atomic",
                    evidenceRefs=[ref],
                )
            )
        return result

    def _texts(self, source, mapper, evidence, section, kind):
        if not section:
            return []
        start, end = section
        result = []
        for offset, line in _lines(source.text[start:end]):
            bullet = _bullet(line)
            if _heading(line):
                continue
            if bullet:
                relative_start, value = bullet
            elif kind in {"responsibility", "benefit"} and line:
                relative_start, value = 0, line
            else:
                continue
            if len(value) < 3:
                continue
            absolute_start, absolute_end = (
                start + offset + relative_start,
                start + offset + relative_start + len(value),
            )
            ref = _evidence_id(kind, absolute_start, absolute_end)
            evidence[ref] = mapper.from_offsets(
                evidence_id=ref, char_start=absolute_start, char_end=absolute_end
            )
            result.append(GroundedJobText(text=value, evidenceRefs=[ref]))
        return result

    @staticmethod
    def _title(source: SourceDocument, ranges: dict[str, tuple[int, int]]) -> str | None:
        for _, line in _lines(source.text):
            label, separator, value = line.partition(":")
            if (
                separator
                and _key(label) in {"job title", "position", "vi tri", "chuc danh"}
                and value.strip()
            ):
                return value.strip()
        # Titles are often a standalone line, or occur in a recruitment
        # sentence rather than an explicit "Job title:" label.  Select only
        # role-shaped phrases, never generic headings such as "description".
        role_words = r"(?:engineer|developer|designer|manager|specialist|consultant|coordinator|analyst|architect|administrator|recruiter|representative|director|intern|assistant|officer|technician)"
        for _, line in _lines(source.text):
            candidate = line.strip(" -:\t")
            normalized = _key(candidate)
            if not candidate or _heading(candidate) or len(candidate) > 100:
                continue
            if re.fullmatch(rf"(?:[a-z0-9 .&/+-]+\s+)?{role_words}(?:\s+[a-z0-9 .&/+-]+)?", normalized):
                return candidate
        patterns = (
            rf"(?:looking|searching|hiring)\s+for\s+(?:an?\s+)?(?:talented\s+)?(?P<title>[a-z0-9 .&/+-]*{role_words})",
            rf"(?:join\s+(?:us|our team)\s+as\s+)(?:an?\s+)?(?P<title>[a-z0-9 .&/+-]*{role_words})",
            rf"(?:position|role)\s+(?:is|for)\s+(?:an?\s+)?(?P<title>[a-z0-9 .&/+-]*{role_words})",
        )
        for match in re.finditer(r"(?s).{0,120}", source.text.lower()):
            excerpt = match.group()
            for pattern in patterns:
                found = re.search(pattern, excerpt)
                if found:
                    return found.group("title").strip(" .,;:-")
        first_section = min((start for start, _ in ranges.values()), default=len(source.text))
        for block in source.blocks:
            if block.char_start >= first_section:
                continue
            text = block.text.strip()
            if 3 <= len(text) <= 80 and not _heading(text) and re.search(role_words, text, re.I):
                return text
        return None

    @staticmethod
    def _experience_months(text: str) -> int | None:
        normalized = _key(text)
        match = re.search(
            r"(?:it nhat\s*|at least\s*|toi thieu\s*)?(\d+)\+?\s*"
            r"(?:years?(?:\s+of)?(?:\s+[a-z0-9 ]+)?(?:\s+experience)?|"
            r"nam(?:\s+[a-z0-9 ]+)?(?:\s+kinh\s+nghiem)?)",
            normalized,
        )
        if match:
            return int(match.group(1)) * 12
        match_months = re.search(
            r"(?:it nhat\s*|at least\s*|toi thieu\s*)?(\d+)\+?\s*"
            r"(?:months?(?:\s+of)?|thang(?:\s+kinh\s+nghiem)?)",
            normalized,
        )
        if match_months:
            return int(match_months.group(1))
        return None

    @staticmethod
    def _company_info(
        source: SourceDocument, mapper: EvidenceMapper, evidence: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        excluded_labels = {
            "dia diem", "dia diem lam viec", "location", "address",
            "yeu cau", "yeu cau ung vien", "yeu cau cong viec", "requirements",
            "quyen loi", "phuc loi", "benefits",
            "mo ta cong viec", "job description", "description",
            "vi tri", "chuc danh", "job title", "position",
            "thoi gian", "thoi gian lam viec", "working time", "working hours",
            "muc luong", "luong", "salary", "compensation",
            "lien he", "contact", "cach thuc ung tuyen", "application",
            "uu tien", "preferred", "hinh thuc", "hinh thuc lam viec",
            "so luong", "kinh nghiem", "experience", "ngay dang",
            "ha noi", "hanoi", "ho chi minh", "hcm", "tphcm", "da nang", "viet nam", "vietnam",
        }
        lines = _lines(source.text)
        # 1. Explicit label pattern: "Company: ABC", "Công ty: ABC", "Tên công ty: ABC"
        for offset, line in lines:
            if ":" not in line:
                continue
            label, _, value = line.partition(":")
            norm_label = _key(label)
            if norm_label in {"company", "cong ty", "ten cong ty", "company name"}:
                cleaned = value.strip(" -•\t")
                if cleaned and len(cleaned) <= 100:
                    start = offset + line.index(value)
                    end = start + len(value)
                    ref = _evidence_id("company", start, end)
                    evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                    return cleaned, ref

        # 2. Header slogan/tagline pattern (e.g. "NSTAGE : Fun Lives On") in the first 15 lines
        for offset, line in lines[:15]:
            # Bullets are item lists (e.g. "- Hà Nội: ..."), never header slogans!
            if re.match(r"^\s*[-•*o]\s+", line):
                continue
            candidate = line.strip(" -•\t")
            if not candidate or _heading(candidate):
                continue
            if ":" in candidate:
                part1, sep, part2 = candidate.partition(":")
                norm1 = _key(part1)
                if norm1 in excluded_labels or any(norm1.startswith(ex + " ") for ex in excluded_labels):
                    continue
                words1 = part1.strip().split()
                if 1 <= len(words1) <= 5 and len(part1.strip()) <= 50 and part2.strip():
                    company = part1.strip()
                    start = offset
                    end = offset + len(line)
                    ref = _evidence_id("company", start, end)
                    evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                    return company, ref

        return None, None

    @staticmethod
    def _experience_range(
        source: SourceDocument, mapper: EvidenceMapper, evidence: dict[str, Any]
    ) -> tuple[int | None, int | None, str | None]:
        for offset, line in _lines(source.text):
            lower_line = line.lower()
            norm = _key(line)
            if not re.search(r"\b(?:kinh nghiem|experience|exp)\b", norm):
                continue

            # Strict inequality cases: "trên 5 năm" (> 5), "dưới 2 năm" (< 2), "hơn 3 năm", "over 5 years"
            # Schema cannot represent strict > or <.
            # Do NOT canonicalize to min=5 or max=2. Keep numeric range null and preserve experienceRaw + evidence.
            if re.search(r"\b(?:trên|tren|over|hơn|hon|>|dưới|duoi|less than|<)\s*\d+", lower_line):
                start = offset
                end = offset + len(line)
                ref = _evidence_id("experience", start, end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                return None, None, line.strip()

            # Range: "2 - 4 năm", "2-4 nam", "tu 3 den 5 nam", "from 2 to 4 years"
            m_range = re.search(
                r"(?:từ|tu\s+)?(\d+)\s*(?:-|–|—|đến|den|to)\s*(\d+)\s*(?:\+?\s*)?(?:năm|nam|years?)",
                lower_line,
            )
            if m_range:
                min_y = int(m_range.group(1))
                max_y = int(m_range.group(2))
                if min_y <= max_y:
                    start = offset
                    end = offset + len(line)
                    ref = _evidence_id("experience", start, end)
                    evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                    return min_y, max_y, line.strip()

            # Closed lower bound (>=): "ít nhất 2 năm", "tối thiểu 2 năm", "at least 2 years", "from 2 years", "3+ years"
            m_min = re.search(
                r"(?:(?:ít nhất|it nhat|tối thiểu|toi thieu|from|at least)\s*(\d+)\s*(?:\+|plus)?|(\d+)\s*(?:\+|plus))\s*(?:năm|nam|years?)\s*(?:kinh nghiệm|kinh nghiem|of experience|experience)?",
                lower_line,
            )
            if m_min:
                min_y = int(m_min.group(1) or m_min.group(2))
                start = offset
                end = offset + len(line)
                ref = _evidence_id("experience", start, end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                return min_y, None, line.strip()

        return None, None, None

    @staticmethod
    def _seniority(title: str, text: str) -> str | None:
        norm_title = _key(title)
        title_mapping = [
            ("intern", ("intern", "thuc tap")),
            ("fresher", ("fresher",)),
            ("junior", ("junior",)),
            ("mid", ("mid", "middle", "mid level")),
            ("senior", ("senior",)),
            ("lead", ("team lead", "tech lead", "lead")),
            ("manager", ("manager", "truong phong", "quan ly")),
        ]
        for name, aliases in title_mapping:
            for alias in aliases:
                if re.search(rf"\b{re.escape(alias)}\b", norm_title):
                    return name

        # Avoid false positives like "làm việc với Senior Manager" or "báo cáo cho Lead"
        for _, line in _lines(text):
            norm_line = _key(line)
            if any(norm_line.startswith(prefix) for prefix in ("cap bac", "level", "seniority", "chuc vu")):
                for name, aliases in title_mapping:
                    if any(re.search(rf"\b{re.escape(alias)}\b", norm_line) for alias in aliases):
                        return name
        return None

    @staticmethod
    def _employment_type(text: str) -> str | None:
        # Avoid inferring from working hours (e.g. "Thứ 2 - Thứ 6, 08:00 - 17:00")
        for _, line in _lines(text):
            norm = _key(line)
            if "gio lam viec" in norm or "thoi gian lam viec" in norm or "working hours" in norm or "thu 2" in norm:
                continue
            if re.search(r"\b(?:toan thoi gian|full[- ]?time)\b", norm):
                return "full_time"
            if re.search(r"\b(?:ban thoi gian|part[- ]?time)\b", norm):
                return "part_time"
            if re.search(r"\b(?:thuc tap|internship)\b", norm):
                return "internship"
            if re.search(r"\b(?:hop dong|contract)\b", norm):
                return "contract"
            if re.search(r"\b(?:thoi vu|temporary)\b", norm):
                return "temporary"
        return None

    @staticmethod
    def _work_mode(text: str) -> str | None:
        # Only direct evidence; do not infer from address or office snacks
        for _, line in _lines(text):
            norm = _key(line)
            if any(norm.startswith(p) for p in ("dia diem", "dia chi", "address", "location", "ha noi", "ho chi minh")):
                continue
            if "do an" in norm or "snack" in norm or "an nhe" in norm:
                continue
            if re.search(r"\b(?:remote|tu xa)\b", norm):
                return "remote"
            if re.search(r"\bhybrid\b", norm):
                return "hybrid"
            if re.search(r"\b(?:on[- ]?site|lam viec tai van phong)\b", norm):
                return "on_site"
        return None

    @staticmethod
    def _salary_info(
        source: SourceDocument, mapper: EvidenceMapper, evidence: dict[str, Any]
    ) -> tuple[int | None, int | None, str | None, str | None, bool | None, str | None]:
        for offset, line in _lines(source.text):
            lower_line = line.lower()
            norm = _key(line)
            if not any(k in norm for k in ("luong", "salary", "thu nhap", "compensation", "$", "usd", "vnd", "trieu")):
                continue

            # Explicit fixed / non-negotiable keywords
            is_fixed = bool(
                re.search(
                    r"\b(?:co dinh|cố định|khong thoa thuan|không thỏa thuận|fixed|non[- ]?negotiable)\b",
                    lower_line,
                )
            )
            # Explicit negotiable keywords
            is_negotiable = bool(
                re.search(
                    r"\b(?:thoa thuan|thỏa thuận|thuong luong|thương lượng|negotiable)\b",
                    lower_line,
                )
            )

            neg_flag: bool | None
            if is_fixed:
                neg_flag = False
            elif is_negotiable:
                neg_flag = True
            else:
                neg_flag = None

            # Negotiable keywords without numbers
            if is_negotiable and not re.search(r"\d", norm):
                start = offset
                end = offset + len(line)
                ref = _evidence_id("salary", start, end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                return None, None, None, None, True, line.strip()

            # Competitive / Attractive without numbers (must NOT be treated as negotiable)
            if re.search(r"\b(?:canh tranh|competitive|hap dan)\b", norm) and not re.search(r"\d", norm):
                start = offset
                end = offset + len(line)
                ref = _evidence_id("salary", start, end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                return None, None, None, None, None, line.strip()

            # VND millions: "20 - 30 triệu/tháng", "20-30 trieu", "20 - 30 tr", "25 triệu cố định"
            m_vnd_range = re.search(
                r"(\d+(?:[.,]\d+)?)\s*(?:-|–|—|đến|den|to)\s*(\d+(?:[.,]\d+)?)\s*(?:triệu|trieu|tr|m)\b",
                lower_line,
            )
            m_vnd_single = re.search(
                r"(\d+(?:[.,]\d+)?)\s*(?:triệu|trieu|tr|m)\b",
                lower_line,
            )
            if m_vnd_range:
                min_v = int(float(m_vnd_range.group(1).replace(",", ".")) * 1_000_000)
                max_v = int(float(m_vnd_range.group(2).replace(",", ".")) * 1_000_000)
                period = "year" if "năm" in lower_line or "year" in lower_line else "month"
                start = offset
                end = offset + len(line)
                ref = _evidence_id("salary", start, end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                return min_v, max_v, "VND", period, neg_flag, line.strip()
            elif m_vnd_single:
                val = int(float(m_vnd_single.group(1).replace(",", ".")) * 1_000_000)
                period = "year" if "năm" in lower_line or "year" in lower_line else "month"
                start = offset
                end = offset + len(line)
                ref = _evidence_id("salary", start, end)
                evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                if re.search(r"\b(?:tối đa|toi da|up to|tới|den)\b", lower_line):
                    return None, val, "VND", period, neg_flag, line.strip()
                elif re.search(r"\b(?:từ|tu|from|tối thiểu|toi thieu)\b", lower_line):
                    return val, None, "VND", period, neg_flag, line.strip()
                elif is_fixed:
                    return val, val, "VND", period, False, line.strip()
                else:
                    return val, None, "VND", period, neg_flag, line.strip()

            # USD: "$1,000 - $2,000 / month", "1000 - 2000 usd"
            if "$" in lower_line or "usd" in lower_line:
                m_usd = re.search(
                    r"(?:\$|usd)?\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(?:-|–|—|to)\s*(?:\$|usd)?\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(?:usd|\$)?",
                    lower_line,
                )
                if m_usd:
                    min_v = int(m_usd.group(1).replace(",", ""))
                    max_v = int(m_usd.group(2).replace(",", ""))
                    period = "year" if "year" in lower_line or "năm" in lower_line else "month"
                    start = offset
                    end = offset + len(line)
                    ref = _evidence_id("salary", start, end)
                    evidence[ref] = mapper.from_offsets(evidence_id=ref, char_start=start, char_end=end)
                    return min_v, max_v, "USD", period, neg_flag, line.strip()

        return None, None, None, None, None, None

    @staticmethod
    def _location(source: SourceDocument) -> str | None:
        lines = _lines(source.text)
        for index, (_, line) in enumerate(lines):
            key = _key(line)
            if key.startswith("location "):
                return line.split(":", 1)[1].strip() if ":" in line else None
            if _heading(line) == "location" and index + 1 < len(lines):
                val = line.split(":", 1)[1].strip() if ":" in line else ""
                if not val:
                    val = lines[index + 1][1].lstrip("-•*o ").split(":", 1)[0].strip()
                if val:
                    return val
        match = re.search(r"(?im)^\s*[-•*o]?\s*(hà nội|ha noi|hanoi|đà nẵng|da nang|ho chi minh city)\s*:", source.text)
        return match.group(1).title() if match else None

    @staticmethod
    def _classifications(requirements, job_title: str | None = None):
        del job_title  # Canonical JD v1 has no evidenceRefs for its title.
        skill_evidence: dict[str, list[str]] = {}
        for item in requirements:
            concepts = [item.concept] if item.concept is not None else item.atomic_concepts
            for concept in concepts:
                existing = skill_evidence.setdefault(concept.concept_id, [])
                existing.extend(ref for ref in item.evidence_refs if ref not in existing)

        results = classify_career(
            skill_evidence,
            [],
            minimum_skill_signals=1,
            include_ancestors=False,
        )
        return [
            CareerClassification(
                code=item.code,
                label=item.label,
                dimension=item.dimension,
                taxonomyVersion=item.taxonomy_version,
                confidence=item.confidence,
                evidenceRefs=list(item.evidence_refs),
                isPrimary=item.is_primary,
            )
            for item in results
        ]
