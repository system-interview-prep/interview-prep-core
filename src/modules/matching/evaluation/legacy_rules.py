from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from src.core.trace_logging import trace_event
from src.modules.job_descriptions.schemas import requirement_list_is_disjunctive
from src.modules.matching.domain.schemas import (
    NOT_EVIDENCED_REASON_CODES,
    ConceptResult,
    RequirementResult,
    UnresolvedRequirement,
)
from src.modules.matching.evaluation.language_equivalence import (
    cefr_level_for_score,
    cefr_level_in_text,
    cefr_meets,
    cefr_rank,
    is_english_credential,
    scale_is_unambiguous,
    score_is_on_scale,
)
from src.modules.matching.retrieval.evidence_retrieval import (
    EvidenceCandidate,
    is_named_technology,
    retrieve_semantic_evidence,
)
from src.modules.taxonomy.facade import SKILL_CATALOG, alias_pattern
from src.modules.user_cvs.schemas import CanonicalResume, EvidenceSpan, TaxonomyRef

RequirementCategory = Literal["skill", "language", "experience", "education", "other"]


@dataclass(frozen=True)
class EvaluatorSelection:
    name: str
    category: RequirementCategory


# Matched against ``_fold_scale`` output, so the Vietnamese here is already
# stripped of its diacritics: "điểm trung bình tích lũy" arrives as "diem trung
# binh tich luy".  The JD parser sets ``threshold``/``scale`` for these lines
# now, but a requirement can also reach this module from a cached parse or an
# LLM parse that did not, and the requirement is no less a GPA threshold then.
_GPA_SUBJECT = (
    r"(?:gpa|cpa|dtb"
    r"|diem\s+trung\s+binh(?:\s+tich\s+luy)?"
    r"|diem\s+tich\s+luy"
    r"|diem\s+tbtl)"
)
_GPA_RE = re.compile(
    rf"\b{_GPA_SUBJECT}(?:\s*(?:>=|>|=|tu|from|toi\s+thieu|dat|:))*\s*"
    r"(?P<value>\d+(?:\.\d+)?)"
    r"(?:\s*/\s*(?P<scale>\d+(?:\.\d+)?))?\b",
    re.I,
)
# "TOEFL iBT 95", "IELTS Academic 6.5", "TOEIC L&R 900": the variant of the
# test sits between its name and the score.  Matching only the name stopped at
# the variant and left the score unread, so a CV that states a result plainly
# counted as stating nothing.
_CREDENTIAL_VARIANT = (
    r"(?:\s*(?:ibt|pbt|cbt|academic|general|overall|band|score|lr|sw|l\s+r|s\s+w))*"
)
_CREDENTIAL_RE = re.compile(
    rf"\b(?P<credential>ielts|toeic|toefl|jlpt|hsk){_CREDENTIAL_VARIANT}\s*"
    r"(?:>=|>|=|tu|from|:)?\s*(?P<value>\d+(?:\.\d+)?)?\s*\+?",
    re.I,
)
_CERTIFICATION_TERMS = (
    "certification",
    "certifications",
    "certificate",
    "certificates",
    "certified",
    "professional license",
    "license",
    "licenses",
    "chung chi",
    "chung nhan",
)
_CERTIFICATION_STOPWORDS = {
    "a", "an", "and", "or", "the", "of", "in", "for", "with", "to",
    "required", "preferred", "relevant", "professional", "technical", "valid",
    "certification", "certifications", "certificate", "certificates", "certified",
    "license", "licenses", "chung", "chi", "nhan",
}

_PROGRAMMING_LANGUAGES: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "java": ("java",),
    "typescript": ("typescript",),
    "javascript": ("javascript",),
    "c++": ("c++", "cpp"),
    "c#": ("c#", "csharp"),
    "kotlin": ("kotlin",),
    "go": ("golang", "go language"),
    "rust": ("rust",),
}
_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "artificial intelligence": ("artificial intelligence", "ai"),
    "machine learning": ("machine learning", "ml"),
    "natural language processing": ("natural language processing", "nlp"),
    "generative ai": ("generative ai", "genai"),
    "large language models": ("large language model", "large language models", "llm"),
    "kubernetes": ("kubernetes", "k8s"),
    "c++": ("c++", "cpp"),
    "c#": ("c#", "csharp"),
    ".net": (".net", "dotnet", "asp.net", "aspnet"),
    "react": ("react", "reactjs"),
    "postgresql": ("postgresql", "postgres", "psql"),
    "aws": ("aws", "amazon web services"),
}
_STRENGTH_RANK = {"mention": 1, "claimed": 2, "applied": 3, "demonstrated": 4}
_SEMANTIC_MET_THRESHOLD = 0.38
_EXPERIENCE_DURATION_RE = re.compile(
    r"\b(?:minimum|at least|min(?:imum)?)?\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>years?|yrs?|months?|mos?)\b",
    re.I,
)

_EDUCATION_LEVELS: dict[str, int] = {
    "high school": 1,
    "secondary school": 1,
    "trung hoc": 1,
    "associate": 2,
    "college": 2,
    "cao dang": 2,
    "bachelor": 3,
    "b.sc": 3,
    "b.s": 3,
    "b.a": 3,
    "university": 3,
    "dai hoc": 3,
    "master": 4,
    "m.sc": 4,
    "m.s": 4,
    "ph.d": 5,
    "phd": 5,
    "doctorate": 5,
}


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    ascii_value = ascii_value.casefold().replace("đ", "d")
    return " ".join(re.findall(r"[a-z0-9+#.]+", ascii_value))


def _fold_scale(value: str) -> str:
    """``_normalize`` with ``/`` kept, because a scale is written ``7.0/10``.

    ``_normalize`` drops the slash so that "HTML/CSS" splits into two skill
    terms, which is right for a skill and wrong for a threshold: it turned
    "7.0/10" into the separate numbers 7.0 and 10, the scale was then guessed
    from the threshold's own size, and a 4.0-scale CV was compared against a
    10-point requirement it already cleared.
    """

    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    ascii_value = ascii_value.casefold().replace("đ", "d")
    return " ".join(re.findall(r"[a-z0-9+#./]+", ascii_value))


def _has_phrase(text: str, phrases: Iterable[str]) -> bool:
    normalized = _normalize(text).replace(".", " ")
    padded = f" {normalized} "
    return any(f" {_normalize(phrase).replace('.', ' ')} " in padded for phrase in phrases)


# How a posting asks for someone who is still studying, or has only just
# stopped.  "Sinh viên năm cuối" is the ordinary Vietnamese for it and was
# missing, so "Sinh viên năm cuối chuyên ngành Công nghệ Thông tin" never
# reached the education evaluator at all: it fell through to the generic
# word-overlap one, which cited nothing and reported the requirement as absent
# from a CV that answers it in full - with a graduation date from years ago.
# The majors a posting means by "chuyên ngành Công nghệ Thông tin".  The list
# used to hold five English-leaning phrases, so a Vietnamese CV reading "Khoa
# học máy tính" or "Kỹ thuật phần mềm" did not answer a Vietnamese posting
# asking for exactly that.
_IT_FIELD_PHRASES = (
    "information technology",
    "software engineering",
    "computer science",
    "computer engineering",
    "information systems",
    "data science",
    "artificial intelligence",
    "cntt",
    "cong nghe thong tin",
    "khoa hoc may tinh",
    "ky thuat phan mem",
    "cong nghe phan mem",
    "ky thuat may tinh",
    "he thong thong tin",
    "an toan thong tin",
    "khoa hoc du lieu",
    "tri tue nhan tao",
    "mang may tinh",
    "tin hoc",
)

_STUDENT_STATUS_PHRASES = (
    "final year",
    "final-year",
    "final year student",
    "undergraduate student",
    "sinh vien nam 3",
    "sinh vien nam 4",
    "sinh vien nam cuoi",
    "nam cuoi",
    "nam 3 nam 4",
    "sap tot nghiep",
    "chuan bi tot nghiep",
    "dang la sinh vien",
    "dang theo hoc",
    "recent graduate",
    "fresh graduate",
    "moi tot nghiep",
    "vua tot nghiep",
)


def _required_education_level(text: str) -> int | None:
    normalized = _normalize(text).replace(".", " ")
    if not any(
        _has_phrase(normalized, (marker,))
        for marker in ("or higher", "and above", "tro len", "above")
    ):
        return None
    matched = [level for label, level in _EDUCATION_LEVELS.items() if _has_phrase(normalized, (label,))]
    return max(matched) if matched else None


