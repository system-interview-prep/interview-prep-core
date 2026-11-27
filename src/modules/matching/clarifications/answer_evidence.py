"""Parse candidate answers into scoped, provenance-tagged matching evidence."""

import hashlib
import re
import unicodedata

from src.modules.matching.domain.schemas import (
    LanguageRequirement,
    Requirement,
    RequirementResult,
    SkillRequirement,
    UnresolvedRequirement,
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
# A Vietnamese no is usually a negated verb ("chua co", "khong su dung") or a
# bare "khong"/"chua" opening the reply. Neither reached the old list, which
# held only a handful of fixed phrases, so "Khong, toi chua co." fell through
# to the positive test - where the bare "co" alternative matched the "co" of
# "chua co" and read the refusal as a yes. A no has to be recognised as a no
# before anything is credited for it.
_NEGATIVE_ANSWER_RE = re.compile(
    # English refusals.
    r"\b(?:no|nope|never used|never worked|have not used|haven't used|do not use|"
    r"don't use|did not|didn't|not yet|no experience|no hands-on experience|"
    r"not experienced)\b"
    # A negator binding the verb that follows it: "chua tot nghiep", "khong su dung".
    r"|(?:chua|khong|chang)\s+(?:co|tung|su dung|biet|lam|dung|dat|ro|thanh thao|"
    r"kinh nghiem|hoc|duoc|tot nghiep|ra truong|xong|hoan thanh|lay|thi|nop)\b"
    # A bare negator standing as the whole reply, with or without the politeness
    # particle Vietnamese usually puts in front of it: "Khong.", "Da chua.",
    # "Da, chua a."  Without this, "Da chua" read as a yes off its "da".
    r"|(?:^|[,.;:!?]\s*|\b(?:da|vang|thua|u|um)\s+)(?:khong|chua)\s*(?:a\b)?\s*"
    r"(?:[,.;:!?]|$)",
    re.IGNORECASE,
)
# A Vietnamese yes is rarely the word "co".  It is "roi", "vang", "da roi",
# "dung roi", "tot nghiep roi" - none of which reached the old list, so a plain
# "Da roi" to "Ban da ra truong chua?" was filed as no answer at all.
_POSITIVE_ANSWER_RE = re.compile(
    r"\b(?:yes|yeah|yep|correct|that's right|i have|i've|i used|i use|i worked|"
    r"i built|i developed|i implemented|have used|worked with|hands-on|experience|"
    r"graduated|"
    r"co|roi|vang|tung|xong|dung roi|dung vay|dung the|phai roi|chinh xac|"
    r"chac chan|tat nhien|di nhien|tot nghiep|ra truong|su dung|lam viec|"
    r"da dung|dang dung|da lam|da trien khai|da hoc|da dat|da co|kinh nghiem)\b",
    re.IGNORECASE,
)
# What a negator takes with it.  The positive test runs over the answer with
# these spans removed, so "chua chac chan" cannot be read as "chac chan" and
# "khong co kinh nghiem" cannot be read as "kinh nghiem".
_NEGATED_SPAN_RE = re.compile(r"\b(?:khong|chua|chang)\s+\S+(?:\s+\S+)?")
_CEFR_RE = re.compile(r"\b([ABC][12])\b", re.IGNORECASE)
_ANSWER_SCALE_RE = re.compile(
    r"(?<!\w)(?P<value>\d+(?:[.,]\d+)?)\s*/\s*(?P<scale>\d+(?:[.,]\d+)?)(?!\w)"
)
_ANSWER_NUMBER_RE = re.compile(r"(?<!\w)(?P<value>\d+(?:[.,]\d+)?)(?!\w)")
# The wording a self-report verdict carries, so a reader can tell at a glance
# that the candidate said it and nobody has checked it.
_UNVERIFIED = "Kết quả dựa trên câu trả lời tự khai của ứng viên, chưa được xác minh độc lập."
_SELF_REPORT_CONFIDENCE = 0.68


def normalize_answer(text: str) -> str:
    """The answer stripped to plain ASCII letters, for matching against words.

    NFKD leaves ``đ`` whole - it is a letter in its own right, not a d with a
    mark - so every phrase spelled with one ("đã dùng", "chưa đạt", "được")
    silently failed to match the word lists, which are written in ASCII.  It is
    folded here with the accents.  ``đúng`` and ``dùng`` collapse together as a
    result; both read as agreement in an answer, so nothing is lost.
    """
    decomposed = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
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


def answer_value_on_scale(text: str, scale: float | None) -> float | None:
    """A graded value the candidate stated, read back onto the posting's scale.

    ``3.6/4.0`` carries its own scale and is taken as written; a bare ``3.6``
    is only read when the posting supplied the scale and the number could fit
    on it, so a reply of "2 năm" is never mistaken for a GPA.
    """
    scaled = _ANSWER_SCALE_RE.search(text)
    if scaled is not None:
        value = float(scaled.group("value").replace(",", "."))
        stated_scale = float(scaled.group("scale").replace(",", "."))
        if stated_scale <= 0:
            return None
        return value if scale is None else value * (scale / stated_scale)
    if scale is None:
        return None
    match = _ANSWER_NUMBER_RE.search(text)
    if match is None:
        return None
    value = float(match.group("value").replace(",", "."))
    return value if 0 <= value <= scale else None


def required_experience_months(requirement: Requirement) -> int | None:
    """The month threshold a requirement carries, whichever shape states it."""
    months = getattr(requirement, "minimum_experience_months", None)
    if months is not None:
        return int(months)
    if isinstance(requirement, UnresolvedRequirement):
        from src.modules.matching.evaluation.legacy_rules import _required_experience_months

        return _required_experience_months(requirement)
    return None


def is_explicit_negative(text: str) -> bool:
    return _NEGATIVE_ANSWER_RE.search(normalize_answer(text)) is not None


def is_explicit_positive(text: str) -> bool:
    without_denials = _NEGATED_SPAN_RE.sub(" ", normalize_answer(text))
    return _POSITIVE_ANSWER_RE.search(without_denials) is not None


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


def _self_report_result(
    requirement: Requirement,
    evidence: EvidenceSpan,
    *,
    status: str,
    reason_code: str,
    explanation: str,
) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement.requirement_id,
        status=status,
        score=1.0 if status == "met" else 0.0,
        confidence=_SELF_REPORT_CONFIDENCE,
        evidenceRefs=[evidence.evidence_id],
        evidenceSources={evidence.evidence_id: "candidate_self_report"},
        reasonCode=reason_code,
        evidenceExplanation=f"{explanation} {_UNVERIFIED}",
    )


