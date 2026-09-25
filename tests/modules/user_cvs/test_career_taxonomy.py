from src.modules.job_descriptions.domain.schemas import JobRequirement
from src.modules.taxonomy.facade import classify_career
from src.modules.user_cvs.domain.schemas import TaxonomyRef
from src.modules.user_cvs.parsing.domain.classification import (
    TAXONOMY_VERSION,
    career_taxonomy,
)


def test_career_taxonomy_has_stable_hierarchy_for_frontend() -> None:
    nodes = {node.code: node for node in career_taxonomy()}

    assert TAXONOMY_VERSION == "internal-career-2026.1"
    assert nodes["technology"].parent_code is None
    assert nodes["technology.software-engineering"].parent_code == "technology"
    assert (
        nodes["technology.software-engineering.backend"].parent_code
        == "technology.software-engineering"
    )
    assert nodes["technology"].as_dict()["parentCode"] is None


def test_jd_and_cv_use_the_same_career_taxonomy_version() -> None:
    requirement = JobRequirement(
        requirementId="req-python", kind="skill", priority="must_have", rawLabel="FastAPI",
        concept=TaxonomyRef(
            conceptId="skill-fastapi", scheme="internal", taxonomyVersion="skills-v1", label="FastAPI"
        ),
    )
    classification = classify_career(
        {requirement.concept.concept_id: requirement.evidence_refs},
        [],
        minimum_skill_signals=1,
        include_ancestors=False,
    )[0]
    assert classification.code == "technology.software-engineering.backend"
    assert classification.taxonomy_version == TAXONOMY_VERSION


def test_ai_title_and_ai_skills_take_primary_over_backend_implementation_stack() -> None:
    classifications = classify_career(
        {
            "skill-natural-language-processing": ["cv-nlp"],
            "skill-generative-ai": ["cv-genai"],
            "skill-large-language-models": ["cv-llm"],
            "skill-fastapi": ["cv-fastapi"],
            "skill-postgresql": ["cv-postgres"],
        },
        [("AI Engineer", ["cv-title"])],
        minimum_skill_signals=2,
        include_ancestors=False,
    )

    assert classifications[0].code == "technology.artificial-intelligence"
    assert classifications[0].is_primary is True


def test_backend_title_and_backend_skills_remain_backend_primary() -> None:
    classifications = classify_career(
        {
            "skill-spring-boot": ["cv-spring"],
            "skill-postgresql": ["cv-postgres"],
            "skill-fastapi": ["cv-fastapi"],
        },
        [("Backend Engineer", ["cv-title"])],
        minimum_skill_signals=2,
        include_ancestors=False,
    )

    assert classifications[0].code == "technology.software-engineering.backend"
