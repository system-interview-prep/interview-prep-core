"""Make taxonomy the sole classification source for job descriptions."""

from alembic import op

revision = "20260909_0008"
down_revision = "20260909_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE job_descriptions ADD COLUMN primary_taxonomy_version TEXT;
    ALTER TABLE job_descriptions ADD COLUMN primary_taxonomy_concept_id TEXT;
    INSERT INTO taxonomy_concepts (taxonomy_version, concept_id, label, kind)
    SELECT 'internal-2026.2', 'occupation.' || substr(md5(name), 1, 16), name, 'occupation'
    FROM job_categories
    ON CONFLICT (taxonomy_version, concept_id) DO NOTHING;
    INSERT INTO taxonomy_aliases (taxonomy_version, concept_id, alias)
    SELECT 'internal-2026.2', 'occupation.' || substr(md5(name), 1, 16), name
    FROM job_categories
    ON CONFLICT DO NOTHING;
    UPDATE job_descriptions jd SET
      primary_taxonomy_version = 'internal-2026.2',
      primary_taxonomy_concept_id = 'occupation.' || substr(md5(jc.name), 1, 16)
    FROM job_categories jc WHERE jd.category_id = jc.id;
    ALTER TABLE job_descriptions DROP CONSTRAINT job_profiles_category_id_fkey;
    DROP INDEX ix_job_descriptions_category_created;
    ALTER TABLE job_descriptions DROP COLUMN category_id;
    DROP TABLE job_categories;
    CREATE INDEX ix_job_descriptions_taxonomy_created ON job_descriptions (primary_taxonomy_version, primary_taxonomy_concept_id, created_at);
    """)


def downgrade() -> None:
    raise RuntimeError("Category removal is intentionally irreversible; restore from a database backup if needed.")
