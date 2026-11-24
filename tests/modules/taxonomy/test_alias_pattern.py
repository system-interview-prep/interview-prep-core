"""List-only aliases ("ai", "monitoring") must never match prose, whatever their case."""

import pytest

from src.modules.taxonomy.skill_catalog import alias_pattern

_PROSE = (
    "Lead mobile design reviews, implementation, release and post-release monitoring",
    "Familiarity with AI-assisted development tools and code generation",
)


# The DB taxonomy lists each label as an alias too ("Monitoring", "AI").
@pytest.mark.parametrize("alias", ["monitoring", "Monitoring", "ai", "AI"])
@pytest.mark.parametrize("sentence", _PROSE)
def test_list_only_alias_ignores_prose_in_any_case(alias: str, sentence: str) -> None:
    assert not alias_pattern(alias).search(sentence)


@pytest.mark.parametrize("alias", ["monitoring", "Monitoring"])
def test_list_only_alias_still_matches_list_items(alias: str) -> None:
    assert alias_pattern(alias).search("- Monitoring")
    assert alias_pattern(alias).search("Python, Monitoring, Grafana")
