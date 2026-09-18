"""Seed initial job categories into taxonomy_concepts."""

from alembic import op

revision = "20260918_0009"
down_revision = "20260909_0008"
branch_labels = None
depends_on = None

CATEGORIES = [
    ("technology", "Technology", ["cong nghe thong tin", "it", "tech"]),
    ("technology.software-engineering", "Software Engineering", ["ky thuat phan mem", "lap trinh"]),
    ("technology.software-engineering.backend", "Backend Engineering", ["backend developer", "lap trinh backend", "server"]),
    ("technology.software-engineering.frontend", "Frontend Engineering", ["frontend developer", "lap trinh frontend", "ui"]),
    ("technology.software-engineering.fullstack", "Fullstack Engineering", ["fullstack developer", "lap trinh fullstack"]),
    ("technology.cloud-devops", "Cloud & DevOps", ["devops engineer", "cloud architect", "sre"]),
    ("technology.artificial-intelligence", "Artificial Intelligence & Data", ["ai engineer", "data scientist", "machine learning"]),
    ("technology.mobile-app", "Mobile App Development", ["mobile developer", "ios", "android", "flutter", "react native"]),
    ("technology.qa-qc", "Quality Assurance & Testing", ["qa engineer", "qc", "tester", "kiem thu phan mem"]),
    ("technology.ui-ux", "UI/UX Design", ["product designer", "ui designer", "ux designer", "thiet ke giao dien"]),
    ("technology.cybersecurity", "Cybersecurity & InfoSec", ["security engineer", "an ninh mang", "bao mat thong tin"]),
    ("technology.game-development", "Game Development", ["game developer", "unity", "unreal"]),
    ("technology.embedded-iot", "Embedded Systems & IoT", ["nhung", "iot", "firmware", "hardware"]),
    ("business.product-management", "Product Management", ["product manager", "quan ly san pham", "pm"]),
    ("business.project-management", "Project Management", ["project manager", "quan ly du an", "scrum master"]),
    ("business.human-resources", "Human Resources", ["hr", "nhan su", "tuyen dung", "recruiter"]),
    ("business.marketing", "Marketing & Communications", ["marketing", "truyen thong", "digital marketing"]),
    ("business.sales", "Sales & Business Development", ["sales", "kinh doanh", "account executive"]),
]


def upgrade() -> None:
    for concept_id, label, aliases in CATEGORIES:
        op.execute(f"""
            INSERT INTO taxonomy_concepts (taxonomy_version, concept_id, label, kind, is_active)
            VALUES ('internal-2026.2', '{concept_id}', '{label}', 'job_category', true)
            ON CONFLICT (taxonomy_version, concept_id) DO UPDATE 
            SET label = EXCLUDED.label, kind = EXCLUDED.kind, is_active = true;
        """)
        for alias in [label, *aliases]:
            clean_alias = alias.replace("'", "''")
            op.execute(f"""
                INSERT INTO taxonomy_aliases (taxonomy_version, concept_id, alias)
                VALUES ('internal-2026.2', '{concept_id}', '{clean_alias}')
                ON CONFLICT (taxonomy_version, concept_id, alias) DO NOTHING;
            """)


def downgrade() -> None:
    concept_ids = "', '".join([c[0] for c in CATEGORIES])
    op.execute(f"""
        DELETE FROM taxonomy_aliases WHERE taxonomy_version = 'internal-2026.2' AND concept_id IN ('{concept_ids}');
        DELETE FROM taxonomy_concepts WHERE taxonomy_version = 'internal-2026.2' AND concept_id IN ('{concept_ids}');
    """)
