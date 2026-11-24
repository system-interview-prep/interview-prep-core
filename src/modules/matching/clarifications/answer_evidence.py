"""Parse candidate answers into scoped, provenance-tagged matching evidence."""

import hashlib
import re
import unicodedata

from src.modules.matching.domain.schemas import (
    LanguageRequirement,
    Requirement,
    RequirementResult,
    SkillRequirement,
)
from src.modules.user_cvs.schemas import EvidenceSpan, LanguageClaim, SkillClaim

_ANSWER_DURATION_RE = re.compile(
    r"(?<!\w)(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>years?|yrs?|months?|mos?|năm|tháng)\b",
    re.IGNORECASE,
)
_PROFICIENCY_LEVELS = {
    "basic": "basic",
    "beginner": "beginner",
    "elementary": "basic",
    "intermediate": "intermediate",
    "competent": "competent",
    "upper intermediate": "intermediate",
    "advanced": "advanced",
    "expert": "expert",
    "so cap": "basic",
    "moi bat dau": "beginner",
    "trung cap": "intermediate",
    "nang cao": "advanced",
    "thanh thao": "advanced",
    "chuyen gia": "expert",
}
_NEGATIVE_ANSWER_RE = re.compile(
    r"\b(?:never used|never worked|have not used|haven't used|do not use|don't use|"
    r"no experience|no hands-on experience|not experienced|chua tung|chua su dung|"
    r"chua co kinh nghiem|khong co kinh nghiem|khong su dung|khong biet)\b",
    re.IGNORECASE,
)
_POSITIVE_ANSWER_RE = re.compile(
    r"\b(?:yes|i have|i used|i use|i worked|i built|i developed|i implemented|"
    r"have used|worked with|hands-on|experience|co|da su dung|da lam|da trien khai|"
    r"tung su dung|kinh nghiem)\b",
    re.IGNORECASE,
)
_CEFR_RE = re.compile(r"\b([ABC][12])\b", re.IGNORECASE)


def normalize_answer(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return ascii_text.casefold()


def answer_duration_months(text: str) -> int | None:
    match = _ANSWER_DURATION_RE.search(text)
    if match is None:
        return None
    value = float(match.group("value").replace(",", "."))
    unit = match.group("unit").casefold()
    return round(value * 12) if unit.startswith(("year", "yr", "năm")) else round(value)


def answer_proficiency(text: str) -> str | None:
    normalized = normalize_answer(text)
    for label in sorted(_PROFICIENCY_LEVELS, key=len, reverse=True):
        if label in normalized:
            return _PROFICIENCY_LEVELS[label]
    return None


def is_explicit_negative(text: str) -> bool:
    return _NEGATIVE_ANSWER_RE.search(normalize_answer(text)) is not None


def is_explicit_positive(text: str) -> bool:
    return _POSITIVE_ANSWER_RE.search(normalize_answer(text)) is not None


def build_candidate_answer_evidence(
    requirement_id: str,
    answer_text: str,
) -> EvidenceSpan:
    answer_hash = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()
    requirement_hash = hashlib.sha256(requirement_id.encode()).hexdigest()[:8]
    evidence_ref = f"clarification-{requirement_hash}-{answer_hash[:16]}"
    return EvidenceSpan(
        evidenceId=evidence_ref,
        documentId=f"candidate-answer-{answer_hash[:16]}",
        documentSha256=answer_hash,
        section="candidate_self_report",
        text=answer_text,
        charStart=0,
        charEnd=len(answer_text),
        evidenceSource="candidate_self_report",
        sourceRequirementId=requirement_id,
    )


def skill_claim_from_answer(
    requirement: Requirement,
    answer_text: str,
    evidence: EvidenceSpan,
) -> SkillClaim | None:
    if not isinstance(requirement, SkillRequirement):
        return None
    months = answer_duration_months(answer_text)
    proficiency = answer_proficiency(answer_text)
    negative = is_explicit_negative(answer_text)
    positive = is_explicit_positive(answer_text)

    if requirement.operator == "gte":
        if negative and months is None:
            months = 0
        if months is None:
            return None
    elif requirement.operator == "proficiency_gte":
        if proficiency is None:
            return None
    elif not negative and not positive:
        return None

    return SkillClaim(
        claimId=f"self-report-{evidence.evidence_id}",
        concept=requirement.skill,
        rawLabel=requirement.skill.label,
        experienceMonths=months,
        proficiencyLevel=proficiency,
        evidenceRefs=[evidence.evidence_id],
        assertionSource="explicit",
        confidence=0.68,
    )


def language_claim_from_answer(
    requirement: Requirement,
    answer_text: str,
    evidence: EvidenceSpan,
) -> LanguageClaim | None:
    if not isinstance(requirement, LanguageRequirement):
        return None
    level_match = _CEFR_RE.search(answer_text)
    if requirement.operator == "equal" and not level_match:
        return None
    if requirement.operator != "equal" and not is_explicit_positive(answer_text):
        return None
    return LanguageClaim(
        code=requirement.language_code,
        level=level_match.group(1).upper() if level_match else None,
        framework="CEFR" if level_match else None,
        evidenceRefs=[evidence.evidence_id],
    )


def candidate_negative_result(
    requirement: Requirement,
    evidence: EvidenceSpan,
) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement.requirement_id,
        status="not_met",
        score=0.0,
        confidence=0.68,
        evidenceRefs=[evidence.evidence_id],
        reasonCode="candidate_self_report_explicitly_below_requirement",
        evidenceExplanation=(
            "Theo câu trả lời tự khai, ứng viên cho biết hiện chưa đáp ứng yêu cầu này. "
            "Kết quả chưa được xác minh độc lập."
        ),
    )


