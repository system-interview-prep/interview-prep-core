"""Which JD requirements are allowed to disqualify a candidate.

A JD parser extracts whatever the posting says.  Postings routinely contain
lines that are not assessable conditions at all: personality wishes
("năng động, vui vẻ"), aspirations ("Yêu thích công nghệ"), protected or
physical attributes (age, health, gender, voice), equipment the employer
expects you to own ("Có laptop"), and *negated* requirements ("No experience
required") that widen the opening instead of narrowing it.  A separate case is
the line the posting itself marks optional - "Có kiến thức về API là 1 lợi
thế" - which names a checkable skill but states it is a bonus.  When any of
these is labelled ``must_have`` and the evaluator then looks for CV evidence
of it, the candidate is rejected for failing to prove something that was never
a condition.

This module classifies every requirement before matching so the eligibility
gate only ever fires on objectively checkable conditions.  Nothing is deleted
silently: each demotion is returned with a reason so the decision stays
auditable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

GateClass = Literal["objective", "optional", "soft_signal", "non_assessable", "vacuous"]


@dataclass(frozen=True)
class GateDecision:
    """How one requirement may participate in eligibility and scoring."""

    gate_class: GateClass
    reason_code: str | None = None

    @property
    def may_gate(self) -> bool:
        """Only objective conditions can turn a pair ``ineligible``."""
        return self.gate_class == "objective"

    @property
    def is_scorable(self) -> bool:
        """Vacuous, protected and equipment lines carry no fit signal."""
        return self.gate_class in ("objective", "optional", "soft_signal")


def _normalize(text: str) -> str:
    """Fold case and Vietnamese diacritics so one pattern matches both forms."""
    lowered = (text or "").lower()
    decomposed = unicodedata.normalize("NFD", lowered)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", stripped.replace("đ", "d")).strip()


# A negated requirement: the posting says the condition is NOT required.
# Treating it as a must-have inverts its meaning and rejects everyone.
_VACUOUS = re.compile(
    r"khong\s*(yeu\s*cau|can|doi\s*hoi|bat\s*buoc)"
    r"|no\s+(prior\s+)?experience\s+(is\s+)?(required|needed|necessary)"
    r"|not\s+required"
    r"|chua\s+co\s+kinh\s+nghiem\s+(cung|van)"
    # "Chấp nhận sinh viên chưa có kinh nghiệm" waives the condition exactly as
    # "không yêu cầu kinh nghiệm" does.  Read as a must-have it rejects the very
    # people the line was written to invite.
    r"|(chap|tiep)\s*nhan\s[^.]{0,40}chua\s*co\s*kinh\s*nghiem"
    r"|chua\s*co\s*kinh\s*nghiem\s*$"
    r"|(fresher|sinh\s*vien|new\s*graduate)s?[^.]{0,24}(welcome|chao\s*don)"
    r"|khong\s+gioi\s+han"
    r"|no\s+specific\s+requirement",
    re.I,
)

# Protected or physical attributes.  These must never gate, and they must not
# be scored either - doing so would launder discrimination into a fit number.
_NON_ASSESSABLE = re.compile(
    r"do\s*tuoi|tuoi\s*tu|tu\s*\d{2}\s*(den|-)\s*\d{2}\s*tuoi|\bage\s*(from|range|between)"
    r"|suc\s*khoe|du\s*suc|good\s+health|physically\s+fit"
    r"|gioi\s*tinh|\bnam\b\s*/\s*\bnu\b|\bmale\b|\bfemale\b"
    # A bare gender opening a requirement ("Nam, từ 18-27").  Anchored to the
    # start and followed by a separator on purpose: folding diacritics turns
    # "năm" (year) into "nam", so an unanchored \bnam\b would demote every
    # requirement that states a duration in years.
    r"|^(nam|nu)\s*[,:;.]"
    r"|tinh\s*trang\s*hon\s*nhan|marital|ngoai\s*hinh|appearance"
    r"|ho\s*khau|temporary\s+residence",
    re.I,
)

# Attitude and personality wishes.  Real signal for a recruiter, but a CV
# cannot prove them, so they belong in the score, never in the gate.
_SOFT_SIGNAL = re.compile(
    r"nang\s*dong|vui\s*ve|than\s*thien|nhiet\s*tinh|cau\s*tien|trung\s*thuc"
    # "tỉ mỉ" folds to "ti mi"; the boundaries keep it out of "optimization".
    r"|can\s*than|cham\s*chi|\bti\s*mi\b|kien\s*nhan|ham\s*hoc|chiu\s*(duoc\s*)?ap\s*luc"
    r"|thai\s*do|tinh\s*than\s*(trach\s*nhiem|hoc\s*hoi|cau\s*tien)"
    r"|giao\s*tiep(\s*tot)?|lam\s*viec\s*nhom|teamwork|team\s*work|team\s*player"
    # "kỹ năng mềm" folds to "ky nang mem"; the boundary keeps it from eating
    # "kỹ năng memory management", which is a concrete technical skill.
    r"|ky\s*nang\s*mem\b|soft\s*skills?"
    r"|problem[\s-]*solving|critical\s*thinking|analytical\s*(and\s*)?(evaluative\s*)?skills?"
    r"|communication\s*skills?|interpersonal|attitude|enthusias|proactive|self[\s-]*motivated"
    r"|chu\s*dong|dam\s*me|passion|eager\s+to\s+learn|willing(ness)?\s+to\s+learn"
    # Any "tư duy X" is a claim about how someone thinks.  The objective
    # anchor below still rescues the mixed lines ("tư duy thuật toán").
    r"|tu\s*duy\b|logical\s+thinking"
    r"|dinh\s*huong\s*theo"
    # Liking and wanting.  An aspiration is not a qualification, yet these
    # arrive as must-haves ("Yêu thích công nghệ", "Want to develop in data
    # analysis") and reject anyone whose CV does not happen to say it.
    r"|yeu\s*thich|mong\s*muon|nguyen\s*vong|say\s*me"
    r"|want(s|ing)?\s+to\s+(develop|grow|learn)|wish(es)?\s+to|aspir"
    # Diligence wording the "chăm chỉ|cầu tiến" set did not reach.
    r"|chiu\s*kho|tim\s*toi|hoc\s*hoi|cap\s*nhat\s*kien\s*thuc"
    # Working with others, in either language.  "teamwork" alone missed both
    # "làm việc độc lập hoặc theo nhóm" and "Ability to collaborate with others".
    r"|lam\s*viec\s*(doc\s*lap|nhom|theo\s*nhom)|doc\s*lap\s*(hoac|va)\s*(theo\s*)?nhom"
    r"|work\s+(independently|in\s+a\s+team|with\s+others)|within\s+a\s+team|collaborat"
    # Juggling work: a claim about working style, not a checkable condition.
    r"|multiple\s+(tasks|assignments|projects)|multi[\s-]*task"
    r"|ability\s+to\s+prioriti[sz]|flexib|linh\s*hoat|da\s*nhiem",
    re.I,
)

# Guard: a line may mention a soft trait while still carrying a hard condition
# ("Tốt nghiệp Đại học CNTT, nhiệt tình").  A concrete technology, degree,
# credential or duration keeps the requirement objective.
_OBJECTIVE_ANCHOR = re.compile(
    r"\b\d+\s*(nam|year|thang|month)"
    r"|tot\s*nghiep|bang\s*(cap|dai\s*hoc)|degree|bachelor|master|cu\s*nhan|ky\s*su"
    r"|dai\s*hoc|cao\s*dang|college|university"
    r"|ielts|toeic|toefl|jlpt|hsk|\bgpa\b|certif|chung\s*chi"
    r"|database|co\s*so\s*du\s*lieu|design\s*pattern|thuat\s*toan|algorithm"
    r"|data\s*structure|cau\s*truc\s*du\s*lieu|\boop\b|\bood\b|microservice"
    r"|performance\s*tuning|toi\s*uu\s*(hoa\s*)?(truy\s*van|hieu\s*nang)"
    r"|java|python|javascript|typescript|c\+\+|c#|\.net|php|golang|ruby|kotlin|swift"
    r"|react|angular|vue|node|spring|django|flask|laravel|flutter"
    r"|sql|mysql|postgres|mongodb|oracle|redis|docker|kubernetes|aws|azure|linux"
    r"|git\b|jira|rest|soap|api\b|html|css",
    re.I,
)


# A named human language keeps a requirement objective even when it is phrased
# with communication wording ("Good English communication skills").  Language
# ability is evidenced from a CV's language section, credentials or its own
# wording, so it belongs with the checkable conditions rather than the wishes.
_NAMED_LANGUAGE = re.compile(
    r"tieng\s*(anh|nhat|han|trung|hoa|phap|duc|viet)"
    r"|\benglish\b|\bjapanese\b|\bkorean\b|\bchinese\b|\bmandarin\b"
    r"|\bfrench\b|\bgerman\b|\bvietnamese\b|\bnihongo\b",
    re.I,
)


# Voice and delivery.  Real conditions for a phone-facing role, but assessed
# in an interview, never from a document.
_VOICE = re.compile(
    r"giong\s*(noi|chuan|dia\s*phuong|truyen\s*cam)|phat\s*am"
    r"|clear\s+voice|voice\s+quality|pleasant\s+voice|noi\s*ngong",
    re.I,
)

# Equipment and logistics.  A genuine condition of the job, but a CV is not
# where it is evidenced: a candidate who owns a laptop is under no obligation
# to write it down.  Keep it out of both the gate and the score.
_EQUIPMENT = re.compile(
    r"\bco\s+(laptop|may\s*tinh|xe\s*may|o\s*to|phuong\s*tien)\b"
    r"|own\s+(a\s+)?(laptop|computer|car|motorbike|vehicle)"
    r"|tu\s*trang\s*bi|bring\s+your\s+own",
    re.I,
)

# The posting itself says the item is a bonus.  Whatever priority the extractor
# attached, a line reading "là 1 lợi thế" is not a condition of entry, and
# letting it disqualify anyone contradicts the sentence it was taken from.
_OPTIONAL_MARKER = re.compile(
    r"la\s*(mot|1)?\s*(loi\s*the|diem\s*cong|uu\s*the)"
    r"|\bis\s+(a|an)\s+(plus|bonus|advantage|asset)\b"
    r"|\bis\s+great\b|\b(nice|good)\s+to\s+have\b"
    r"|\bpreferred\b|\bpreferably\b|\bbonus\s+points?\b"
    r"|\buu\s*tien\b",
    re.I,
)


def _marks_whole_requirement_optional(text: str) -> bool:
    """Whether "là lợi thế" speaks for the requirement or only for one clause.

    "Nền tảng lập trình vững chắc; năng khiếu toán học là lợi thế" states a
    condition *and* a bonus.  Reading the marker as the whole line's would waive
    the condition - the mirror of the mistake this module exists to prevent.  A
    marker qualifies the clause it sits in, so it only speaks for the whole
    requirement once it reaches the opening clause.
    """
    clauses = [part for part in re.split(r"[;,.]", text) if part.strip()]
    return bool(clauses) and bool(_OPTIONAL_MARKER.search(clauses[0]))


def classify_requirement_gate(raw_label: str, kind: str | None = None) -> GateDecision:
    """Decide whether one requirement may disqualify a candidate.

    ``kind`` from the JD parser is advisory only.  It is unreliable in practice
    (education lines arrive as ``skill``, health lines as ``language``), so the
    decision is driven by the requirement text itself.
    """
    text = _normalize(raw_label)
    if not text:
        return GateDecision("non_assessable", "requirement_text_empty")

    if _VACUOUS.search(text) and not _OBJECTIVE_ANCHOR.search(text):
        return GateDecision("vacuous", "requirement_is_negated")

    if _NON_ASSESSABLE.search(text):
        return GateDecision("non_assessable", "protected_or_physical_attribute")

    if _VOICE.search(text):
        return GateDecision("non_assessable", "voice_not_evidenced_by_a_document")

    if _EQUIPMENT.search(text):
        return GateDecision("non_assessable", "equipment_not_evidenced_by_a_cv")

    if (
        _SOFT_SIGNAL.search(text)
        and not _OBJECTIVE_ANCHOR.search(text)
        and not _NAMED_LANGUAGE.search(text)
    ):
        return GateDecision("soft_signal", "attitude_not_verifiable_from_cv")

    # Checked last, and deliberately not anchor-guarded: "Có kiến thức về SQL là
    # một lợi thế" names a concrete skill *and* says it is optional.  The anchor
    # tells us the line is assessable, not that it is required.
    if _marks_whole_requirement_optional(text):
        return GateDecision("optional", "posting_marks_requirement_optional")

    return GateDecision("objective")


__all__ = ["GateDecision", "GateClass", "classify_requirement_gate"]