def _education_level(degree: str | None, evidence_text: str = "") -> int | None:
    text = f"{degree or ''} {evidence_text}"
    normalized = _normalize(text).replace(".", " ")
    matched = [level for label, level in _EDUCATION_LEVELS.items() if _has_phrase(normalized, (label,))]
    return max(matched) if matched else None


def _matching_evidence(
    resume: CanonicalResume,
    phrases: Iterable[str],
) -> list[EvidenceSpan]:
    return [item for item in resume.evidence if _has_phrase(item.text, phrases)]


def _catalog_aliases(concept_id: str) -> tuple[str, ...]:
    """The shared catalogue's own wordings for a concept.

    ``_CONCEPT_ALIASES`` below is a hand-kept table of thirteen entries that
    duplicates, badly, the catalogue every parser already matches CVs and
    postings against.  Where the two disagree the requirement loses: the
    catalogue lists "algorithms", "cấu trúc dữ liệu" and "giải thuật" for
    ``skill-data-structures-algorithms``, the table lists nothing, so only the
    full English label "Data Structures and Algorithms" was ever searched for
    and a CV certifying "Elementary algorithms, intermediate algorithms" did
    not evidence it.  Read the catalogue first; the table stays as an override
    for the abbreviations ("k8s", "psql") it adds on top.
    """
    entry = SKILL_CATALOG.get(concept_id)
    return entry.aliases if entry else ()


def _alias_pattern_evidence(
    resume: CanonicalResume,
    concept_id: str,
    phrases: Iterable[str],
) -> list[EvidenceSpan]:
    """Evidence found by the same matcher that extracts skills from a CV.

    ``_has_phrase`` compares whole normalised tokens, so "HTML5" does not
    answer "HTML" and "Vue 3" does not answer "Vue": the posting asks for the
    technology and the CV names its version.  ``alias_pattern`` is the matcher
    the CV and JD parsers already use and it reads both, so running it here
    makes one CV line mean the same thing on both sides of the match.  Purely
    additive - it widens what counts as a mention, never narrows it.
    """
    aliases = [alias for alias in phrases if alias.strip()]
    if concept_id not in SKILL_CATALOG and len(aliases) <= 1:
        return []
    patterns = [alias_pattern(alias) for alias in aliases]
    return [
        item
        for item in resume.evidence
        if any(pattern.search(item.text) for pattern in patterns)
    ]


def _dedupe_evidence(items: Iterable[EvidenceSpan]) -> list[EvidenceSpan]:
    return list({item.evidence_id: item for item in items}.values())


def _slug(value: str) -> str:
    return "-".join(_normalize(value).split()) or "concept"


def _decompose_explicit_list(text: str) -> tuple[list[TaxonomyRef], str]:
    """Conservatively decompose only explicit conjunction/disjunction lists.

    Taxonomy-backed concepts from the JD parser are authoritative. This fallback
    exists for legacy canonical records that only retained ``rawLabel``; it does
    not split punctuation unless the text also contains an explicit boolean
    conjunction.
    """
    any_of = bool(re.search(r"\b(?:or|hoac|hay)\b", _normalize(text)))
    all_of = bool(re.search(r"\b(?:and|va)\b", _normalize(text)))
    if not any_of and not all_of:
        return [], "atomic"
    scope_parts = re.split(r"\b(?:about|of|in|ve|về)\b", text, flags=re.I)
    has_scope_marker = len(scope_parts) > 1
    tail = scope_parts[-1].strip(" .:;-")
    parts = [
        part.strip(" .:;-()")
        for part in re.split(r"\s*,\s*|\s+\b(?:and|or|và|va|hoặc|hoac|hay)\b\s+", tail, flags=re.I)
    ]
    parts = [part for part in parts if part and len(_normalize(part).split()) <= 6]
    if len(parts) < 2:
        return [], "atomic"
    if not has_scope_marker and any(len(_normalize(part).split()) > 2 for part in parts):
        return [], "atomic"
    concepts = [
        TaxonomyRef(
            conceptId=f"legacy-{_slug(label)}",
            scheme="legacy-explicit-list",
            taxonomyVersion="1",
            label=label,
        )
        for label in parts
    ]
    return concepts, "any_of" if any_of else "all_of"


def _unique_refs(items: Iterable[EvidenceSpan]) -> list[str]:
    return list(dict.fromkeys(item.evidence_id for item in items))


def _required_experience_months(requirement: UnresolvedRequirement) -> int | None:
    if requirement.minimum_experience_months is not None:
        return requirement.minimum_experience_months
    match = _EXPERIENCE_DURATION_RE.search(_normalize(requirement.raw_label))
    if not match:
        return None
    value = float(match.group("value"))
    return round(value * 12) if match.group("unit").startswith(("year", "yr")) else round(value)


def _date_window(value: object) -> tuple[date, date] | None:
    if value is None or not hasattr(value, "value") or not hasattr(value, "precision"):
        return None
    parts = [int(part) for part in value.value.split("-")]
    year = parts[0]
    if value.precision == "year":
        return date(year, 1, 1), date(year, 12, 31)
    month = parts[1]
    if value.precision == "month":
        last_day = 31
        while last_day > 28:
            try:
                date(year, month, last_day)
                break
            except ValueError:
                last_day -= 1
        return date(year, month, 1), date(year, month, last_day)
    exact = date(year, month, parts[2])
    return exact, exact


