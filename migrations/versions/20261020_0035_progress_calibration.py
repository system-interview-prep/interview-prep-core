"""Grader version on evaluations; measured noise for the progress page.

``interview_evaluations.grader_version``: the grading model and prompt version
that produced the scores (``<llm_model>|<GRADER_PROMPT_VERSION>``). A provider
or prompt change can shift every score at once, so the progress page only
reads a trend within one grader version.

``progress_calibration``: one row per run of ``scripts/progress_calibration.py``.
It holds the measured noise the progress page uses instead of fixed guesses:
how much the grader varies on the same answer, how much one skill's score
varies between two close sessions of the same person, the spread between
people, and the score shift per practice tier. The newest row is used.

Revision ID: 20261020_0035
Revises: 20261020_0034
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261020_0035"
down_revision: str | None = "20261020_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE interview_evaluations ADD COLUMN IF NOT EXISTS grader_version VARCHAR(160);

        CREATE TABLE IF NOT EXISTS progress_calibration (
          id BIGSERIAL PRIMARY KEY,
          measured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          grader_version VARCHAR(160),
          grading_sd DOUBLE PRECISION,
          typical_error DOUBLE PRECISION,
          between_sd DOUBLE PRECISION,
          tier_offsets JSONB NOT NULL DEFAULT '{}'::jsonb,
          sample JSONB NOT NULL DEFAULT '{}'::jsonb,
          notes TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_progress_calibration_measured ON progress_calibration(measured_at DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS progress_calibration; "
        "ALTER TABLE interview_evaluations DROP COLUMN IF EXISTS grader_version;"
    )
