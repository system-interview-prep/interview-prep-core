"""Application service enforcing question-bank lifecycle invariants."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.question_bank.models import (
    ExpectedPoint,
    ExpectedPointSource,
    InterviewQuestion,
    InterviewQuestionVersion,
    KnowledgeSource,
    QuestionApproval,
    QuestionReview,
    QuestionVersionCompetency,
    QuestionVersionRole,
    QuestionVersionRubric,
    RubricAnchor,
    RubricCriterion,
)
from src.modules.question_bank.schemas import CreateQuestionDraftRequest, ReviewRequest


class QuestionBankService:
    """Transactional use cases; AI never bypasses these lifecycle checks."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_draft(
        self, payload: CreateQuestionDraftRequest, author_id: str
    ) -> InterviewQuestionVersion:
        if payload.hard_answer_seconds < payload.soft_answer_seconds:
            raise HTTPException(
                status_code=422, detail="hardAnswerSeconds phải lớn hơn hoặc bằng softAnswerSeconds."
            )
        if sum(item.is_primary for item in payload.competencies) != 1:
            raise HTTPException(status_code=422, detail="Draft phải có đúng một primary competency.")

        question = InterviewQuestion(id=uuid4(), stable_key=payload.stable_key, created_by=author_id)
        version = InterviewQuestionVersion(
            id=uuid4(),
            question_id=question.id,
            version=payload.version,
            question_type=payload.question_type,
            difficulty_band=payload.difficulty_band,
            expert_difficulty=payload.expert_difficulty,
            canonical_locale=payload.canonical_locale,
            canonical_text=payload.canonical_text,
            objective=payload.objective,
            thinking_seconds=payload.thinking_seconds,
            soft_answer_seconds=payload.soft_answer_seconds,
            hard_answer_seconds=payload.hard_answer_seconds,
            context_policy=payload.context_policy,
            personalization_policy=payload.personalization_policy,
            canonical_snapshot={},
            created_by=author_id,
            change_summary=payload.change_summary,
        )
        self.db.add_all([question, version])
        for item in payload.competencies:
            self.db.add(
                QuestionVersionCompetency(
                    question_version_id=version.id,
                    competency_id=item.competency_id,
                    relevance=item.relevance,
                    is_primary=item.is_primary,
                )
            )
        for item in payload.roles:
            self.db.add(
                QuestionVersionRole(
                    question_version_id=version.id, role_id=item.role_id, relevance=item.relevance
                )
            )
        await self.db.flush()
        return version

    async def submit(self, version_id: UUID, author_id: str) -> InterviewQuestionVersion:
        version = await self._owned_draft(version_id, author_id)
        await self._validate_publishable_contract(version)
        version.status = "IN_REVIEW"
        await self.db.flush()
        return version

    async def record_review(
        self, version_id: UUID, reviewer_id: str, payload: ReviewRequest
    ) -> QuestionReview:
        version = await self._version_or_404(version_id)
        if version.status not in {"IN_REVIEW", "PILOT_READY"}:
            raise HTTPException(status_code=409, detail="Chỉ version đang review mới nhận review.")
        review = QuestionReview(
            question_version_id=version.id,
            reviewer_id=reviewer_id,
            review_type=payload.review_type,
            decision=payload.decision,
            scores=payload.scores,
            checklist=payload.checklist,
            findings=payload.findings,
            comment=payload.comment,
        )
        if payload.decision == "REQUEST_CHANGES":
            version.status = "NEEDS_REVISION"
        elif payload.decision == "REJECT":
            version.status = "REJECTED"
        elif payload.decision == "QUARANTINE":
            version.status = "QUARANTINED_LICENSE"
        self.db.add(review)
        await self.db.flush()
        return review

    async def approve(
        self, version_id: UUID, approver_id: str, policy_version: str
    ) -> InterviewQuestionVersion:
        version = await self._version_or_404(version_id)
        if version.created_by == approver_id:
            raise HTTPException(
                status_code=403, detail="Author không được final-approve version của chính mình."
            )
        if version.status not in {"IN_REVIEW", "PILOT_READY"}:
            raise HTTPException(status_code=409, detail="Version chưa ở trạng thái có thể approve.")
        await self._validate_publishable_contract(version)
        approved_reviews = await self.db.scalar(
            select(func.count())
            .select_from(QuestionReview)
            .where(QuestionReview.question_version_id == version.id, QuestionReview.decision == "APPROVE")
        )
        if not approved_reviews:
            raise HTTPException(status_code=422, detail="Cần ít nhất một review APPROVE trước khi publish.")
        question = await self.db.get(InterviewQuestion, version.question_id)
        assert question is not None
        version.status = "APPROVED"
        question.current_approved_version_id = version.id
        self.db.add(
            QuestionApproval(
                question_version_id=version.id,
                approver_id=approver_id,
                approval_policy_version=policy_version,
            )
        )
        await self.db.flush()
        return version

    async def _owned_draft(self, version_id: UUID, author_id: str) -> InterviewQuestionVersion:
        version = await self._version_or_404(version_id)
        if version.created_by != author_id:
            raise HTTPException(status_code=403, detail="Chỉ author mới được submit draft này.")
        if version.status not in {"DRAFT", "NEEDS_REVISION"}:
            raise HTTPException(status_code=409, detail="Version không còn ở trạng thái draft.")
        return version

    async def _version_or_404(self, version_id: UUID) -> InterviewQuestionVersion:
        version = await self.db.get(InterviewQuestionVersion, version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy question version.")
        return version

    async def _validate_publishable_contract(self, version: InterviewQuestionVersion) -> None:
        primary_count = await self.db.scalar(
            select(func.count())
            .select_from(QuestionVersionCompetency)
            .where(
                QuestionVersionCompetency.question_version_id == version.id,
                QuestionVersionCompetency.is_primary.is_(True),
            )
        )
        if primary_count != 1:
            raise HTTPException(status_code=422, detail="Question version cần đúng một primary competency.")
        rubric_version_id = await self.db.scalar(
            select(QuestionVersionRubric.rubric_version_id).where(
                QuestionVersionRubric.question_version_id == version.id
            )
        )
        if rubric_version_id is None:
            raise HTTPException(status_code=422, detail="Question version chưa gắn rubric version.")
        weights = await self.db.scalar(
            select(func.coalesce(func.sum(RubricCriterion.weight), 0)).where(
                RubricCriterion.rubric_version_id == rubric_version_id
            )
        )
        if float(weights) != 1.0:
            raise HTTPException(status_code=422, detail="Tổng rubric weights phải bằng 1.0.")
        incomplete_anchors = await self.db.scalar(
            select(func.count())
            .select_from(RubricCriterion)
            .where(RubricCriterion.rubric_version_id == rubric_version_id)
            .where(
                select(func.count())
                .select_from(RubricAnchor)
                .where(RubricAnchor.criterion_id == RubricCriterion.id)
                .correlate(RubricCriterion)
                .scalar_subquery()
                != 4
            )
        )
        if incomplete_anchors:
            raise HTTPException(status_code=422, detail="Mỗi rubric criterion phải có đủ anchor level 0–3.")
        unsupported_critical = await self.db.scalar(
            select(func.count())
            .select_from(ExpectedPoint)
            .where(ExpectedPoint.question_version_id == version.id, ExpectedPoint.importance == "CRITICAL")
            .where(
                select(func.count())
                .select_from(ExpectedPointSource)
                .join(KnowledgeSource, KnowledgeSource.id == ExpectedPointSource.source_id)
                .where(
                    ExpectedPointSource.expected_point_id == ExpectedPoint.id,
                    KnowledgeSource.license_verified.is_(True),
                    KnowledgeSource.commercial_use_status == "ALLOWED",
                )
                .correlate(ExpectedPoint)
                .scalar_subquery()
                == 0
            )
        )
        if unsupported_critical:
            raise HTTPException(
                status_code=422, detail="Mỗi critical expected point cần source commercial-use đã verified."
            )
