"""Add a versioned taxonomy shared by CV and job-description parsing."""

from alembic import op

revision = "20260909_0006"
down_revision = "20260908_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE taxonomy_versions (
      version TEXT PRIMARY KEY, priority INTEGER NOT NULL DEFAULT 0,
      is_active BOOLEAN NOT NULL DEFAULT true, published_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE taxonomy_concepts (
      taxonomy_version TEXT NOT NULL REFERENCES taxonomy_versions(version) ON DELETE CASCADE,
      concept_id TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'skill',
      is_active BOOLEAN NOT NULL DEFAULT true, PRIMARY KEY (taxonomy_version, concept_id)
    );
    CREATE TABLE taxonomy_aliases (
      taxonomy_version TEXT NOT NULL, concept_id TEXT NOT NULL, alias TEXT NOT NULL,
      PRIMARY KEY (taxonomy_version, concept_id, alias),
      FOREIGN KEY (taxonomy_version, concept_id) REFERENCES taxonomy_concepts(taxonomy_version, concept_id) ON DELETE CASCADE
    );
    CREATE INDEX ix_taxonomy_versions_active_priority ON taxonomy_versions (is_active, priority DESC, published_at DESC);
    CREATE INDEX ix_taxonomy_aliases_lookup ON taxonomy_aliases (taxonomy_version, alias);
    """)
    op.execute("""
    INSERT INTO taxonomy_versions (version, priority) VALUES ('internal-2026.2', 1);
    INSERT INTO taxonomy_concepts (taxonomy_version, concept_id, label) VALUES
      ('internal-2026.2','skill-python','Python'), ('internal-2026.2','skill-java','Java'),
      ('internal-2026.2','skill-spring-boot','Spring Boot'), ('internal-2026.2','skill-javascript','JavaScript'),
      ('internal-2026.2','skill-typescript','TypeScript'), ('internal-2026.2','skill-react','React'),
      ('internal-2026.2','skill-fastapi','FastAPI'), ('internal-2026.2','skill-postgresql','PostgreSQL'),
      ('internal-2026.2','skill-docker','Docker'), ('internal-2026.2','skill-kubernetes','Kubernetes'),
      ('internal-2026.2','skill-aws','AWS'), ('internal-2026.2','skill-git','Git'),
      ('internal-2026.2','skill-json','JSON'), ('internal-2026.2','skill-sql','SQL');
    INSERT INTO taxonomy_aliases (taxonomy_version, concept_id, alias)
      SELECT taxonomy_version, concept_id, label FROM taxonomy_concepts;
    INSERT INTO taxonomy_aliases VALUES
      ('internal-2026.2','skill-spring-boot','springboot'), ('internal-2026.2','skill-javascript','js'),
      ('internal-2026.2','skill-react','reactjs'), ('internal-2026.2','skill-react','react.js'),
      ('internal-2026.2','skill-postgresql','postgres'), ('internal-2026.2','skill-kubernetes','k8s'),
      ('internal-2026.2','skill-aws','amazon web services');
    """)


def downgrade() -> None:
    op.execute("DROP TABLE taxonomy_aliases; DROP TABLE taxonomy_concepts; DROP TABLE taxonomy_versions;")
