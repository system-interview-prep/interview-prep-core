"""The interviewer's playbook: lessons from past interviews, approved by a person.

"AI tích luỹ skill phỏng vấn" (ADVISOR_FEEDBACK_RESEARCH_2026-10-26.md, section 3).
A reflection job proposes entries (PROPOSED) from flagged and reviewed turns;
an admin approves them (ACTIVE) and only ACTIVE entries reach the engine's
prompts (interviews/core/playbook.py). ``helpful`` / ``harmful`` count the
reviewed turns that ran with the entry and passed or failed.

Revision ID: 20261026_0037
Revises: 20261020_0036
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261026_0037"
down_revision: str | None = "20261020_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS interviewer_playbook (
          id VARCHAR(36) PRIMARY KEY,
          step VARCHAR(24) NOT NULL DEFAULT 'all'
            CHECK (step IN ('all', 'evaluate_answer', 'probe', 'clarify')),
          skill VARCHAR(160),
          text TEXT NOT NULL,
          status VARCHAR(16) NOT NULL DEFAULT 'PROPOSED'
            CHECK (status IN ('PROPOSED', 'ACTIVE', 'RETIRED', 'REJECTED')),
          origin VARCHAR(16) NOT NULL DEFAULT 'reflection' CHECK (origin IN ('reflection', 'manual')),
          replaces_id VARCHAR(36),
          helpful INTEGER NOT NULL DEFAULT 0,
          harmful INTEGER NOT NULL DEFAULT 0,
          source JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_by VARCHAR(36),
          decided_by VARCHAR(36),
          decided_at TIMESTAMPTZ,
          note TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_interviewer_playbook_status ON interviewer_playbook(status, step);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS interviewer_playbook")
