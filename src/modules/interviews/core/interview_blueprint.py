"""Interview blueprint: the stage policy an interview expert configures and versions.

Before this module every number of the stage flow (preset turn timings, pacing
ratios, reserves, closing Q&A limits) was a constant spread over the planner,
the question selector and the engine. They now live in one validated object.
``default_blueprint()`` holds exactly the values those constants had, so a
session without a configured blueprint behaves as before.

The stage *types* stay fixed (WARM_UP, VALIDATE, DEEP_DIVE, CHALLENGE,
BEHAVIORAL, CLOSING): the engine, scoring and reports understand each of them.
A blueprint tunes them and turns the optional ones off; it cannot invent a new
type. Demo sessions ignore the blueprint (core.demo_mode) so the scripted demo
stays fixed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

BLUEPRINT_SCHEMA_VERSION = "1.0"
# Session packages the product sells (frontend questionBudget.ts), demo excluded.
PACKAGE_MINUTES = (15, 25, 45)


class _Policy(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class WarmUpPolicy(_Policy):
    """The opening self-introduction. Always asked; never probed."""

    soft_answer_seconds: int = Field(120, alias="softAnswerSeconds", ge=30, le=600)
    hard_answer_seconds: int = Field(180, alias="hardAnswerSeconds", ge=30, le=900)


class ValidatePolicy(_Policy):
    """The CV project question that checks the candidate's own experience."""

    enabled: bool = True
    soft_answer_seconds: int = Field(180, alias="softAnswerSeconds", ge=30, le=900)
    hard_answer_seconds: int = Field(240, alias="hardAnswerSeconds", ge=30, le=1200)
    probe_enabled: bool = Field(True, alias="probeEnabled")
    # Keep asking VALIDATE questions until this share of the session has passed.
    until_ratio: float = Field(0.30, alias="untilRatio", gt=0, lt=1)


class TechnicalPolicy(_Policy):
    """DEEP_DIVE and CHALLENGE questions from the Question Bank."""

    max_questions: int = Field(8, alias="maxQuestions", ge=1, le=12)
    # Budget per question: thinking 30 + soft answer 150 + one follow-up 60.
    expected_seconds_per_question: int = Field(240, alias="expectedSecondsPerQuestion", ge=60, le=1200)
    probe_enabled: bool = Field(True, alias="probeEnabled")
    deep_dive_until_ratio: float = Field(0.75, alias="deepDiveUntilRatio", gt=0, lt=1)
    challenge_until_ratio: float = Field(0.85, alias="challengeUntilRatio", gt=0, le=1)
    challenge_for_coding: bool = Field(True, alias="challengeForCoding")
    challenge_for_cv_gaps: bool = Field(True, alias="challengeForCvGaps")
    # End the session early after this many consecutive "I give up" answers.
    fast_fail_after_give_ups: int = Field(2, alias="fastFailAfterGiveUps", ge=1, le=10)


class BehavioralPolicy(_Policy):
    """The STAR question about teamwork or a hard incident."""

    enabled: bool = True
    soft_answer_seconds: int = Field(180, alias="softAnswerSeconds", ge=30, le=900)
    hard_answer_seconds: int = Field(240, alias="hardAnswerSeconds", ge=30, le=1200)
    # Time kept free for it at the end of the technical part.
    reserve_seconds: int = Field(180, alias="reserveSeconds", ge=0, le=900)


class ClosingPolicy(_Policy):
    """The candidate's questions and the goodbye."""

    qna_enabled: bool = Field(True, alias="qnaEnabled")
    max_questions: int = Field(2, alias="maxQuestions", ge=1, le=5)
    budget_seconds: int = Field(180, alias="budgetSeconds", ge=30, le=900)
    reserve_seconds: int = Field(60, alias="reserveSeconds", ge=0, le=600)
    # The Q&A opens only with at least this much time left.
    min_seconds_to_open: int = Field(180, alias="minSecondsToOpen", ge=0, le=900)


