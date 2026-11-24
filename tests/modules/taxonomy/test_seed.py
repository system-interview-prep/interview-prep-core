from src.modules.taxonomy.seed import _kind


def test_seed_maps_all_builtin_dimensions() -> None:
    assert _kind("domain") == "domain"
    assert _kind("occupation") == "occupation"
    assert _kind("specialization") == "competency"
    assert _kind("skill") == "skill"
