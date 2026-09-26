from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from src.core.trace_logging import trace_event
from src.modules.matching.schemas import ConceptResult, RequirementResult, UnresolvedRequirement
from src.modules.user_cvs.schemas import CanonicalResume, EvidenceSpan, TaxonomyRef

RequirementCategory = Literal["skill", "language", "experience", "education", "other"]


@dataclass(frozen=True)
class EvaluatorSelection:
    name: str
    category: RequirementCategory


_GPA_RE = re.compile(
    r"\b(?:gpa|cpa)(?:\s*(?:>=|>|=|tu|from|:))?\s*(?P<value>\d+(?:\.\d+)?)"
    r"(?:\s*/\s*(?P<scale>\d+(?:\.\d+)?))?\b",
    re.I,
)
_CREDENTIAL_RE = re.compile(
    r"\b(?P<credential>ielts|toeic|toefl|jlpt|hsk)\s*"
    r"(?:>=|>|=|tu|from|:)?\s*(?P<value>\d+(?:\.\d+)?)?\s*\+?",
    re.I,
)

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


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    ascii_value = ascii_value.casefold().replace("đ", "d")
    return " ".join(re.findall(r"[a-z0-9+#.]+", ascii_value))


def _has_phrase(text: str, phrases: Iterable[str]) -> bool:
    normalized = _normalize(text).replace(".", " ")
    padded = f" {normalized} "
    return any(f" {_normalize(phrase).replace('.', ' ')} " in padded for phrase in phrases)


def _matching_evidence(
    resume: CanonicalResume,
    phrases: Iterable[str],
) -> list[EvidenceSpan]:
    return [item for item in resume.evidence if _has_phrase(item.text, phrases)]


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


def select_evaluator(requirement: UnresolvedRequirement) -> EvaluatorSelection | None:
    text = _normalize(requirement.raw_label)
    if requirement.threshold is not None and requirement.scale is not None:
        return EvaluatorSelection("gpa", "education")
    if _GPA_RE.search(text):
        return EvaluatorSelection("gpa", "education")
    if requirement.credential or _CREDENTIAL_RE.search(text):
        return EvaluatorSelection("language_credential", "language")
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
    if _has_phrase(
        text,
        ("sinh vien nam 4", "final year", "final-year", "moi tot nghiep", "recent graduate"),
    ):
        return EvaluatorSelection("education_status", "education")
    if _has_phrase(text, ("research", "nghien cuu", "competition", "cuoc thi", "giai thuong")):
        return EvaluatorSelection("research_or_competition", "skill")
    if _has_phrase(text, ("dam me", "passion")) and _has_phrase(text, ("ai", "artificial intelligence")):
        return EvaluatorSelection("ai_activity", "skill")
    if requirement.minimum_experience_months is not None:
        return EvaluatorSelection("generic_requirement", "experience")
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
    group_operator: Literal["atomic", "all_of", "any_of"] = "atomic",
    evidence_explanation: str | None = None,
) -> RequirementResult:
    concept_results = concept_results or []
    if status == "unknown" and evidence_explanation is None:
        evidence_explanation = (
            f"Chưa có bằng chứng đủ rõ để kết luận; nguyên nhân đánh giá: {reason_code}."
        )
    evidence_refs = (
        list(dict.fromkeys(ref for concept in concept_results for ref in concept.evidence_refs))
        if concept_results
        else _unique_refs(evidence)
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
        groupOperator=group_operator,
    )


_GENERIC_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "good", "have",
    "hands", "hand", "in", "knowledge", "of", "or", "strong", "the", "to", "with",
    "experience", "understanding", "ability", "skill", "skills", "using", "working",
    "years", "year", "minimum", "plus", "preferred", "must", "should", "is", "are",
    "backend", "frontend", "fullstack", "development", "developer", "building", "build",
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


def _absence_result(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
    *,
    reason_prefix: str = "requirement",
) -> RequirementResult:
    coverage = _raw_coverage(resume)
    if coverage == "complete":
        return _result(
            requirement,
            status="not_met",
            reason_code=f"{reason_prefix}_not_evidenced",
            confidence=0.9,
            evidence_explanation=(
                "Không tìm thấy bằng chứng phù hợp trong toàn bộ raw text của CV; "
                "tiêu chí được xác định là chưa đáp ứng."
            ),
        )
    return _result(
        requirement,
        status="unknown",
        reason_code="raw_text_coverage_incomplete",
        confidence=0.0,
        evidence_explanation=(
            f"Không thể kết luận vắng bằng chứng vì raw_text_coverage={coverage}; "
            "CV chưa được đọc đầy đủ. Đây là lý do coverage, không phải bằng chứng đạt."
        ),
    )


