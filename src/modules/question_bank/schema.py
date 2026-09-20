"""Question-bank schema bootstrap for the intentional clean-slate build."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.modules.question_bank.models import QuestionBankBase


async def create_question_bank_schema(engine: AsyncEngine) -> None:
    """Create the SQLAlchemy-owned question-bank schema and safe active-bank view.

    The project is intentionally rebuilding from an empty database, so this is
    bootstrap code rather than an Alembic compatibility migration.
    """
    async with engine.begin() as connection:
        await connection.run_sync(QuestionBankBase.metadata.create_all)
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_question_version_primary_competency "
                "ON question_version_taxonomy_concepts (question_version_id) "
                "WHERE purpose = 'PRIMARY_COMPETENCY'"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_question_version_status_difficulty "
                "ON interview_question_versions (status, difficulty_band)"
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_question_taxonomy_concept "
                "ON question_version_taxonomy_concepts (taxonomy_version, concept_id, question_version_id)"
            )
        )
        await connection.execute(
            text(
                "CREATE OR REPLACE VIEW active_question_bank AS "
                "SELECT q.id AS question_id, q.stable_key, qv.id AS question_version_id, "
                "qv.version, qv.question_type, qv.difficulty_band, qv.canonical_locale, "
                "qv.canonical_text, qv.objective, qv.soft_answer_seconds, qv.hard_answer_seconds "
                "FROM interview_questions q "
                "JOIN interview_question_versions qv ON qv.id = q.current_approved_version_id "
                "WHERE qv.status IN ('APPROVED', 'CALIBRATED') AND q.retired_at IS NULL"
            )
        )
