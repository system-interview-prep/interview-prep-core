from src.modules.user_cvs.parsing.domain.classification import (
    TAXONOMY_VERSION,
    career_taxonomy,
)
from src.modules.taxonomy.facade import classify_career
from src.modules.job_descriptions.domain.schemas import JobRequirement
from src.modules.user_cvs.domain.schemas import TaxonomyRef


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
