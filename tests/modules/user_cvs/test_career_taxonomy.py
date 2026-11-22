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
    assert nodes["technology.ai-engineering"].label == "AI Engineering"
    # Two nodes used to declare this code; the richer one (it also covers
    # data analysts and BI) survived the de-duplication, and it is the label
    # already seeded in taxonomy_concepts and stored in parsed CVs and JDs.
    assert nodes["technology.data-engineering"].label == "Data Engineering & Analytics"
    assert nodes["technology.data-science"].label == "Data Science"
    assert nodes["technology.cybersecurity"].label == "Cybersecurity"
    assert nodes["technology.qa-automation"].label == "QA & Test Automation"
    assert nodes["technology.ux-ui-design"].label == "UX/UI Design"
    assert nodes["technology.business-analysis"].label == "Business Analysis"
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

    assert classifications[0].code == "technology.ai-engineering"
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


def test_frontend_skills_outrank_a_smaller_backend_stack() -> None:
    """A frontend CV must not be classified backend.

    Both readings used to exceed the 0.95 confidence cap, so they tied and the
    tie fell through to declaration order in _RULES, where backend comes first.
    Ranking happens on the uncapped score now, so the stronger evidence wins.
    """
    classifications = classify_career(
        {
            "skill-javascript": ["cv-js"],
            "skill-typescript": ["cv-ts"],
            "skill-html": ["cv-html"],
            "skill-css": ["cv-css"],
            "skill-react": ["cv-react"],
            "skill-vue": ["cv-vue"],
            "skill-php": ["cv-php"],
            "skill-laravel": ["cv-laravel"],
            "skill-mysql": ["cv-mysql"],
            "skill-rest-api": ["cv-rest"],
        },
        [("Frontend Developer", ["cv-title"])],
        minimum_skill_signals=2,
        include_ancestors=False,
    )

    assert classifications[0].code == "technology.software-engineering.frontend"
    assert classifications[0].is_primary is True
    # The cap still applies to what is reported.
    assert classifications[0].confidence == 0.95


def test_each_specialization_is_classified_at_most_once() -> None:
    """Duplicate rules used to emit one code twice, under two different labels."""
    for title in ("Data Engineer", "Security Engineer"):
        codes = [
            item.code
            for item in classify_career(
                {}, [(title, ["cv-title"])], minimum_skill_signals=2, include_ancestors=False
            )
        ]
        assert len(codes) == len(set(codes)), f"{title} -> {codes}"
