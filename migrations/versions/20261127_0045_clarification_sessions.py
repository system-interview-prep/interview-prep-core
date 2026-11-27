"""Keep the clarification questions asked and the answers given.

Both lived only in React state, so a reload threw away every answer the
candidate had typed and sent the page back through Jev and the question model
for a fresh set of questions - new wording for the same requirements, at the
cost of two more LLM calls per reload.  Nothing was written down, so there was
also nothing to measure: how many questions got answered, and what those
answers changed, could not be read back afterwards.

One row per (user, cv, job, requirement).  The question is stored when it is
generated and the answer is merged into the same row when it arrives, so a
reload restores exactly what the candidate saw and said.

Revision ID: 20261127_0045
Revises: 20261124_0044
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261127_0045"
down_revision: str | None = "20261124_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS clarification_sessions (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id VARCHAR(36) NOT NULL,
          cv_id VARCHAR(36) NOT NULL,
          job_id VARCHAR(36) NOT NULL,
          requirement_id VARCHAR(128) NOT NULL,
          -- As asked. Regenerating different wording on reload is what made the
          -- step feel unreliable, so the asked text is the stored text.
          question_text TEXT NOT NULL,
          missing_dimension VARCHAR(32) NOT NULL,
          confidence DOUBLE PRECISION NOT NULL
            CHECK (confidence >= 0 AND confidence <= 1),
          semantic_alignment_score DOUBLE PRECISION
            CHECK (semantic_alignment_score IS NULL
                   OR (semantic_alignment_score >= 0 AND semantic_alignment_score <= 1)),
          evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
          -- Null until the candidate answers; the rest of the answer columns
          -- are filled in the same write.
          answer_text TEXT,
          answered_at TIMESTAMPTZ,
          outcome_status VARCHAR(16)
            CHECK (outcome_status IS NULL
                   OR outcome_status IN ('met', 'not_met', 'unknown', 'not_applicable')),
          outcome_reason_code VARCHAR(128),
          outcome_explanation TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CHECK ((answer_text IS NULL) = (answered_at IS NULL))
        );

        -- One live question per requirement in a given CV-to-JD pairing: the
        -- key a reload reads back, and the key an answer merges onto.
        CREATE UNIQUE INDEX IF NOT EXISTS ux_clarification_sessions_scope
          ON clarification_sessions(user_id, cv_id, job_id, requirement_id);
        CREATE INDEX IF NOT EXISTS ix_clarification_sessions_lookup
          ON clarification_sessions(user_id, cv_id, job_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS clarification_sessions")