def _evaluate_generic_requirement(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
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
    ranked_candidates = [
        (
            sum(1 for term in terms if _has_phrase(evidence.text, (term,))),
            evidence,
        )
        for evidence in resume.evidence
        if any(_has_phrase(evidence.text, (term,)) for term in terms)
    ]
    candidates = [evidence for _, evidence in ranked_candidates]
    candidates = sorted(
        _dedupe_evidence(candidates),
        key=lambda item: (-_STRENGTH_RANK[_evidence_strength(item)], item.char_start, item.evidence_id),
    )[:4]
    if not candidates:
        return _absence_result(requirement, resume, reason_prefix="requirement")
    strongest = _evidence_strength(candidates[0])
    strongest_overlap = max(
        overlap
        for overlap, evidence in ranked_candidates
        if evidence.evidence_id in {item.evidence_id for item in candidates[:4]}
    )
    minimum_overlap = 1 if len(terms) <= 2 else 2
    if strongest in {"applied", "demonstrated"} and strongest_overlap >= minimum_overlap:
        return _result(
            requirement,
            status="met",
            reason_code="generic_requirement_evidenced",
            evidence=candidates,
            confidence=min(0.9, 0.55 + 0.1 * _STRENGTH_RANK[strongest]),
            evidence_explanation="CV có bằng chứng theo ngữ cảnh thực hành liên quan đến yêu cầu JD.",
        )
    return _result(
        requirement,
        status="unknown",
        reason_code="generic_requirement_evidence_weak",
        evidence=candidates,
        confidence=0.35,
        evidence_explanation=(
            "CV có đề cập thuật ngữ liên quan nhưng chưa thể hiện đủ ngữ cảnh thực hành, "
            "mức độ hoặc thời lượng để xác nhận yêu cầu."
        ),
    )


def _evaluate_gpa(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    requirement_match = _GPA_RE.search(_normalize(requirement.raw_label))
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
        gpa_evidence = [item for item in linked_evidence if _GPA_RE.search(_normalize(item.text))]
        return _result(
            requirement,
            status="met" if meets else "not_met",
            reason_code="gpa_threshold_met" if meets else "gpa_below_threshold",
            evidence=gpa_evidence or linked_evidence,
            confidence=1.0,
        )

    for evidence in resume.evidence:
        match = _GPA_RE.search(_normalize(evidence.text))
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
        status="unknown",
        reason_code="gpa_evidence_missing",
        confidence=0.0,
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

    for evidence in resume.evidence:
        match = _CREDENTIAL_RE.search(_normalize(evidence.text))
        if not match or match.group("credential") != credential or not match.group("value"):
            continue
        candidate = float(match.group("value"))
        meets = threshold is None or candidate >= threshold
        return _result(
            requirement,
            status="met" if meets else "not_met",
            reason_code=("language_credential_evidenced" if meets else "language_credential_below_minimum"),
            evidence=[evidence],
            confidence=1.0,
        )
    return _result(
        requirement,
        status="unknown",
        reason_code="language_credential_evidence_missing",
        confidence=0.0,
    )


def _evaluate_education_status(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    status_phrases = ("final year", "final-year", "sinh vien nam 4", "recent graduate", "moi tot nghiep")
    field_phrases = (
        "information technology",
        "software engineering",
        "computer science",
        "cntt",
        "cong nghe thong tin",
    )
    evidence_by_id = {item.evidence_id: item for item in resume.evidence}
    structured_evidence = []
    for education in resume.education:
        field = _normalize(education.field_of_study or "")
        if education.student_status in {"final_year", "recent_graduate"} and _has_phrase(
            field, field_phrases
        ):
            linked_evidence = [
                evidence_by_id[ref] for ref in education.evidence_refs if ref in evidence_by_id
            ]
            status_evidence = [
                item
                for item in linked_evidence
                if _has_phrase(item.text, status_phrases)
                and _has_phrase(item.text, field_phrases)
            ]
            structured_evidence.extend(status_evidence or linked_evidence)
    if structured_evidence:
        return _result(
            requirement,
            status="met",
            reason_code="education_status_and_field_evidenced",
            evidence=structured_evidence,
            confidence=1.0,
        )
    evidence = [
        item
        for item in resume.evidence
        if _has_phrase(item.text, status_phrases) and _has_phrase(item.text, field_phrases)
    ]
    if evidence:
        return _result(
            requirement,
            status="met",
            reason_code="education_status_and_field_evidenced",
            evidence=evidence,
            confidence=1.0,
        )
    return _result(
        requirement,
        status="unknown",
        reason_code="education_evidence_missing",
        confidence=0.0,
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
    concepts = [
        _evaluate_atomic_concept(
            concept=concept,
            resume=resume,
            minimum_strength=minimum_strength,
        )
        for concept in requested
    ]
    status = _group_status(concepts, group_operator)
    confidence = _group_confidence(concepts, group_operator)
    return _result(
        requirement,
        status=status,
        reason_code=(
            "concept_group_evidenced"
            if status == "met"
            else "concept_group_not_met"
            if status == "not_met"
            else "concept_group_evidence_missing"
        ),
        confidence=confidence,
        concept_results=concepts,
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


def _evidence_strength(evidence: EvidenceSpan) -> Literal["mention", "claimed", "applied", "demonstrated"]:
    text = _normalize(evidence.text)
    if re.search(
        r"\b(?:research(?:ed)?|scientific|benchmark(?:ed)?|evaluat(?:ed|ion)|"
        r"experiment|nghien cuu|danh gia|thu nghiem)\b",
        text,
    ):
        return "demonstrated"
    if re.search(
        r"\b(?:built|developed|implemented|integrated|deployed|pipeline|workflow|"
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
) -> ConceptResult:
    evidence_by_id = {item.evidence_id: item for item in resume.evidence}
    structured_refs = [
        ref
        for claim in resume.skills
        if claim.concept.concept_id == concept.concept_id
        for ref in claim.evidence_refs
    ]
    label_key = concept.label.casefold().strip()
    match_phrases = [concept.label]
    if label_key in _CONCEPT_ALIASES:
        match_phrases.extend(_CONCEPT_ALIASES[label_key])
    elif concept.concept_id in _CONCEPT_ALIASES:
        match_phrases.extend(_CONCEPT_ALIASES[concept.concept_id])
    candidates = _dedupe_evidence(
        [evidence_by_id[ref] for ref in structured_refs if ref in evidence_by_id]
        + _matching_evidence(resume, tuple(dict.fromkeys(match_phrases)))
    )
    ranked = sorted(
        candidates,
        key=lambda item: (-_STRENGTH_RANK[_evidence_strength(item)], item.char_start, item.evidence_id),
    )[:2]
    if not ranked:
        coverage = _raw_coverage(resume)
        return ConceptResult(
            conceptId=concept.concept_id,
            label=concept.label,
            status="not_met" if coverage == "complete" else "unknown",
            confidence=0.9 if coverage == "complete" else 0.0,
            reasonCode="concept_evidence_missing",
        )
    strength = _evidence_strength(ranked[0])
    strength_rank = _STRENGTH_RANK[strength]
    sufficient = strength_rank >= _STRENGTH_RANK[minimum_strength]
    return ConceptResult(
        conceptId=concept.concept_id,
        label=concept.label,
        status="met" if sufficient else "unknown",
        confidence=min(0.95, 0.25 + 0.16 * strength_rank + 0.04 * (len(ranked) - 1)),
        evidenceRefs=_unique_refs(ranked),
        evidenceStrength=strength,
        reasonCode="concept_evidence_sufficient" if sufficient else "concept_evidence_too_weak",
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
        status="unknown",
        reason_code="programming_foundation_evidence_missing",
        evidence=supported_languages.values(),
        confidence=0.4 if supported_languages else 0.0,
    )


def _evaluate_research_or_competition(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    evidence = _matching_evidence(
        resume,
        ("scientific research", "nlp research", "competition", "cuoc thi", "research & ai projects"),
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
        status="unknown",
        reason_code="research_or_competition_evidence_missing",
        confidence=0.0,
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
        status="unknown",
        reason_code="ai_activity_evidence_missing",
        confidence=0.0,
    )


def evaluate_unresolved_requirement(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
) -> RequirementResult:
    selection = select_evaluator(requirement)
    evaluators = {
        "gpa": _evaluate_gpa,
        "language_credential": _evaluate_language,
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
        evidence_coverage=(resume.parsing.evidence_index_coverage if resume.parsing else "legacy"),
        raw_text_coverage=(resume.parsing.raw_text_coverage if resume.parsing else "legacy"),
        canonical_section_coverage=(resume.parsing.canonical_section_coverage if resume.parsing else {}),
        retrieval_methods=["structured_concept_refs", "exact_alias_phrase", "generic_token_overlap"],
    )
    return result