class PacingPolicy(_Policy):
    # No new question once this little time is left.
    emergency_cutoff_seconds: int = Field(90, alias="emergencyCutoffSeconds", ge=0, le=600)
    # Past this share of the session, VALIDATE/DEEP_DIVE answers get no follow-up.
    behind_schedule_ratio: float = Field(0.8, alias="behindScheduleRatio", gt=0, le=1)


class InterviewBlueprint(_Policy):
    schema_version: str = Field(BLUEPRINT_SCHEMA_VERSION, alias="schemaVersion")
    warm_up: WarmUpPolicy = Field(default_factory=WarmUpPolicy, alias="warmUp")
    validate_cv: ValidatePolicy = Field(default_factory=ValidatePolicy, alias="validate")
    technical: TechnicalPolicy = Field(default_factory=TechnicalPolicy)
    behavioral: BehavioralPolicy = Field(default_factory=BehavioralPolicy)
    closing: ClosingPolicy = Field(default_factory=ClosingPolicy)
    pacing: PacingPolicy = Field(default_factory=PacingPolicy)

    @model_validator(mode="after")
    def _consistent(self) -> InterviewBlueprint:
        for name, policy in (
            ("warmUp", self.warm_up),
            ("validate", self.validate_cv),
            ("behavioral", self.behavioral),
        ):
            if policy.hard_answer_seconds < policy.soft_answer_seconds:
                raise ValueError(f"{name}: hardAnswerSeconds must be >= softAnswerSeconds")
        if not (
            self.validate_cv.until_ratio
            < self.technical.deep_dive_until_ratio
            <= self.technical.challenge_until_ratio
        ):
            raise ValueError(
                "Ratios must satisfy validate.untilRatio < deepDiveUntilRatio <= challengeUntilRatio"
            )
        return self

    def preset_expected_seconds(self) -> int:
        """Expected seconds of the turns asked in every session, plus the closing reserve."""
        seconds = self.warm_up.soft_answer_seconds + self.closing.reserve_seconds
        if self.validate_cv.enabled:
            seconds += self.validate_cv.soft_answer_seconds
        if self.behavioral.enabled:
            seconds += self.behavioral.soft_answer_seconds
        return seconds

    def technical_question_budget(self, duration_minutes: int) -> int:
        available = int(duration_minutes) * 60 - self.preset_expected_seconds()
        per_question = self.technical.expected_seconds_per_question
        return max(1, min(self.technical.max_questions, available // per_question))

    def preview(self) -> list[dict[str, Any]]:
        """What each session package gets with this blueprint (shown before activation)."""
        rows = []
        for minutes in PACKAGE_MINUTES:
            questions = self.technical_question_budget(minutes)
            expected = (
                self.preset_expected_seconds() + questions * self.technical.expected_seconds_per_question
            )
            rows.append(
                {
                    "durationMinutes": minutes,
                    "technicalQuestions": questions,
                    "expectedSeconds": expected,
                    "fits": expected <= minutes * 60,
                }
            )
        return rows

    def activation_errors(self) -> list[str]:
        """Reasons this blueprint may not be activated (empty when it may)."""
        return [
            f"Gói {row['durationMinutes']} phút cần khoảng {row['expectedSeconds'] // 60} phút, "
            "vượt thời lượng."
            for row in self.preview()
            if not row["fits"]
        ]

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


def default_blueprint() -> InterviewBlueprint:
    return InterviewBlueprint()


def blueprint_from_payload(payload: Any) -> InterviewBlueprint:
    """The blueprint a session froze; the default when it has none (older sessions)."""
    if isinstance(payload, InterviewBlueprint):
        return payload
    if isinstance(payload, dict) and payload:
        try:
            return InterviewBlueprint.model_validate(payload)
        except ValueError:
            return default_blueprint()
    return default_blueprint()
