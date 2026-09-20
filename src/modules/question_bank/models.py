"""SQLAlchemy source-of-truth models for the reusable question bank."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class QuestionBankBase(DeclarativeBase):
    """Separate metadata keeps question-bank schema isolated from legacy tables."""


def _id() -> Mapped[UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class InterviewQuestion(QuestionBankBase):
    __tablename__ = "interview_questions"
    id: Mapped[UUID] = _id()
    stable_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    current_approved_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("interview_question_versions.id", use_alter=True, name="fk_question_current_version"),
        nullable=True,
    )
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = _now()
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InterviewQuestionVersion(QuestionBankBase):
    __tablename__ = "interview_question_versions"
    __table_args__ = (UniqueConstraint("question_id", "version", name="uq_question_version"),)
    id: Mapped[UUID] = _id()
    question_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_questions.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="1.0")
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="DRAFT")
    question_type: Mapped[str] = mapped_column(String(48), nullable=False)
    difficulty_band: Mapped[str] = mapped_column(String(32), nullable=False)
    expert_difficulty: Mapped[str | None] = mapped_column(String(128))
    canonical_locale: Mapped[str] = mapped_column(String(35), nullable=False)
    canonical_text: Mapped[str] = mapped_column(Text, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    thinking_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    soft_answer_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_answer_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    context_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    personalization_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    canonical_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = _now()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    change_summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class QuestionVersionTaxonomyConcept(QuestionBankBase):
    __tablename__ = "question_version_taxonomy_concepts"
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), primary_key=True
    )
    taxonomy_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    concept_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), primary_key=True)
    relevance: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)


class QuestionLocalization(QuestionBankBase):
    __tablename__ = "question_localizations"
    __table_args__ = (UniqueConstraint("question_version_id", "locale", name="uq_question_localization"),)
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(35), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="DRAFT")
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuestionVariant(QuestionBankBase):
    __tablename__ = "question_variants"
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(35), nullable=False)
    variant_type: Mapped[str] = mapped_column(String(32), nullable=False)
    variant_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="DRAFT")
    semantic_hash: Mapped[str | None] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(36))


class ExpectedPoint(QuestionBankBase):
    __tablename__ = "expected_points"
    __table_args__ = (UniqueConstraint("question_version_id", "stable_key", name="uq_expected_point_key"),)
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[str] = mapped_column(String(16), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)


class KnowledgeSource(QuestionBankBase):
    __tablename__ = "knowledge_sources"
    id: Mapped[UUID] = _id()
    stable_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(48), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    document_version: Mapped[str | None] = mapped_column(Text)
    section_reference: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    license_name: Mapped[str | None] = mapped_column(Text)
    license_url: Mapped[str | None] = mapped_column(Text)
    license_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    commercial_use_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="UNKNOWN")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ACTIVE")


class ExpectedPointSource(QuestionBankBase):
    __tablename__ = "expected_point_sources"
    expected_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("expected_points.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="RESTRICT"), primary_key=True
    )
    support_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(36))


class Rubric(QuestionBankBase):
    __tablename__ = "rubrics"
    id: Mapped[UUID] = _id()
    stable_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rubric_versions.id", use_alter=True, name="fk_rubric_current_version")
    )


class RubricVersion(QuestionBankBase):
    __tablename__ = "rubric_versions"
    __table_args__ = (UniqueConstraint("rubric_id", "version", name="uq_rubric_version"),)
    id: Mapped[UUID] = _id()
    rubric_id: Mapped[UUID] = mapped_column(ForeignKey("rubrics.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    score_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    score_max: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    minimum_coverage: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    aggregation_method: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregation_policy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = _now()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuestionVersionRubric(QuestionBankBase):
    __tablename__ = "question_version_rubrics"
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), primary_key=True
    )
    rubric_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("rubric_versions.id", ondelete="RESTRICT"), nullable=False
    )


class RubricCriterion(QuestionBankBase):
    __tablename__ = "rubric_criteria"
    __table_args__ = (UniqueConstraint("rubric_version_id", "stable_key", name="uq_rubric_criterion_key"),)
    id: Mapped[UUID] = _id()
    rubric_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("rubric_versions.id", ondelete="CASCADE"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)


class RubricAnchor(QuestionBankBase):
    __tablename__ = "rubric_anchors"
    __table_args__ = (CheckConstraint("level BETWEEN 0 AND 3", name="ck_rubric_anchor_level"),)
    criterion_id: Mapped[UUID] = mapped_column(
        ForeignKey("rubric_criteria.id", ondelete="CASCADE"), primary_key=True
    )
    level: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    positive_indicators: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    negative_indicators: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class QuestionReviewAssignment(QuestionBankBase):
    __tablename__ = "question_review_assignments"
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    review_type: Mapped[str] = mapped_column(String(32), nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(36))
    required_reviewer_role: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OPEN")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class QuestionReview(QuestionBankBase):
    __tablename__ = "question_reviews"
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[str] = mapped_column(String(36), nullable=False)
    review_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scores: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    checklist: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    findings: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = _now()


class QuestionApproval(QuestionBankBase):
    __tablename__ = "question_approvals"
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    approver_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approval_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_at: Mapped[datetime] = _now()


class FollowUpPolicy(QuestionBankBase):
    __tablename__ = "follow_up_policies"
    id: Mapped[UUID] = _id()
    stable_key: Mapped[str] = mapped_column(String(180), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_criterion_key: Mapped[str] = mapped_column(String(120), nullable=False)
    trigger_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    constraints: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="DRAFT")
    __table_args__ = (UniqueConstraint("stable_key", "version", name="uq_follow_up_policy_version"),)


class QuestionFollowUp(QuestionBankBase):
    __tablename__ = "question_follow_ups"
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    follow_up_policy_id: Mapped[UUID] = mapped_column(
        ForeignKey("follow_up_policies.id", ondelete="RESTRICT"), nullable=False
    )
    target_criterion_id: Mapped[UUID] = mapped_column(
        ForeignKey("rubric_criteria.id", ondelete="RESTRICT"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(35), nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_text: Mapped[str] = mapped_column(Text, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class QuestionRelation(QuestionBankBase):
    __tablename__ = "question_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_question_version_id",
            "target_question_version_id",
            "relation_type",
            name="uq_question_relation",
        ),
    )
    id: Mapped[UUID] = _id()
    source_question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    target_question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="PROPOSED")
    generation_method: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[str | None] = mapped_column(String(32))
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class QuestionEmbedding(QuestionBankBase):
    __tablename__ = "question_embeddings"
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_purpose: Mapped[str] = mapped_column(String(32), primary_key=True)
    embedding_model: Mapped[str] = mapped_column(String(120), primary_key=True)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(VECTOR(1024), nullable=False)
    created_at: Mapped[datetime] = _now()


class ClassificationProposal(QuestionBankBase):
    __tablename__ = "classification_proposals"
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="CASCADE"), nullable=False
    )
    classifier_type: Mapped[str] = mapped_column(String(32), nullable=False)
    classifier_model: Mapped[str | None] = mapped_column(String(120))
    taxonomy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    proposal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="PROPOSED")
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QuestionFeedbackReport(QuestionBankBase):
    __tablename__ = "question_feedback_reports"
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    reporter_id: Mapped[str | None] = mapped_column(String(36))
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OPEN")
    triaged_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = _now()


class QuestionCalibration(QuestionBankBase):
    __tablename__ = "question_calibrations"
    __table_args__ = (
        UniqueConstraint(
            "question_version_id", "calibration_version", name="uq_question_calibration_version"
        ),
    )
    id: Mapped[UUID] = _id()
    question_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("interview_question_versions.id", ondelete="RESTRICT"), nullable=False
    )
    calibration_version: Mapped[str] = mapped_column(String(32), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mean_score: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    score_stddev: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    mean_answer_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    timeout_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    skip_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    clarification_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    empirical_difficulty: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    discrimination: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    human_ai_agreement: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    inter_rater_agreement: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    slice_metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    calculated_at: Mapped[datetime] = _now()


class QuestionImport(QuestionBankBase):
    __tablename__ = "question_imports"
    id: Mapped[UUID] = _id()
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_format: Mapped[str] = mapped_column(String(8), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="1")
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    warning_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = _now()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commit_idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)


class QuestionImportRow(QuestionBankBase):
    __tablename__ = "question_import_rows"
    __table_args__ = (UniqueConstraint("import_id", "row_number", name="uq_question_import_row"),)
    id: Mapped[UUID] = _id()
    import_id: Mapped[UUID] = mapped_column(
        ForeignKey("question_imports.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    normalized_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation_errors: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    validation_warnings: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    override_reason: Mapped[str | None] = mapped_column(Text)
    overridden_by: Mapped[str | None] = mapped_column(String(36))
    draft_question_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    draft_question_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    updated_at: Mapped[datetime] = _now()
