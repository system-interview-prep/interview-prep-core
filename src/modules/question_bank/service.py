"""Transactional lifecycle and taxonomy checks for the question bank."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.question_bank.models import (
    ExpectedPoint, ExpectedPointSource, InterviewQuestion, InterviewQuestionVersion,
    KnowledgeSource, QuestionApproval, QuestionReview, QuestionVersionRubric,
    QuestionVersionTaxonomyConcept, RubricAnchor, RubricCriterion,
)
from src.modules.question_bank.schemas import CreateQuestionDraftRequest, ReviewRequest


class QuestionBankService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_draft(self, payload: CreateQuestionDraftRequest, author_id: str) -> InterviewQuestionVersion:
        if payload.hard_answer_seconds < payload.soft_answer_seconds:
            raise HTTPException(422, "hardAnswerSeconds must be >= softAnswerSeconds.")
        await self._validate_taxonomy_mappings(payload.taxonomy_version, payload.taxonomy_mappings)
        question = InterviewQuestion(id=uuid4(), stable_key=payload.stable_key, created_by=author_id)
        version = InterviewQuestionVersion(
            id=uuid4(), question_id=question.id, version=payload.version, taxonomy_version=payload.taxonomy_version,
            question_type=payload.question_type, difficulty_band=payload.difficulty_band,
            expert_difficulty=payload.expert_difficulty, canonical_locale=payload.canonical_locale,
            canonical_text=payload.canonical_text, objective=payload.objective,
            thinking_seconds=payload.thinking_seconds, soft_answer_seconds=payload.soft_answer_seconds,
            hard_answer_seconds=payload.hard_answer_seconds, context_policy=payload.context_policy,
            personalization_policy=payload.personalization_policy, canonical_snapshot={}, created_by=author_id,
            change_summary=payload.change_summary,
        )
        self.db.add_all([question, version])
        self.db.add_all(QuestionVersionTaxonomyConcept(
            question_version_id=version.id, taxonomy_version=payload.taxonomy_version,
            concept_id=item.concept_id, purpose=item.purpose, relevance=item.relevance,
        ) for item in payload.taxonomy_mappings)
        await self.db.flush()
        return version

    async def submit(self, version_id: UUID, author_id: str) -> InterviewQuestionVersion:
        version = await self._version_or_404(version_id)
        if version.created_by != author_id or version.status not in {"DRAFT", "NEEDS_REVISION"}:
            raise HTTPException(403, "Only the draft author can submit this version.")
        await self._validate_publishable_contract(version)
        version.status = "IN_REVIEW"
        await self.db.flush()
        return version

    async def record_review(self, version_id: UUID, reviewer_id: str, payload: ReviewRequest) -> QuestionReview:
        version = await self._version_or_404(version_id)
        if version.status not in {"IN_REVIEW", "PILOT_READY"}:
            raise HTTPException(409, "Only versions in review accept reviews.")
        review = QuestionReview(question_version_id=version.id, reviewer_id=reviewer_id,
            review_type=payload.review_type, decision=payload.decision, scores=payload.scores,
            checklist=payload.checklist, findings=payload.findings, comment=payload.comment)
        version.status = {"REQUEST_CHANGES": "NEEDS_REVISION", "REJECT": "REJECTED", "QUARANTINE": "QUARANTINED_LICENSE"}.get(payload.decision, version.status)
        self.db.add(review)
        await self.db.flush()
        return review

    async def approve(self, version_id: UUID, approver_id: str, policy_version: str) -> InterviewQuestionVersion:
        version = await self._version_or_404(version_id)
        if version.created_by == approver_id:
            raise HTTPException(403, "Author cannot final-approve their own version.")
        if version.status not in {"IN_REVIEW", "PILOT_READY"}:
            raise HTTPException(409, "Version is not ready for approval.")
        await self._validate_publishable_contract(version)
        approved = await self.db.scalar(select(func.count()).select_from(QuestionReview).where(
            QuestionReview.question_version_id == version.id, QuestionReview.decision == "APPROVE"))
        if not approved:
            raise HTTPException(422, "At least one APPROVE review is required.")
        question = await self.db.get(InterviewQuestion, version.question_id)
        assert question is not None
        version.status, question.current_approved_version_id = "APPROVED", version.id
        self.db.add(QuestionApproval(question_version_id=version.id, approver_id=approver_id, approval_policy_version=policy_version))
        await self.db.flush()
        return version

    async def _version_or_404(self, version_id: UUID) -> InterviewQuestionVersion:
        version = await self.db.get(InterviewQuestionVersion, version_id)
        if version is None:
            raise HTTPException(404, "Question version not found.")
        return version

    async def _validate_taxonomy_mappings(self, taxonomy_version: str, mappings: Sequence) -> None:
        expected = {"TARGET_ROLE": "job_role", "TARGET_SKILL": "skill", "PRIMARY_COMPETENCY": "competency", "SUPPORTING_COMPETENCY": "competency"}
        if sum(item.purpose == "PRIMARY_COMPETENCY" for item in mappings) != 1:
            raise HTTPException(422, "Exactly one PRIMARY_COMPETENCY is required.")
        if len({(item.concept_id, item.purpose) for item in mappings}) != len(mappings):
            raise HTTPException(422, "A taxonomy mapping cannot be duplicated.")
        ids = list({item.concept_id for item in mappings})
        result = await self.db.execute(text("SELECT concept_id, kind FROM taxonomy_concepts WHERE taxonomy_version = :version AND is_active AND concept_id = ANY(:ids)"), {"version": taxonomy_version, "ids": ids})
        kinds = {row["concept_id"]: row["kind"] for row in result.mappings()}
        if any(kinds.get(item.concept_id) != expected[item.purpose] for item in mappings):
            raise HTTPException(422, "Taxonomy mapping has an invalid or inactive concept kind.")

    async def _validate_publishable_contract(self, version: InterviewQuestionVersion) -> None:
        mappings = (await self.db.execute(select(QuestionVersionTaxonomyConcept).where(QuestionVersionTaxonomyConcept.question_version_id == version.id))).scalars().all()
        await self._validate_taxonomy_mappings(version.taxonomy_version, mappings)
        rubric_id = await self.db.scalar(select(QuestionVersionRubric.rubric_version_id).where(QuestionVersionRubric.question_version_id == version.id))
        if rubric_id is None:
            raise HTTPException(422, "Question version has no rubric version.")
        weights = await self.db.scalar(select(func.coalesce(func.sum(RubricCriterion.weight), 0)).where(RubricCriterion.rubric_version_id == rubric_id))
        if float(weights) != 1.0:
            raise HTTPException(422, "Rubric weights must total 1.0.")
        incomplete = await self.db.scalar(select(func.count()).select_from(RubricCriterion).where(RubricCriterion.rubric_version_id == rubric_id).where(select(func.count()).select_from(RubricAnchor).where(RubricAnchor.criterion_id == RubricCriterion.id).correlate(RubricCriterion).scalar_subquery() != 4))
        if incomplete:
            raise HTTPException(422, "Every criterion needs anchors 0-3.")
        unsupported = await self.db.scalar(select(func.count()).select_from(ExpectedPoint).where(ExpectedPoint.question_version_id == version.id, ExpectedPoint.importance == "CRITICAL").where(select(func.count()).select_from(ExpectedPointSource).join(KnowledgeSource, KnowledgeSource.id == ExpectedPointSource.source_id).where(ExpectedPointSource.expected_point_id == ExpectedPoint.id, KnowledgeSource.license_verified.is_(True), KnowledgeSource.commercial_use_status == "ALLOWED").correlate(ExpectedPoint).scalar_subquery() == 0))
        if unsupported:
            raise HTTPException(422, "Every CRITICAL expected point needs an allowed verified source.")
