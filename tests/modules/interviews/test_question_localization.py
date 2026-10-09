"""A vi-VN interview asks every bank question in Vietnamese, not only the presets."""

import pytest

from src.modules.interviews.planning import question_selector
from src.seeds.question_bank_seed import _CONCEPT_FIXTURES
from src.seeds.question_bank_seed_vi import QUESTION_TEXT_VI
from tests.modules.interviews.test_question_fallback import _db, rollback_db  # noqa: F401
from tests.modules.interviews.test_question_selector import candidate


def test_every_english_seed_question_has_vietnamese_wording() -> None:
    english = {
        q["stable_key"]
        for fixture in _CONCEPT_FIXTURES
        for q in fixture["questions"]
        if q.get("locale", "en-US") != "vi-VN"
    }
    assert english - QUESTION_TEXT_VI.keys() == set()
    assert QUESTION_TEXT_VI.keys() - english == set()


def test_localized_question_ranks_like_a_native_one() -> None:
    def rank(item):
        return question_selector._candidate_rank(item, difficulty="intermediate", locale="vi-VN")

    english = candidate(question_version_id="en", canonical_locale="en-US", difficulty_band="intermediate")
    localized = candidate(
        question_version_id="loc",
        canonical_locale="en-US",
        difficulty_band="intermediate",
        localized_from="en-US",
    )
    assert rank(localized)[0] == 0
    assert rank(english)[0] > rank(localized)[0]


@_db
@pytest.mark.asyncio
async def test_vi_session_gets_the_approved_vietnamese_text(rollback_db) -> None:  # noqa: F811
    target = {
        "taxonomyVersion": "internal-2026.1",
        "conceptId": "skill-javascript",
        "label": "JavaScript",
        "rationale": {"source": "job_requirement"},
    }
    vi = await question_selector._load_candidates(
        rollback_db, target=target, locale="vi-VN", difficulty="unspecified"
    )
    en = await question_selector._load_candidates(
        rollback_db, target=target, locale="en-US", difficulty="unspecified"
    )

    assert vi and all(item.localized_from == "en-US" for item in vi)
    assert {item.question_text for item in vi} <= set(QUESTION_TEXT_VI.values())
    # The English session still gets the canonical text.
    assert all(item.localized_from is None for item in en)
    assert {item.question_text for item in en}.isdisjoint(QUESTION_TEXT_VI.values())
    snapshot = question_selector._snapshot(vi[0], target=target, locale="vi-VN", selection_rank=2)
    assert snapshot["localizedFrom"] == "en-US" and snapshot["canonicalLocale"] == "en-US"