def candidate_self_report_result(
    requirement: Requirement,
    answer_text: str,
    evidence: EvidenceSpan,
) -> RequirementResult | None:
    """The verdict a requirement's own evaluator cannot reach from an answer.

    Four evaluator families read structured CV fields and nothing else - the
    employment timeline, a GPA, a certification list, a degree - so a
    clarification answer about any of them was never cited and the requirement
    came back ``unknown`` however plainly the candidate answered it.  The
    question was asked, the answer was typed, and the result did not move.

    This is the floor for that case, and only that case: the caller applies it
    when the evaluator did not use the answer.  A threshold is still a
    threshold - a reply that names no quantity cannot clear one, and says so in
    its reason code instead of going quiet - and nothing here is treated as
    verified, so ``met`` arrives capped, tagged ``candidate_self_report`` and
    held back from the must-have gate by its reason code.
    """

    if is_explicit_negative(answer_text):
        return candidate_negative_result(requirement, evidence)

    months_required = required_experience_months(requirement)
    if months_required is not None:
        months = answer_duration_months(answer_text)
        if months is None:
            return _self_report_result(
                requirement,
                evidence,
                status="unknown",
                reason_code="candidate_self_report_missing_duration",
                explanation=(
                    "Câu trả lời chưa nêu thời lượng cụ thể nên chưa đủ để đối chiếu với mốc "
                    f"{months_required} tháng mà tin tuyển dụng yêu cầu."
                ),
            )
        if months >= months_required:
            return _self_report_result(
                requirement,
                evidence,
                status="met",
                reason_code="candidate_self_report_meets_requirement",
                explanation=(
                    f"Ứng viên tự khai {months} tháng kinh nghiệm, đạt mốc {months_required} tháng."
                ),
            )
        return _self_report_result(
            requirement,
            evidence,
            status="not_met",
            reason_code="candidate_self_report_explicitly_below_requirement",
            explanation=(
                f"Ứng viên tự khai {months} tháng kinh nghiệm, chưa đạt mốc {months_required} tháng."
            ),
        )

    threshold = getattr(requirement, "threshold", None)
    if threshold is not None:
        scale = getattr(requirement, "scale", None)
        value = answer_value_on_scale(answer_text, scale)
        if value is None:
            return _self_report_result(
                requirement,
                evidence,
                status="unknown",
                reason_code="candidate_self_report_missing_value",
                explanation=(
                    "Câu trả lời chưa nêu con số cụ thể nên chưa đủ để đối chiếu với mức "
                    f"{threshold} mà tin tuyển dụng yêu cầu."
                ),
            )
        if value >= float(threshold):
            return _self_report_result(
                requirement,
                evidence,
                status="met",
                reason_code="candidate_self_report_meets_requirement",
                explanation=f"Ứng viên tự khai mức {value:g}, đạt yêu cầu {threshold:g}.",
            )
        return _self_report_result(
            requirement,
            evidence,
            status="not_met",
            reason_code="candidate_self_report_explicitly_below_requirement",
            explanation=f"Ứng viên tự khai mức {value:g}, chưa đạt yêu cầu {threshold:g}.",
        )

    minimum_level = getattr(requirement, "minimum_proficiency_level", None)
    if minimum_level is not None and answer_proficiency(answer_text) is None:
        return _self_report_result(
            requirement,
            evidence,
            status="unknown",
            reason_code="candidate_self_report_missing_proficiency",
            explanation=(
                "Câu trả lời chưa nêu mức độ thành thạo nên chưa đủ để đối chiếu với yêu cầu."
            ),
        )

    if is_explicit_positive(answer_text):
        return _self_report_result(
            requirement,
            evidence,
            status="met",
            reason_code="candidate_self_report_meets_requirement",
            explanation="Ứng viên tự khai đáp ứng yêu cầu này.",
        )
    # Neither a yes nor a no: nothing was claimed, so nothing is credited.
    return _self_report_result(
        requirement,
        evidence,
        status="unknown",
        reason_code="candidate_self_report_unclear",
        explanation=(
            "Câu trả lời chưa khẳng định rõ nên chưa đủ để kết luận về yêu cầu này."
        ),
    )


def candidate_negative_result(
    requirement: Requirement,
    evidence: EvidenceSpan,
) -> RequirementResult:
    return RequirementResult(
        requirementId=requirement.requirement_id,
        status="not_met",
        score=0.0,
        confidence=_SELF_REPORT_CONFIDENCE,
        evidenceRefs=[evidence.evidence_id],
        # A "no" is the candidate's own word as much as a "yes" is, so the
        # reader sees where it came from rather than reading it as a CV finding.
        evidenceSources={evidence.evidence_id: "candidate_self_report"},
        reasonCode="candidate_self_report_explicitly_below_requirement",
        evidenceExplanation=(
            "Theo câu trả lời tự khai, ứng viên cho biết hiện chưa đáp ứng yêu cầu này. "
            "Kết quả chưa được xác minh độc lập."
        ),
    )