def _month_keys(start: date, end: date) -> set[tuple[int, int]]:
    if end < start:
        return set()
    keys: set[tuple[int, int]] = set()
    cursor = date(start.year, start.month, 1)
    boundary = date(end.year, end.month, 1)
    while cursor <= boundary:
        keys.add((cursor.year, cursor.month))
        cursor = date(cursor.year + (cursor.month // 12), cursor.month % 12 + 1, 1)
    return keys


def _experience_duration_stats(resume: CanonicalResume) -> dict[str, object]:
    today = datetime.now(UTC).date()
    minimum_months: set[tuple[int, int]] = set()
    maximum_months: set[tuple[int, int]] = set()
    evidence_refs: list[str] = []
    timeline_seen = False
    timeline_uncertain = False
    intervals: list[tuple[int, int]] = []
    for employment in resume.employment:
        start_window = _date_window(employment.start_date)
        end_window = _date_window(employment.end_date)
        if employment.is_current and end_window is None:
            end_window = (today, today)
        refs = [*employment.evidence_refs]
        refs.extend(ref for achievement in employment.achievements for ref in achievement.evidence_refs)
        evidence_refs.extend(refs)
        if start_window is None or end_window is None:
            timeline_uncertain = True
            continue
        timeline_seen = True
        minimum_interval = _month_keys(start_window[1], end_window[0])
        maximum_interval = _month_keys(start_window[0], end_window[1])
        minimum_months.update(minimum_interval)
        maximum_months.update(maximum_interval)
        if maximum_interval:
            interval_start = min(year * 12 + month for year, month in maximum_interval)
            interval_end = max(year * 12 + month for year, month in maximum_interval)
            intervals.append((interval_start, interval_end))
    merged_interval_count = 0
    current_end: int | None = None
    for interval_start, interval_end in sorted(intervals):
        if current_end is None or interval_start > current_end + 1:
            merged_interval_count += 1
        current_end = max(current_end or interval_end, interval_end)
    return {
        "minimum_months": minimum_months,
        "maximum_months": maximum_months,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "timeline_seen": timeline_seen,
        "timeline_uncertain": timeline_uncertain,
        "merged_interval_count": merged_interval_count,
    }


def _evaluate_experience_duration(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    required_months = _required_experience_months(requirement)
    if required_months is None:
        return _result(
            requirement,
            status="unknown",
            reason_code="requirement_evaluator_unsupported",
            confidence=0.0,
            evidence_explanation="Không xác định được số tháng kinh nghiệm tối thiểu từ yêu cầu JD.",
        )

    stats = _experience_duration_stats(resume)
    minimum_months = stats["minimum_months"]
    maximum_months = stats["maximum_months"]
    evidence_refs = stats["evidence_refs"]
    timeline_seen = stats["timeline_seen"]
    timeline_uncertain = stats["timeline_uncertain"]
    if not timeline_seen:
        employment_coverage = (
            resume.parsing.canonical_section_coverage.get("employment")
            if resume.parsing
            else None
        )
        if employment_coverage in {"complete", "complete_empty"} and _raw_coverage(resume) == "complete":
            return _result(
                requirement,
                status="unknown",
                reason_code="experience_duration_evidence_missing",
                confidence=0.0,
                evidence_explanation=(
                    "CV chưa nêu timeline employment đủ rõ để xác nhận thời lượng kinh nghiệm; "
                    "cần hỏi ứng viên trước khi kết luận."
                ),
            )
        return _result(
            requirement,
            status="unknown",
            reason_code="experience_duration_evidence_missing",
            confidence=0.0,
            evidence_explanation="CV chưa có timeline employment đủ rõ để tính thời lượng kinh nghiệm.",
        )
    if len(minimum_months) >= required_months:
        return _result(
            requirement,
            status="met",
            reason_code="experience_duration_satisfied",
            evidence=[item for item in resume.evidence if item.evidence_id in evidence_refs],
            confidence=0.95 if not timeline_uncertain else 0.85,
            evidence_explanation=(
                f"Timeline employment xác thực tối thiểu {len(minimum_months)} tháng, "
                f"đáp ứng yêu cầu {required_months} tháng."
            ),
        )
    if len(maximum_months) < required_months and not timeline_uncertain:
        return _result(
            requirement,
            status="not_met",
            reason_code="experience_duration_below_minimum",
            evidence=[item for item in resume.evidence if item.evidence_id in evidence_refs],
            confidence=0.95,
            evidence_explanation=(
                f"Timeline employment xác thực tối đa {len(maximum_months)} tháng, "
                f"thấp hơn yêu cầu {required_months} tháng."
            ),
        )
    return _result(
        requirement,
        status="unknown",
        reason_code="experience_duration_ambiguous",
        evidence=[item for item in resume.evidence if item.evidence_id in evidence_refs],
        confidence=0.45,
        evidence_explanation=(
            "CV có timeline employment nhưng độ chính xác ngày/tháng chưa đủ để xác nhận "
            f"mốc {required_months} tháng."
        ),
    )


def select_evaluator(requirement: UnresolvedRequirement) -> EvaluatorSelection | None:
    text = _normalize(requirement.raw_label)
    if requirement.threshold is not None and requirement.scale is not None:
        return EvaluatorSelection("gpa", "education")
    if _GPA_RE.search(_fold_scale(requirement.raw_label)):
        return EvaluatorSelection("gpa", "education")
    # Degree thresholds are identifiable from the requirement text itself.  Do
    # not depend on the JD parser's ``kind`` classification: older/LLM parser
    # versions may label the same requirement as ``other`` or ``experience``.
    if _required_education_level(text) is not None:
        return EvaluatorSelection("education_degree", "education")
    if requirement.credential or _CREDENTIAL_RE.search(text):
        return EvaluatorSelection("language_credential", "language")
    if _has_phrase(text, _CERTIFICATION_TERMS):
        return EvaluatorSelection("certification", "other")
    if _required_experience_months(requirement) is not None:
        return EvaluatorSelection("experience_duration", "experience")
    decomposed, _ = _decompose_explicit_list(requirement.raw_label)
    if requirement.atomic_concepts or decomposed:
        return EvaluatorSelection("knowledge_skill", "skill")
    if _has_phrase(
        text,
        (
            "programming foundation",
            "nen tang lap trinh",
            "kien thuc nen tang ve lap trinh",
            "kien thuc nen tang lap trinh",
        ),
    ):
        return EvaluatorSelection("programming_foundation", "skill")
    if _has_phrase(text, _STUDENT_STATUS_PHRASES):
        return EvaluatorSelection("education_status", "education")
    if _has_phrase(text, ("research", "nghien cuu", "competition", "cuoc thi", "giai thuong")):
        return EvaluatorSelection("research_or_competition", "skill")
    if _has_phrase(text, ("dam me", "passion")) and _has_phrase(text, ("ai", "artificial intelligence")):
        return EvaluatorSelection("ai_activity", "skill")
    # Every non-empty rawLabel is evaluable.  A generic evaluator is the
    # conservative final fallback; valid requirements must never become
    # ``requirement_evaluator_unsupported`` merely because the JD parser did
    # not produce atomic concepts.
    return EvaluatorSelection("generic_requirement", "other")


def _result(
    requirement: UnresolvedRequirement,
    *,
    status: Literal["met", "not_met", "unknown"],
    reason_code: str,
    evidence: Iterable[EvidenceSpan] = (),
    confidence: float,
    concept_results: list[ConceptResult] | None = None,
    retrieval_candidates: Iterable[EvidenceCandidate] = (),
    group_operator: Literal["atomic", "all_of", "any_of"] = "atomic",
    evidence_explanation: str | None = None,
) -> RequirementResult:
    concept_results = concept_results or []
    evidence_refs = (
        list(dict.fromkeys(ref for concept in concept_results for ref in concept.evidence_refs))
        if concept_results
        else _unique_refs(evidence)
    )
    # Unknown is reserved for cited evidence whose meaning or strength is
    # insufficient. If the CV has no evidence reference, the requirement is
    # not met according to the submitted CV.
    if status == "unknown" and not evidence_refs:
        status = "not_met"
        if reason_code not in NOT_EVIDENCED_REASON_CODES:
            reason_code = "requirement_not_found"
        evidence_explanation = (
            f"Không tìm thấy bằng chứng đáp ứng “{requirement.raw_label}” trong CV hiện tại; "
            "tiêu chí được tính là chưa đáp ứng."
        )
    elif status == "met" and not evidence_refs:
        status = "not_met"
        reason_code = "requirement_not_found"
        evidence_explanation = f"Không tìm thấy “{requirement.raw_label}” trong CV hiện tại."
    elif status == "not_met" and not evidence_refs:
        evidence_explanation = (
            f"Không tìm thấy bằng chứng đáp ứng “{requirement.raw_label}” trong CV hiện tại; "
            "tiêu chí được tính là chưa đáp ứng."
        )
    elif status == "unknown" and evidence_explanation is None:
        evidence_explanation = (
            "CV có nhắc đến nội dung liên quan nhưng chưa đủ rõ để xác nhận yêu cầu."
            if evidence_refs
            else f"CV hiện tại chưa nhắc tới “{requirement.raw_label}” — cần ứng viên xác nhận thêm."
        )
    return RequirementResult(
        requirementId=requirement.requirement_id,
        status=status,
        score=1.0 if status == "met" else 0.0 if status == "not_met" else None,
        confidence=confidence,
        evidenceRefs=evidence_refs,
        reasonCode=reason_code,
        evidenceExplanation=evidence_explanation,
        conceptResults=concept_results,
        retrievalCandidates=[
            candidate.trace(rank=index, evidence_strength=_evidence_strength(candidate.evidence))
            for index, candidate in enumerate(retrieval_candidates, start=1)
        ],
        groupOperator=group_operator,
    )


_GENERIC_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "good", "have",
    "hands", "hand", "in", "into", "knowledge", "of", "on", "or", "strong", "the", "to", "via", "with",
    "experience", "understanding", "ability", "skill", "skills", "using", "working",
    "years", "year", "minimum", "plus", "preferred", "must", "should", "is", "are",
    "frontend", "fullstack", "development", "developer", "building", "build",
    "application", "applications", "system", "systems",
    "va", "và", "ve", "về", "có", "kinh", "nghiem", "nghiệm", "voi", "với", "su", "sự",
}


def _generic_terms(raw_label: str) -> list[str]:
    terms = _normalize(raw_label).split()
    return [term for term in terms if term not in _GENERIC_STOPWORDS and len(term) > 1]


def _raw_coverage(resume: CanonicalResume) -> str:
    if resume.parsing is None:
        # Legacy canonical records do not carry the source-text coverage
        # contract.  Indexed evidence alone cannot prove that an absent term
        # was absent from the original CV.
        return "unavailable"
    return resume.parsing.raw_text_coverage


def _absence_is_conclusive(resume: CanonicalResume) -> bool:
    """Require complete source text and evidence indexing before concluding absence."""
    return bool(
        resume.parsing
        and resume.parsing.raw_text_coverage == "complete"
        and resume.parsing.evidence_index_coverage == "complete"
    )


def _absence_result(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
    *,
    reason_prefix: str = "requirement",
) -> RequirementResult:
    return _result(
        requirement,
        status="unknown",
        reason_code=f"{reason_prefix}_not_evidenced",
        confidence=0.6,
        evidence_explanation=(
            f"CV hiện tại chưa nhắc tới “{requirement.raw_label}” — cần ứng viên xác nhận thêm."
        ),
    )


def _answers_addressed_to(
    requirement: UnresolvedRequirement, resume: CanonicalResume
) -> list[EvidenceSpan]:
    """The candidate's own replies to the question asked about this requirement.

    A clarification answer is not an anonymous CV line.  It was written in
    reply to a question about this requirement and carries that requirement's
    id, so its relevance is established by construction.  Ranking it by word
    overlap with the posting's wording made it prove that relevance a second
    time, and a Vietnamese answer to an English requirement shares no words at
    all with it - so the answer was never cited, the rescore step then dropped
    it as unused, and the system asked a question and ignored the reply.
    """

    return [
        item
        for item in resume.evidence
        if item.evidence_source == "candidate_self_report"
        and item.source_requirement_id == requirement.requirement_id
    ]


def _is_candidate_answer(evidence: EvidenceSpan) -> bool:
    """A span the candidate typed, which never enters through the lexical path.

    Admitting an answer because it shares words with the posting would reward
    pasting the posting back: an English reply echoing the requirement's own
    wording scored, while the same claim in Vietnamese did not.  Answers are
    admitted by the requirement id they carry and the work they describe, so
    they are taken out here and added back deliberately.
    """

    return evidence.evidence_source == "candidate_self_report"




def _evaluate_generic_requirement(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    """Score the requirement, then record any answer that did not earn its way in.

    An answer only improves a verdict when it says what the candidate actually
    did.  Admitting every answer as evidence moved the requirement from
    ``not_met`` to ``unknown``, and because ``unknown`` rows are left out of
    the score, typing anything at all - "tôi không biết", or a line of
    gibberish - raised it by the same amount as a real answer.  A bare claim is
    still cited, because the reader must be able to see what was read, but it
    leaves the verdict exactly where the CV left it.
    """

    addressed_answers = _answers_addressed_to(requirement, resume)
    substantive_ids = {
        item.evidence_id
        for item in addressed_answers
        if _evidence_strength(item) in {"applied", "demonstrated"}
    }
    result = _generic_requirement_result(requirement, resume, substantive_ids)
    cited = set(result.evidence_refs)
    unsubstantiated = [
        item.evidence_id for item in addressed_answers if item.evidence_id not in cited
    ]
    if not unsubstantiated:
        return result
    return result.model_copy(
        update={
            "evidence_refs": [*result.evidence_refs, *unsubstantiated],
            "evidence_explanation": (
                "Hệ thống đã đọc câu trả lời của bạn, nhưng câu trả lời chưa nêu công việc "
                "cụ thể đã làm, nên tiêu chí giữ nguyên kết luận rút ra từ CV."
            ),
        }
    )


def _generic_requirement_result(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
    substantive_answer_ids: set[str],
) -> RequirementResult:
    terms = _generic_terms(requirement.raw_label)
    if not terms:
        return _result(
            requirement,
            status="unknown",
            reason_code="requirement_evaluator_unsupported",
            confidence=0.0,
            evidence_explanation="rawLabel không chứa nội dung có thể phân tích; cần JD hợp lệ.",
        )
    weak_exact_terms = {"high", "low", "good", "solid", "strong"}
    ranked_candidates = [
        (
            sum(
                1
                for term in terms
                if term not in weak_exact_terms and _has_phrase(evidence.text, (term,))
            ),
            evidence,
        )
        for evidence in resume.evidence
        if any(_has_phrase(evidence.text, (term,)) for term in terms)
        and not _is_candidate_answer(evidence)
    ]
    # A concrete technology is independently useful evidence (a CV may place
    # C# and .NET in separate spans). Capability phrases need two substantive
    # lexical hits so a generic adjective such as ``high`` cannot suppress the
    # semantic retrieval pass.
    # Vietnamese is written one syllable per token, so splitting a label on
    # whitespace yields syllables, not words, and stripping diacritics makes
    # them collide (phuc covers both "phuc vu" and "phuc tap"; nhan covers
    # "nhan", "nhan" and "nhan"). A flat floor of two hits therefore got
    # WEAKER as a requirement got more specific: a thirteen-syllable sentence
    # needed the same two hits as a two-syllable phrase.
    #
    # Measured on the frozen corpus that rule credited a bachelor's-degree line
    # as evidence for "van hanh lo phan ung hat nhan", and 83% of the verdicts
    # it produced failed a majority test. Requiring a strict majority of the
    # requirement's content syllables to appear in one span scales the bar with
    # the size of the claim. What it rejects becomes "unknown", which raises a
    # clarification question - it does not reject the candidate.
    minimum_overlap = (
        1 if is_named_technology(requirement.raw_label) else max(2, len(terms) // 2 + 1)
    )

    substantive_answers = [
        item for item in resume.evidence if item.evidence_id in substantive_answer_ids
    ]
    if substantive_answers:
        ranked_candidates = [
            *(
                (overlap, evidence)
                for overlap, evidence in ranked_candidates
                if evidence.evidence_id not in substantive_answer_ids
            ),
            # The id the answer carries is what makes it relevant, so it enters
            # at the floor rather than being measured against it instead of
            # being thrown away for not repeating the posting's wording. Only
            # answers that describe real work get this far; the caller records
            # the rest without letting them move the verdict.
            *((minimum_overlap, item) for item in substantive_answers),
        ]

    exact_candidates = sorted(
        _dedupe_evidence(evidence for _, evidence in ranked_candidates),
        key=lambda item: (-_STRENGTH_RANK[_evidence_strength(item)], item.char_start, item.evidence_id),
    )[:4]
    exact_overlap = {
        evidence.evidence_id: overlap for overlap, evidence in ranked_candidates
    }
    exact_candidates = [
        evidence
        for evidence in exact_candidates
        if exact_overlap.get(evidence.evidence_id, 0) >= minimum_overlap
    ]
    retrieval_candidates = [
        EvidenceCandidate(
            evidence=evidence,
            retrieval_method="exact_phrase",
            semantic_score=1.0,
            lexical_score=1.0,
        )
        for evidence in exact_candidates
    ]
    semantic_candidates = retrieve_semantic_evidence(requirement.raw_label, resume)
    if not exact_candidates and semantic_candidates:
        candidates = [candidate.evidence for candidate in semantic_candidates]
        retrieval_candidates = semantic_candidates
    else:
        candidates = exact_candidates
    # A single meaningful lexical mention is weak evidence: retain its source
    # passage for clarification, but do not let it satisfy a broad criterion.
    if not candidates:
        weak_candidates = sorted(
            _dedupe_evidence(evidence for overlap, evidence in ranked_candidates if overlap > 0),
            key=lambda item: (-_STRENGTH_RANK[_evidence_strength(item)], item.char_start, item.evidence_id),
        )[:4]
        if weak_candidates:
            candidates = weak_candidates
            retrieval_candidates = [
                EvidenceCandidate(
                    evidence=evidence,
                    retrieval_method="partial_exact_phrase",
                    semantic_score=0.0,
                    lexical_score=exact_overlap.get(evidence.evidence_id, 0) / max(len(terms), 1),
                )
                for evidence in weak_candidates
            ]
    if not candidates:
        return _absence_result(requirement, resume, reason_prefix="requirement")
    strongest = _evidence_strength(candidates[0])
    strongest_overlap = max(
        (
            overlap
            for overlap, evidence in ranked_candidates
            if evidence.evidence_id in {item.evidence_id for item in candidates[:4]}
        ),
        default=0,
    )
    semantic_match = bool(semantic_candidates and not exact_candidates)
    semantic_score = retrieval_candidates[0].semantic_score if retrieval_candidates else 0.0
    if (
        semantic_match
        and strongest in {"applied", "demonstrated"}
        and semantic_score >= _SEMANTIC_MET_THRESHOLD
    ):
        return _result(
            requirement,
            status="met",
            reason_code="semantic_requirement_evidenced",
            evidence=candidates,
            retrieval_candidates=retrieval_candidates,
            confidence=min(0.88, 0.55 + semantic_score * 0.25),
            evidence_explanation=(
                "CV có evidence semantic tương đồng, kèm ngữ cảnh thực hành và kết quả đủ rõ "
                "để xác nhận yêu cầu."
            ),
        )
    if semantic_match:
        return _result(
            requirement,
            status="unknown",
            reason_code="semantic_requirement_evidence_weak",
            evidence=candidates,
            retrieval_candidates=retrieval_candidates,
            confidence=min(0.55, 0.25 + semantic_score * 0.3),
            evidence_explanation=(
                "CV có evidence semantic liên quan nhưng chưa đủ ngữ cảnh hành động, "
                "độ mạnh hoặc kết quả để xác nhận yêu cầu."
            ),
        )
    if strongest in {"applied", "demonstrated"} and strongest_overlap >= minimum_overlap:
        return _result(
            requirement,
            status="met",
            reason_code="generic_requirement_evidenced",
            evidence=candidates,
            retrieval_candidates=retrieval_candidates,
            confidence=min(0.9, 0.55 + 0.1 * _STRENGTH_RANK[strongest]),
            evidence_explanation="CV có bằng chứng theo ngữ cảnh thực hành liên quan đến yêu cầu JD.",
        )
    return _result(
        requirement,
        status="unknown",
        reason_code="generic_requirement_evidence_weak",
        evidence=candidates,
        retrieval_candidates=retrieval_candidates,
        confidence=0.35,
        evidence_explanation=(
            "CV có nhắc đến nội dung liên quan nhưng chưa thể hiện đủ ngữ cảnh thực hành, "
            "mức độ hoặc thời lượng để xác nhận yêu cầu."
        ),
    )


def _evaluate_gpa(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    requirement_match = _GPA_RE.search(_fold_scale(requirement.raw_label))
    threshold = requirement.threshold
    scale = requirement.scale
    if threshold is None and requirement_match:
        threshold = float(requirement_match.group("value"))
    if scale is None and requirement_match and requirement_match.group("scale"):
        scale = float(requirement_match.group("scale"))
    if scale is None and threshold is not None:
        scale = 4.0 if threshold <= 4.0 else 10.0
    if threshold is None or scale is None:
        return _result(
            requirement,
            status="unknown",
            reason_code="requirement_evaluator_unsupported",
            confidence=0.0,
        )

    evidence_by_id = {item.evidence_id: item for item in resume.evidence}
    for education in resume.education:
        if education.gpa is None or education.gpa_scale is None:
            continue
        meets = education.gpa / education.gpa_scale >= threshold / scale
        linked_evidence = [
            evidence_by_id[ref] for ref in education.evidence_refs if ref in evidence_by_id
        ]
        gpa_evidence = [
            item for item in linked_evidence if _GPA_RE.search(_fold_scale(item.text))
        ]
        return _result(
            requirement,
            status="met" if meets else "not_met",
            reason_code="gpa_threshold_met" if meets else "gpa_below_threshold",
            evidence=gpa_evidence or linked_evidence,
            confidence=1.0,
        )

    for evidence in resume.evidence:
        match = _GPA_RE.search(_fold_scale(evidence.text))
        if not match:
            continue
        candidate = float(match.group("value"))
        candidate_scale = float(match.group("scale") or (4.0 if candidate <= 4.0 else 10.0))
        meets = candidate / candidate_scale >= threshold / scale
        return _result(
            requirement,
            status="met" if meets else "not_met",
            reason_code="gpa_threshold_met" if meets else "gpa_below_threshold",
            evidence=[evidence],
            confidence=1.0,
        )
    return _result(
        requirement,
        status="not_met" if _absence_is_conclusive(resume) else "unknown",
        reason_code=("gpa_not_evidenced" if _absence_is_conclusive(resume) else "gpa_evidence_missing"),
        confidence=0.9 if _absence_is_conclusive(resume) else 0.0,
    )


def _evaluate_language(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    requirement_match = _CREDENTIAL_RE.search(_normalize(requirement.raw_label))
    credential = _normalize(requirement.credential or "")
    if not credential and requirement_match:
        credential = requirement_match.group("credential")
    threshold = requirement.threshold
    if threshold is None and requirement_match and requirement_match.group("value"):
        threshold = float(requirement_match.group("value"))
    if not credential:
        return _result(
            requirement,
            status="unknown",
            reason_code="requirement_evaluator_unsupported",
            confidence=0.0,
        )

    # The level the posting is asking for, once, so a CV carrying a different
    # test can be placed against it.  ``None`` whenever the requirement names
    # no number, or names a test CEFR does not describe (JLPT, HSK).
    required_level = (
        cefr_level_for_score(credential, threshold) if threshold is not None else None
    )
    best_equivalent: tuple[int, EvidenceSpan, str] | None = None

    for evidence in resume.evidence:
        normalized = _normalize(evidence.text)
        match = _CREDENTIAL_RE.search(normalized)
        if match and not scale_is_unambiguous(match.group("credential"), normalized):
            # The result is real but sits on a scale these bands do not
            # describe; placing it anyway would be worse than not placing it.
            continue
        if match and match.group("credential") == credential and match.group("value"):
            candidate = float(match.group("value"))
            # A number off the test's own scale is not a result - "TOEIC 2026"
            # is a year, and reading it as one passed every threshold.
            if score_is_on_scale(credential, candidate):
                meets = threshold is None or candidate >= threshold
                return _result(
                    requirement,
                    status="met" if meets else "not_met",
                    reason_code=(
                        "language_credential_evidenced"
                        if meets
                        else "language_credential_below_minimum"
                    ),
                    evidence=[evidence],
                    confidence=1.0,
                )
        if required_level is None:
            continue
        candidate_level: str | None = None
        if match and match.group("value") and is_english_credential(match.group("credential")):
            candidate_level = cefr_level_for_score(
                match.group("credential"), float(match.group("value"))
            )
        if candidate_level is None:
            candidate_level = cefr_level_in_text(normalized)
        if candidate_level is None:
            continue
        rank = cefr_rank(candidate_level)
        if best_equivalent is None or rank > best_equivalent[0]:
            best_equivalent = (rank, evidence, candidate_level)

    if best_equivalent is not None and required_level is not None:
        _, evidence, candidate_level = best_equivalent
        if not cefr_meets(candidate_level, required_level):
            # Short of the level even under the most generous reading, so the
            # posting's wording cannot rescue it.
            return _result(
                requirement,
                status="not_met",
                reason_code="language_credential_below_minimum",
                evidence=[evidence],
                confidence=0.9,
            )
        if requirement.equivalent_allowed:
            return _result(
                requirement,
                status="met",
                reason_code="language_credential_equivalent",
                evidence=[evidence],
                # One CEFR band wide, not one point: the posting accepted that
                # when it wrote "hoặc tương đương", but it is a coarser reading
                # than a same-test comparison and is not claimed as exact.
                confidence=0.85,
            )
        # The posting named one test and did not say another would do.  The
        # candidate clears the level on a different test, which is a question
        # for the employer, not grounds for rejecting the candidate outright.
        return _result(
            requirement,
            status="unknown",
            reason_code="language_credential_equivalence_unconfirmed",
            evidence=[evidence],
            confidence=0.5,
        )

    return _result(
        requirement,
        status="not_met" if _absence_is_conclusive(resume) else "unknown",
        reason_code=(
            "language_credential_not_evidenced"
            if _absence_is_conclusive(resume)
            else "language_credential_evidence_missing"
        ),
        confidence=0.9 if _absence_is_conclusive(resume) else 0.0,
    )


def _evaluate_certification(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    terms = {
        term
        for term in _normalize(requirement.raw_label).split()
        if len(term) > 1 and term not in _CERTIFICATION_STOPWORDS
    }
    evidence_by_id = {item.evidence_id: item for item in resume.evidence}
    candidates: list[tuple[int, list[EvidenceSpan]]] = []
    for certification in resume.certifications:
        label = _normalize(f"{certification.name} {certification.issuer or ''}")
        matched_terms = terms.intersection(label.split())
        linked_evidence = [
            evidence_by_id[ref]
            for ref in certification.evidence_refs
            if ref in evidence_by_id
        ]
        if linked_evidence:
            candidates.append((len(matched_terms), linked_evidence))

    if not candidates:
        candidates = [
            (
                len(terms.intersection(_normalize(item.text).split())),
                [item],
            )
            for item in resume.evidence
            if _has_phrase(item.section, ("certification", "certificate", "credentials", "chung chi"))
        ]

    if not terms:
        evidence = [item for _, refs in candidates for item in refs]
        if evidence:
            return _result(
                requirement,
                status="met",
                reason_code="certification_evidenced",
                evidence=_dedupe_evidence(evidence)[:4],
                confidence=0.95,
            )
    else:
        minimum_hits = max(1, (len(terms) + 1) // 2)
        best_hits = max((hits for hits, _ in candidates), default=0)
        if best_hits >= minimum_hits:
            evidence = [item for hits, refs in candidates if hits == best_hits for item in refs]
            return _result(
                requirement,
                status="met",
                reason_code="certification_evidenced",
                evidence=_dedupe_evidence(evidence)[:4],
                confidence=0.9,
            )
        if best_hits:
            evidence = [item for hits, refs in candidates if hits == best_hits for item in refs]
            return _result(
                requirement,
                status="unknown",
                reason_code="certification_needs_confirmation",
                evidence=_dedupe_evidence(evidence)[:4],
                confidence=0.55,
                evidence_explanation=(
                    "CV có chứng chỉ liên quan nhưng tên chưa khớp đủ rõ với chứng chỉ được yêu cầu."
                ),
            )

    return _result(
        requirement,
        status="not_met",
        reason_code="certification_not_evidenced",
        confidence=0.9 if _absence_is_conclusive(resume) else 0.6,
    )


def _evaluate_education_status(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    """Score a requirement phrased as a stage of study.

    "Sinh viên năm cuối" names a floor, not an exact point: the posting is
    asking for someone far enough through a degree to be useful, and someone
    who finished that degree is past that floor, not short of it.  Reading it
    as an exact point rejected a candidate for holding the qualification the
    line asks them to be close to obtaining - the same shape of error as
    reading "Đại học trở lên" to exclude a master's.

    What the line really narrows, therefore, is the *field*, and that is where
    this reads the CV.
    """
    status_phrases = _STUDENT_STATUS_PHRASES
    field_phrases = _IT_FIELD_PHRASES
    requirement_names_field = _has_phrase(requirement.raw_label, field_phrases)
    evidence_by_id = {item.evidence_id: item for item in resume.evidence}

    on_field: list[tuple[object, bool, list[EvidenceSpan]]] = []
    off_field: list[tuple[object, list[EvidenceSpan]]] = []
    for education in resume.education:
        if not _is_degree_programme(education):
            continue
        refs = [evidence_by_id[ref] for ref in education.evidence_refs if ref in evidence_by_id]
        # The parser does not always split a field out of the line, so the
        # entry's own source text is read alongside ``fieldOfStudy``.
        described = " ".join([education.field_of_study or "", *(item.text for item in refs)])
        if not requirement_names_field or _has_phrase(described, field_phrases):
            # An education entry can link several spans - the degree line and
            # the GPA line beside it.  Cite the one that carries the status and
            # the field, so the reader sees what the verdict was read from.
            on_topic = [item for item in refs if _has_phrase(item.text, field_phrases)]
            with_status = [item for item in on_topic if _has_phrase(item.text, status_phrases)]
            on_field.append(
                (
                    education,
                    _education_is_currently_in_progress(education),
                    with_status or on_topic or refs,
                )
            )
        else:
            off_field.append((education, refs))

    if on_field:
        studying = any(in_progress for _, in_progress, _ in on_field)
        evidence = [item for _, _, refs in on_field for item in refs]
        finished = max(
            (end for entry, _, _ in on_field if (end := _education_end_date(entry)) is not None),
            default=None,
        )
        return _result(
            requirement,
            status="met",
            reason_code=(
                "education_status_and_field_evidenced" if studying else "education_status_exceeded"
            ),
            evidence=evidence,
            confidence=1.0 if studying else 0.9,
            evidence_explanation=(
                None
                if studying
                else (
                    "CV cho thấy đã hoàn tất chương trình đại học đúng chuyên ngành"
                    + (f" vào {finished.month:02d}/{finished.year}" if finished else "")
                    + ", tức đã qua giai đoạn mà vị trí yêu cầu."
                )
            ),
        )

    free_text_evidence = [
        item
        for item in resume.evidence
        if (_has_phrase(item.text, status_phrases) and _has_phrase(item.text, field_phrases))
        or _evidence_indicates_current_education(item.text, field_phrases)
    ]
    if free_text_evidence:
        return _result(
            requirement,
            status="met",
            reason_code="education_status_and_field_evidenced",
            evidence=free_text_evidence,
            confidence=1.0,
        )

    if off_field:
        # A degree is on the CV, in a field this requirement did not name.  The
        # vocabulary of named fields is narrower than the majors that actually
        # qualify, so this asks rather than rejects: ``unknown`` keeps the pair
        # out of the eligibility gate and raises the question instead.
        evidence = [item for _, refs in off_field for item in refs]
        named = ", ".join(
            dict.fromkeys(
                str(entry.field_of_study or entry.degree or entry.institution)
                for entry, _ in off_field
            )
        )
        return _result(
            requirement,
            status="unknown",
            reason_code="education_field_not_confirmed",
            evidence=evidence,
            confidence=0.5,
            evidence_explanation=(
                f"CV có bằng đại học, nhưng ngành ghi trong CV là “{named}” — "
                "chưa xác nhận được đúng chuyên ngành vị trí yêu cầu."
            ),
        )

    return _result(
        requirement,
        status="not_met" if _absence_is_conclusive(resume) else "unknown",
        reason_code=(
            "education_status_not_evidenced"
            if _absence_is_conclusive(resume)
            else "education_evidence_missing"
        ),
        confidence=0.9 if _absence_is_conclusive(resume) else 0.0,
    )


def _education_end_date(education: object) -> date | None:
    """The month a programme finished, from its grounded end date."""
    end = getattr(education, "end_date", None)
    value = getattr(end, "value", None)
    if not value:
        return None
    parts = value.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 12
    except (TypeError, ValueError):
        return None
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


_DEGREE_PROGRAMME_MARKERS = (
    "university",
    "college",
    "dai hoc",
    "cao dang",
    "hoc vien",
    "bachelor",
    "master",
    "cu nhan",
    "ky su",
    "engineer",
)


def _is_degree_programme(education: object) -> bool:
    """Whether an education entry is the kind a student-status line asks about.

    A posting asking for "sinh viên năm cuối ... tại các trường đại học" is
    asking about a degree.  A CV also lists short courses and bootcamps, and
    one of those with no end date must not be read as a degree that might
    still be running - that is what kept a candidate who graduated in 2017
    from being recognised as a graduate at all.
    """
    haystack = " ".join(
        str(getattr(education, field, "") or "")
        for field in ("institution", "degree", "field_of_study")
    )
    return _has_phrase(haystack, _DEGREE_PROGRAMME_MARKERS)


def _education_is_currently_in_progress(education: object) -> bool:
    """Infer current/final-year study from a grounded date range."""

    start = getattr(education, "start_date", None)
    end = getattr(education, "end_date", None)
    if start is None or end is None:
        return False
    try:
        start_parts = [int(part) for part in start.value.split("-")]
        end_parts = [int(part) for part in end.value.split("-")]
        start_year, end_year = start_parts[0], end_parts[0]
        if end_year - start_year < 2:
            return False
        today = datetime.now(UTC).date()
        start_date = date(start_year, start_parts[1] if len(start_parts) > 1 else 1, 1)
        if len(end_parts) > 1:
            end_month = end_parts[1]
            if end_month == 12:
                end_date = date(end_year, 12, 31)
            else:
                end_date = date(end_year, end_month + 1, 1) - date.resolution
        else:
            end_date = date(end_year, 12, 31)
        return start_date <= today <= end_date
    except (AttributeError, TypeError, ValueError):
        return False


def _evidence_indicates_current_education(text: str, field_phrases: tuple[str, ...]) -> bool:
    """Recover an active study range from a verbatim source passage."""
    normalized = _normalize(text)
    education_markers = ("university", "college", "bachelor", "degree", "student", "dai hoc", "cao dang")
    if not _has_phrase(normalized, education_markers) or not _has_phrase(normalized, field_phrases):
        return False
    years = [int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", normalized)]
    if len(years) < 2:
        return False
    start_year, end_year = years[0], years[-1]
    current_year = datetime.now(UTC).year
    return end_year - start_year >= 2 and start_year <= current_year <= end_year


def _evaluate_education_degree(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    required_level = _required_education_level(requirement.raw_label)
    if required_level is None:
        return _evaluate_generic_requirement(requirement, resume)

    evidence_by_id = {item.evidence_id: item for item in resume.evidence}
    qualifying: list[EvidenceSpan] = []
    below: list[EvidenceSpan] = []
    for education in resume.education:
        refs = [evidence_by_id[ref] for ref in education.evidence_refs if ref in evidence_by_id]
        entry_text = " ".join(item.text for item in refs)
        level = _education_level(education.degree, entry_text)
        if level is None:
            continue
        (qualifying if level >= required_level else below).extend(refs)

    # A parser may have populated raw evidence while the structured education
    # entry is incomplete.  Use the same degree hierarchy against those spans.
    if not qualifying and not below:
        for evidence in resume.evidence:
            level = _education_level(None, evidence.text)
            if level is None:
                continue
            (qualifying if level >= required_level else below).append(evidence)

    if qualifying:
        return _result(
            requirement,
            status="met",
            reason_code="education_degree_evidenced",
            evidence=_dedupe_evidence(qualifying)[:6],
            confidence=1.0,
            evidence_explanation=(
                "Bằng cấp trong CV đạt hoặc cao hơn mức học vấn tối thiểu được yêu cầu."
            ),
        )
    if below:
        return _result(
            requirement,
            status="not_met",
            reason_code="education_degree_below_minimum",
            evidence=_dedupe_evidence(below)[:6],
            confidence=0.95,
            evidence_explanation="CV có bằng cấp nhưng thấp hơn mức học vấn tối thiểu.",
        )
    coverage = resume.parsing.raw_text_coverage if resume.parsing else "legacy"
    absence_is_conclusive = _absence_is_conclusive(resume)
    return _result(
        requirement,
        status="not_met" if absence_is_conclusive else "unknown",
        reason_code=(
            "education_degree_not_evidenced"
            if absence_is_conclusive
            else "education_degree_evidence_incomplete"
        ),
        confidence=0.9 if absence_is_conclusive else 0.0,
        evidence_explanation=(
            "CV đã được đọc và đánh chỉ mục đầy đủ nhưng không có bằng chứng bằng cấp đáp ứng yêu cầu."
            if absence_is_conclusive
            else f"Không thể kết luận bằng cấp vắng mặt vì raw_text_coverage={coverage}."
        ),
    )


def _evaluate_knowledge_skill(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    requirement_text = _normalize(requirement.raw_label)
    fallback_concepts, fallback_operator = _decompose_explicit_list(requirement.raw_label)
    requested = requirement.atomic_concepts or fallback_concepts
    if not requested:
        return _evaluate_generic_requirement(requirement, resume)

    group_operator = _concept_group_operator(
        requirement, requirement_text, len(requested), fallback_operator
    )
    minimum_strength = _minimum_evidence_strength(requirement_text)
    evaluated_concepts = [
        _evaluate_atomic_concept(
            concept=concept,
            resume=resume,
            minimum_strength=minimum_strength,
        )
        for concept in requested
    ]
    concepts = [
        concept.model_copy(
            update={
                "status": "not_met",
                "confidence": 0.9,
                "reason_code": "concept_not_found",
            }
        )
        if concept.status == "unknown" and not concept.evidence_refs
        else concept
        for concept, _ in evaluated_concepts
    ]
    retrieval_candidates = [candidate for _, candidates in evaluated_concepts for candidate in candidates]
    status = _group_status(concepts, group_operator)
    confidence = _group_confidence(concepts, group_operator)
    nothing_cited = not any(concept.evidence_refs for concept in concepts)
    return _result(
        requirement,
        status=status,
        reason_code=(
            "concept_group_evidenced"
            if status == "met"
            else "concept_group_not_met"
            if status == "not_met"
            else "concept_group_not_evidenced"
            if nothing_cited
            else "concept_group_evidence_missing"
        ),
        confidence=confidence,
        concept_results=concepts,
        retrieval_candidates=retrieval_candidates,
        group_operator=group_operator,
    )


def _concept_group_operator(
    requirement: UnresolvedRequirement,
    text: str,
    concept_count: int,
    fallback_operator: str = "atomic",
) -> Literal["atomic", "all_of", "any_of"]:
    if concept_count <= 1:
        return "atomic"
    # The posting's own sentence outranks the extractor's label.  A line that
    # says "ít nhất một" and arrives stored as ``all_of`` is a must-have no
    # candidate can satisfy: the CV would have to evidence every language the
    # posting listed as an example.  Only ``all_of`` is overridden - reading a
    # conjunction as alternatives would waive a real condition, which is the
    # mirror of the mistake being fixed.
    if requirement_list_is_disjunctive(requirement.raw_label):
        return "any_of"
    if requirement.group_operator in {"all_of", "any_of"}:
        return requirement.group_operator
    if fallback_operator in {"all_of", "any_of"}:
        return fallback_operator
    return "any_of" if re.search(r"\b(?:or|hoac|hay)\b", text) else "all_of"


def _minimum_evidence_strength(text: str) -> str:
    # Generic wording policy: named/basic knowledge can be claimed, whereas
    # production/deployment/implementation experience needs applied evidence.
    if re.search(
        r"\b(?:production|deploy(?:ment|ed)?|implement(?:ed|ation)?|"
        r"experience|kinh nghiem|trien khai)\b",
        text,
    ):
        return "applied"
    return "claimed"


# A clarification answer is a sentence written in reply to a question, not a
# line lifted from a CV, so the wording that marks real work in it is wider -
# and Vietnamese, which the CV lexicon below barely covers. Kept as its own
# pattern so that widening it can never change how a CV passage is scored, and
# so the measurements in the paper's CV-side tables stay comparable.
_ANSWER_APPLIED_RE = re.compile(
    r"\b(?:optimi[sz]ed|debugged|refactored|fixed|resolved|maintained|automated|"
    r"monitored|profiled|handled|troubleshot|"
    r"toi uu|sua loi|go loi|khac phuc|cai thien|nang cap|van hanh|bao tri|"
    r"kiem thu|thiet ke|phu trach|chiu trach nhiem|truc tiep lam|da lam|tung lam)\b"
)


def _evidence_strength(evidence: EvidenceSpan) -> Literal["mention", "claimed", "applied", "demonstrated"]:
    text = _normalize(evidence.text)
    if evidence.evidence_source == "candidate_self_report" and _ANSWER_APPLIED_RE.search(text):
        return "applied"
    if re.search(
        r"\b(?:research(?:ed)?|scientific|benchmark(?:ed)?|evaluat(?:ed|ion)|"
        r"experiment|nghien cuu|danh gia|thu nghiem|\d[\d,.]*\s*"
        r"(?:requests?|records?|ms|seconds?|minutes?|million|percent|%)|p(?:95|99)\b)",
        text,
    ):
        return "demonstrated"
    if re.search(
        r"\b(?:built|designed|developed|implemented|integrated|deployed|operated|"
        r"reworked|introduced|replaced|reorganized|modelled|migrated|packaged|"
        r"pipeline|workflow|remediation|tuning|reduced|processed|led|"
        r"xay dung|phat trien|trien khai|tich hop)\b",
        text,
    ):
        return "applied"
    if evidence.section in {"skills", "profile"} or re.search(
        r"\b(?:skills?|tech|technologies|stack)\b", text
    ):
        return "claimed"
    return "mention"


def _evaluate_atomic_concept(
    *,
    concept: TaxonomyRef,
    resume: CanonicalResume,
    minimum_strength: str,
) -> tuple[ConceptResult, list[EvidenceCandidate]]:
    evidence_by_id = {item.evidence_id: item for item in resume.evidence}
    structured_refs = [
        ref
        for claim in resume.skills
        if claim.concept.concept_id == concept.concept_id
        for ref in claim.evidence_refs
    ]
    label_key = concept.label.casefold().strip()
    match_phrases = [concept.label, *_catalog_aliases(concept.concept_id)]
    if label_key in _CONCEPT_ALIASES:
        match_phrases.extend(_CONCEPT_ALIASES[label_key])
    elif concept.concept_id in _CONCEPT_ALIASES:
        match_phrases.extend(_CONCEPT_ALIASES[concept.concept_id])
    phrases = tuple(dict.fromkeys(match_phrases))
    candidates = _dedupe_evidence(
        [evidence_by_id[ref] for ref in structured_refs if ref in evidence_by_id]
        + _matching_evidence(resume, phrases)
        + _alias_pattern_evidence(resume, concept.concept_id, phrases)
    )
    ranked = sorted(
        candidates,
        key=lambda item: (-_STRENGTH_RANK[_evidence_strength(item)], item.char_start, item.evidence_id),
    )[:2]
    retrieval_candidates = [
        EvidenceCandidate(
            evidence=evidence,
            retrieval_method="exact_phrase",
            semantic_score=1.0,
            lexical_score=1.0,
            concept_id=concept.concept_id,
        )
        for evidence in ranked
    ]
    if not ranked and not is_named_technology(concept.label):
        semantic_candidates = retrieve_semantic_evidence(
            concept.label,
            resume,
            concept_id=concept.concept_id,
        )
        ranked = [candidate.evidence for candidate in semantic_candidates]
        retrieval_candidates = semantic_candidates
    if not ranked:
        # Without a CV passage, this concept is not met according to the
        # evidence available in the submitted CV.
        return (
            ConceptResult(
                conceptId=concept.concept_id,
                label=concept.label,
                status="not_met",
                confidence=0.6,
                reasonCode="concept_not_evidenced",
            ),
            [],
        )
    strength = _evidence_strength(ranked[0])
    strength_rank = _STRENGTH_RANK[strength]
    semantic_match = any(
        candidate.retrieval_method == "semantic_lexical_expansion"
        for candidate in retrieval_candidates
    )
    semantic_score = retrieval_candidates[0].semantic_score if retrieval_candidates else 0.0
    sufficient = strength_rank >= _STRENGTH_RANK[minimum_strength]
    semantic_sufficient = semantic_match and semantic_score >= _SEMANTIC_MET_THRESHOLD
    return (
        ConceptResult(
            conceptId=concept.concept_id,
            label=concept.label,
            status="met" if (sufficient and (not semantic_match or semantic_sufficient)) else "unknown",
            confidence=min(0.95, 0.25 + 0.16 * strength_rank + 0.04 * (len(ranked) - 1)),
            evidenceRefs=_unique_refs(ranked),
            evidenceStrength=strength,
            reasonCode=(
                "concept_semantic_evidence_sufficient"
                if semantic_match and semantic_sufficient
                else "concept_semantic_evidence_weak"
                if semantic_match
                else "concept_evidence_sufficient"
                if sufficient
                else "concept_evidence_too_weak"
            ),
        ),
        retrieval_candidates,
    )


def _group_status(
    concepts: list[ConceptResult], group_operator: str
) -> Literal["met", "not_met", "unknown"]:
    statuses = [concept.status for concept in concepts]
    if group_operator == "any_of":
        if "met" in statuses:
            return "met"
        return "not_met" if statuses and all(status == "not_met" for status in statuses) else "unknown"
    if "not_met" in statuses:
        return "not_met"
    return "met" if statuses and all(status == "met" for status in statuses) else "unknown"


def _group_confidence(concepts: list[ConceptResult], group_operator: str) -> float:
    if not concepts:
        return 0.0
    if group_operator == "any_of":
        return max(concept.confidence for concept in concepts)
    return sum(concept.confidence for concept in concepts) / len(concepts)


def _evaluate_programming_foundation(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    explicit = _matching_evidence(
        resume,
        ("strong programming foundation", "programming foundation", "nen tang lap trinh"),
    )
    if explicit:
        return _result(
            requirement,
            status="met",
            reason_code="programming_foundation_evidenced",
            evidence=explicit[:2],
            confidence=1.0,
        )

    supported_languages: dict[str, EvidenceSpan] = {}
    for language, aliases in _PROGRAMMING_LANGUAGES.items():
        matches = _matching_evidence(resume, aliases)
        if matches:
            supported_languages[language] = matches[0]
    if len(supported_languages) >= 2:
        return _result(
            requirement,
            status="met",
            reason_code="programming_foundation_supported_by_technologies",
            evidence=list(supported_languages.values())[:4],
            confidence=0.9,
        )
    return _result(
        requirement,
        status=(
            "unknown"
            if supported_languages or not _absence_is_conclusive(resume)
            else "not_met"
        ),
        reason_code=(
            "programming_foundation_evidence_missing"
            if supported_languages or not _absence_is_conclusive(resume)
            else "programming_foundation_not_evidenced"
        ),
        evidence=supported_languages.values(),
        confidence=(
            0.4
            if supported_languages
            else 0.9
            if _absence_is_conclusive(resume)
            else 0.0
        ),
    )


def _evaluate_research_or_competition(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    evidence = _matching_evidence(
        resume,
        (
            "scientific research",
            "research project",
            "research experience",
            "research study",
            "nghien cuu khoa hoc",
            "de tai nghien cuu",
            "nlp research",
            "competition",
            "cuoc thi",
            "giai thuong",
            "research & ai projects",
        ),
    )
    if evidence:
        return _result(
            requirement,
            status="met",
            reason_code="research_or_competition_evidenced",
            evidence=evidence[:4],
            confidence=1.0,
        )
    return _result(
        requirement,
        status="not_met" if _absence_is_conclusive(resume) else "unknown",
        reason_code=(
            "research_or_competition_not_evidenced"
            if _absence_is_conclusive(resume)
            else "research_or_competition_evidence_missing"
        ),
        confidence=0.9 if _absence_is_conclusive(resume) else 0.0,
    )


def _evaluate_ai_activity(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    evidence = [
        item
        for item in resume.evidence
        if _has_phrase(item.text, ("ai", "genai", "llm"))
        and _has_phrase(item.text, ("project", "research", "workflow", "integration", "pipeline"))
    ]
    if evidence:
        return _result(
            requirement,
            status="met",
            reason_code="ai_research_or_application_evidenced",
            evidence=evidence[:4],
            confidence=0.9,
        )
    return _result(
        requirement,
        status="not_met" if _absence_is_conclusive(resume) else "unknown",
        reason_code=(
            "ai_activity_not_evidenced"
            if _absence_is_conclusive(resume)
            else "ai_activity_evidence_missing"
        ),
        confidence=0.9 if _absence_is_conclusive(resume) else 0.0,
    )


def evaluate_unresolved_requirement(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    selection = select_evaluator(requirement)
    evaluators = {
        "gpa": _evaluate_gpa,
        "language_credential": _evaluate_language,
        "certification": _evaluate_certification,
        "experience_duration": _evaluate_experience_duration,
        "education_degree": _evaluate_education_degree,
        "education_status": _evaluate_education_status,
        "knowledge_skill": _evaluate_knowledge_skill,
        "programming_foundation": _evaluate_programming_foundation,
        "research_or_competition": _evaluate_research_or_competition,
        "ai_activity": _evaluate_ai_activity,
        "generic_requirement": _evaluate_generic_requirement,
    }
    evaluator = evaluators.get(selection.name)
    if evaluator is None:
        result = _evaluate_generic_requirement(requirement, resume)
    else:
        result = evaluator(requirement, resume)
    duration_stats = (
        _experience_duration_stats(resume)
        if selection.name == "experience_duration"
        else None
    )
    decision_basis = {
        "raw_coverage": resume.parsing.raw_text_coverage if resume.parsing else "legacy",
        "section_coverage": resume.parsing.canonical_section_coverage if resume.parsing else {},
        "evidence_coverage": resume.parsing.evidence_index_coverage if resume.parsing else "legacy",
        "threshold_version": "semantic-expansion-v1",
        "required_strength": (
            "applied" if selection.name in {"knowledge_skill", "generic_requirement"} else None
        ),
        "operator": result.group_operator,
    }
    trace_event(
        "matching",
        "requirement_evaluated",
        requirement_id=requirement.requirement_id,
        requirement_kind=requirement.kind,
        evaluator=selection.name,
        evaluator_category=selection.category,
        result_status=result.status,
        reason_code=result.reason_code,
        score=result.score,
        confidence=result.confidence,
        evidence_refs=result.evidence_refs,
        resolution_source="structured_concept_refs"
        if result.concept_results
        else "canonical_evidence",
        matched_aliases=[],
        concept_results=[concept.model_dump(by_alias=True) for concept in result.concept_results],
        retrieval_candidates=[
            candidate.model_dump(by_alias=True) for candidate in result.retrieval_candidates
        ],
        decision_basis=decision_basis,
        required_months=(
            _required_experience_months(requirement)
            if selection.name == "experience_duration"
            else None
        ),
        total_verified_months=(
            len(duration_stats["minimum_months"]) if duration_stats else None
        ),
        counted_employment_refs=(
            duration_stats["evidence_refs"] if duration_stats else []
        ),
        merged_interval_count=(
            duration_stats["merged_interval_count"] if duration_stats else None
        ),
        evidence_coverage=(resume.parsing.evidence_index_coverage if resume.parsing else "legacy"),
        raw_text_coverage=(resume.parsing.raw_text_coverage if resume.parsing else "legacy"),
        canonical_section_coverage=(resume.parsing.canonical_section_coverage if resume.parsing else {}),
        retrieval_methods=list(
            dict.fromkeys(
                ["structured_concept_refs", "exact_alias_phrase", "generic_token_overlap"]
                + [candidate.retrieval_method for candidate in result.retrieval_candidates]
            )
        ),
    )
    return result
