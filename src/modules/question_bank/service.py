"""Transactional lifecycle and taxonomy checks for the question bank."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.question_bank.models import (
    ExpectedPoint,
    ExpectedPointSource,
    InterviewQuestion,
    InterviewQuestionVersion,
    KnowledgeSource,
    QuestionApproval,
    QuestionReview,
    QuestionVersionRubric,
    QuestionVersionTaxonomyConcept,
    Rubric,
    RubricAnchor,
    RubricCriterion,
    RubricVersion,
)
from src.modules.question_bank.schemas import (
    AttachRubricRequest,
    CreateQuestionDraftRequest,
    ReviewRequest,
    UpdateQuestionDraftRequest,
)

# Career codes are taxonomy specializations, which the taxonomy seed stores as
# kind `competency`; no `job_role` concepts exist. TARGET_ROLE must accept both
# or the P1 career-classification fallback can never be served by an authored
# question.
_MAPPING_KINDS: dict[str, frozenset[str]] = {
    "TARGET_ROLE": frozenset({"job_role", "competency"}),
    "TARGET_SKILL": frozenset({"skill"}),
    "PRIMARY_COMPETENCY": frozenset({"competency"}),
    "SUPPORTING_COMPETENCY": frozenset({"competency"}),
}
_EDITABLE_STATUSES = frozenset({"DRAFT", "NEEDS_REVISION"})
_WEIGHT_TOLERANCE = Decimal("0.0001")


def _next_version(current: str, taken: set[str]) -> str:
    """Bump the last numeric component ("1.0.0" -> "1.0.1"), skipping versions in use."""
    parts = current.split(".")
    if parts and parts[-1].isdigit():
        prefix, number = ".".join(parts[:-1]), int(parts[-1])
        while True:
            number += 1
            candidate = f"{prefix}.{number}" if prefix else str(number)
            if candidate not in taken:
                return candidate
    suffix = 2
    while f"{current}-r{suffix}" in taken:
        suffix += 1
    return f"{current}-r{suffix}"


class QuestionBankService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_draft(self, payload: CreateQuestionDraftRequest, author_id: str) -> InterviewQuestionVersion:
        if payload.hard_answer_seconds < payload.soft_answer_seconds:
            raise HTTPException(422, "hardAnswerSeconds must be >= softAnswerSeconds.")
        await self._validate_taxonomy_mappings(payload.taxonomy_version, payload.taxonomy_mappings)
        duplicate = select(InterviewQuestion.id).where(InterviewQuestion.stable_key == payload.stable_key)
        if await self.db.scalar(duplicate):
            raise HTTPException(409, "A question with this stableKey already exists.")
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

    async def revise(self, version_id: UUID, author_id: str) -> InterviewQuestionVersion:
        """Start a new DRAFT version from an existing one.

        Interviews keep using the question's current approved version until the
        revision is itself approved; frozen sessions hold their own snapshot.
        """
        source = await self._version_or_404(version_id)
        open_draft = await self.db.scalar(
            select(InterviewQuestionVersion.id).where(
                InterviewQuestionVersion.question_id == source.question_id,
                InterviewQuestionVersion.status.in_(("DRAFT", "NEEDS_REVISION", "IN_REVIEW", "PILOT_READY")),
            )
        )
        if open_draft:
            raise HTTPException(409, "This question already has a version in progress.")
        taken = set(
            await self.db.scalars(
                select(InterviewQuestionVersion.version).where(
                    InterviewQuestionVersion.question_id == source.question_id
                )
            )
        )
        version = InterviewQuestionVersion(
            id=uuid4(), question_id=source.question_id, version=_next_version(source.version, taken),
            taxonomy_version=source.taxonomy_version, question_type=source.question_type,
            difficulty_band=source.difficulty_band, expert_difficulty=source.expert_difficulty,
            canonical_locale=source.canonical_locale, canonical_text=source.canonical_text,
            objective=source.objective, thinking_seconds=source.thinking_seconds,
            soft_answer_seconds=source.soft_answer_seconds, hard_answer_seconds=source.hard_answer_seconds,
            context_policy=source.context_policy, personalization_policy=source.personalization_policy,
            canonical_snapshot=source.canonical_snapshot or {}, created_by=author_id,
            change_summary=f"Revision of {source.version}",
        )
        self.db.add(version)
        await self.db.flush()
        mappings = (
            await self.db.execute(
                select(QuestionVersionTaxonomyConcept).where(
                    QuestionVersionTaxonomyConcept.question_version_id == source.id
                )
            )
        ).scalars().all()
        self.db.add_all(
            QuestionVersionTaxonomyConcept(
                question_version_id=version.id, taxonomy_version=item.taxonomy_version,
                concept_id=item.concept_id, purpose=item.purpose, relevance=item.relevance,
            )
            for item in mappings
        )
        rubric_version_id = await self.db.scalar(
            select(QuestionVersionRubric.rubric_version_id)
            .where(QuestionVersionRubric.question_version_id == source.id)
        )
        if rubric_version_id is not None:
            self.db.add(QuestionVersionRubric(question_version_id=version.id, rubric_version_id=rubric_version_id))
        await self.db.flush()
        return version

    async def update_draft(
        self, version_id: UUID, author_id: str, payload: UpdateQuestionDraftRequest
    ) -> InterviewQuestionVersion:
        version = await self._version_or_404(version_id)
        if version.created_by != author_id or version.status not in _EDITABLE_STATUSES:
            raise HTTPException(403, "Only the draft author can edit an editable version.")
        changes = payload.model_dump(exclude_unset=True, exclude={"taxonomy_mappings"})
        for field_name, value in changes.items():
            if value is not None:
                setattr(version, field_name, value)
        if version.hard_answer_seconds < version.soft_answer_seconds:
            raise HTTPException(422, "hardAnswerSeconds must be >= softAnswerSeconds.")
        if payload.taxonomy_mappings is not None:
            await self._validate_taxonomy_mappings(version.taxonomy_version, payload.taxonomy_mappings)
            existing = (
                await self.db.execute(
                    select(QuestionVersionTaxonomyConcept).where(
                        QuestionVersionTaxonomyConcept.question_version_id == version.id
                    )
                )
            ).scalars().all()
            for item in existing:
                await self.db.delete(item)
            await self.db.flush()
            self.db.add_all(
                QuestionVersionTaxonomyConcept(
                    question_version_id=version.id, taxonomy_version=version.taxonomy_version,
                    concept_id=item.concept_id, purpose=item.purpose, relevance=item.relevance,
                )
                for item in payload.taxonomy_mappings
            )
        await self.db.flush()
        return version

    async def attach_rubric(
        self, version_id: UUID, author_id: str, payload: AttachRubricRequest
    ) -> RubricVersion:
        """Link an existing rubric version to a draft, or create one inline."""
        version = await self._version_or_404(version_id)
        if version.created_by != author_id or version.status not in _EDITABLE_STATUSES:
            raise HTTPException(403, "Only the draft author can attach a rubric to an editable version.")
        if payload.rubric_version_id is not None:
            if payload.criteria:
                raise HTTPException(422, "Send either rubricVersionId or criteria, not both.")
            rubric_version = await self.db.get(RubricVersion, payload.rubric_version_id)
            if rubric_version is None:
                raise HTTPException(404, "Rubric version not found.")
        else:
            rubric_version = await self._create_rubric_version(version, author_id, payload)
        link = await self.db.get(QuestionVersionRubric, version.id)
        if link is None:
            self.db.add(
                QuestionVersionRubric(question_version_id=version.id, rubric_version_id=rubric_version.id)
            )
        else:
            link.rubric_version_id = rubric_version.id
        await self.db.flush()
        return rubric_version

    async def _create_rubric_version(
        self, version: InterviewQuestionVersion, author_id: str, payload: AttachRubricRequest
    ) -> RubricVersion:
        if not payload.criteria:
            raise HTTPException(422, "A rubric needs at least one criterion.")
        if len({item.stable_key for item in payload.criteria}) != len(payload.criteria):
            raise HTTPException(422, "Rubric criterion keys must be unique.")
        if abs(sum(item.weight for item in payload.criteria) - Decimal(1)) > _WEIGHT_TOLERANCE:
            raise HTTPException(422, "Rubric weights must total 1.0.")
        question = await self.db.get(InterviewQuestion, version.question_id)
        assert question is not None
        rubric_key = f"{question.stable_key}@{version.version}"
        rubric = await self.db.scalar(select(Rubric).where(Rubric.stable_key == rubric_key))
        if rubric is None:
            rubric = Rubric(id=uuid4(), stable_key=rubric_key)
            self.db.add(rubric)
            await self.db.flush()
        existing = await self.db.scalar(
            select(func.count()).select_from(RubricVersion).where(RubricVersion.rubric_id == rubric.id)
        )
        rubric_version = RubricVersion(
            id=uuid4(), rubric_id=rubric.id, version=str((existing or 0) + 1),
            minimum_coverage=payload.minimum_coverage, aggregation_method="weighted_mean",
            created_by=author_id,
        )
        self.db.add(rubric_version)
        await self.db.flush()
        criteria = [
            RubricCriterion(
                id=uuid4(), rubric_version_id=rubric_version.id, stable_key=item.stable_key,
                name=item.name, description=item.description, weight=item.weight,
                critical=item.critical, display_order=order,
            )
            for order, item in enumerate(payload.criteria)
        ]
        self.db.add_all(criteria)
        # The models declare no relationships, so the unit of work cannot order
        # anchor inserts after their criteria on its own.
        await self.db.flush()
        self.db.add_all(
            RubricAnchor(criterion_id=criterion.id, level=anchor.level, description=anchor.description)
            for criterion, item in zip(criteria, payload.criteria, strict=True)
            for anchor in item.anchors
        )
        rubric.current_version_id = rubric_version.id
        await self.db.flush()
        return rubric_version

    async def submit(self, version_id: UUID, author_id: str) -> InterviewQuestionVersion:
        version = await self._version_or_404(version_id)
        if version.created_by != author_id or version.status not in _EDITABLE_STATUSES:
            raise HTTPException(403, "Only the draft author can submit this version.")
        await self._validate_publishable_contract(version)
        version.status = "IN_REVIEW"
        await self.db.flush()
        return version

    async def record_review(self, version_id: UUID, reviewer_id: str, payload: ReviewRequest) -> QuestionReview:
        version = await self._version_or_404(version_id)
        if version.created_by == reviewer_id:
            raise HTTPException(403, "Author cannot review their own version.")
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
        rubric_version_id = await self.db.scalar(
            select(QuestionVersionRubric.rubric_version_id)
            .where(QuestionVersionRubric.question_version_id == version.id)
        )
        rubric_version = await self.db.get(RubricVersion, rubric_version_id)
        if rubric_version is not None and rubric_version.approved_at is None:
            rubric_version.approved_at = datetime.now(UTC)
        self.db.add(QuestionApproval(question_version_id=version.id, approver_id=approver_id, approval_policy_version=policy_version))
        await self.db.flush()
        return version

    async def _version_or_404(self, version_id: UUID) -> InterviewQuestionVersion:
        version = await self.db.get(InterviewQuestionVersion, version_id)
        if version is None:
            raise HTTPException(404, "Question version not found.")
        return version

    async def _validate_taxonomy_mappings(self, taxonomy_version: str, mappings: Sequence) -> None:
        if sum(item.purpose == "PRIMARY_COMPETENCY" for item in mappings) != 1:
            raise HTTPException(422, "Exactly one PRIMARY_COMPETENCY is required.")
        if len({(item.concept_id, item.purpose) for item in mappings}) != len(mappings):
            raise HTTPException(422, "A taxonomy mapping cannot be duplicated.")
        ids = list({item.concept_id for item in mappings})
        result = await self.db.execute(text("SELECT concept_id, kind FROM taxonomy_concepts WHERE taxonomy_version = :version AND is_active AND concept_id = ANY(:ids)"), {"version": taxonomy_version, "ids": ids})
        kinds = {row["concept_id"]: row["kind"] for row in result.mappings()}
        if any(kinds.get(item.concept_id) not in _MAPPING_KINDS[item.purpose] for item in mappings):
            raise HTTPException(422, "Taxonomy mapping has an invalid or inactive concept kind.")

    async def _validate_publishable_contract(self, version: InterviewQuestionVersion) -> None:
        mappings = (await self.db.execute(select(QuestionVersionTaxonomyConcept).where(QuestionVersionTaxonomyConcept.question_version_id == version.id))).scalars().all()
        await self._validate_taxonomy_mappings(version.taxonomy_version, mappings)
        rubric_id = await self.db.scalar(select(QuestionVersionRubric.rubric_version_id).where(QuestionVersionRubric.question_version_id == version.id))
        if rubric_id is None:
            raise HTTPException(422, "Question version has no rubric version.")
        weights = await self.db.scalar(select(func.coalesce(func.sum(RubricCriterion.weight), 0)).where(RubricCriterion.rubric_version_id == rubric_id))
        if abs(Decimal(weights) - Decimal(1)) > _WEIGHT_TOLERANCE:
            raise HTTPException(422, "Rubric weights must total 1.0.")
        incomplete = await self.db.scalar(select(func.count()).select_from(RubricCriterion).where(RubricCriterion.rubric_version_id == rubric_id).where(select(func.count()).select_from(RubricAnchor).where(RubricAnchor.criterion_id == RubricCriterion.id).correlate(RubricCriterion).scalar_subquery() != 4))
        if incomplete:
            raise HTTPException(422, "Every criterion needs anchors 0-3.")
        unsupported = await self.db.scalar(select(func.count()).select_from(ExpectedPoint).where(ExpectedPoint.question_version_id == version.id, ExpectedPoint.importance == "CRITICAL").where(select(func.count()).select_from(ExpectedPointSource).join(KnowledgeSource, KnowledgeSource.id == ExpectedPointSource.source_id).where(ExpectedPointSource.expected_point_id == ExpectedPoint.id, KnowledgeSource.license_verified.is_(True), KnowledgeSource.commercial_use_status == "ALLOWED").correlate(ExpectedPoint).scalar_subquery() == 0))
        if unsupported:
            raise HTTPException(422, "Every CRITICAL expected point needs an allowed verified source.")
