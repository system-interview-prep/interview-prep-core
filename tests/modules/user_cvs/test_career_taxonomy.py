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
