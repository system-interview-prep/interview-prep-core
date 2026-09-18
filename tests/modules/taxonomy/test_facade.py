from src.modules.taxonomy.facade import TAXONOMY_VERSION, career_taxonomy


def test_public_taxonomy_facade_exposes_versioned_catalog() -> None:
    assert TAXONOMY_VERSION == "internal-career-2026.1"
    assert any(item.code == "technology" for item in career_taxonomy())
