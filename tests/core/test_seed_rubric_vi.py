"""The generic seed rubric is Vietnamese, and existing databases are brought along safely."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.seeds.question_bank_rubric_vi import (
    SEED_ANCHORS,
    SEED_CRITERIA,
    localize_seed_rubrics,
    seed_anchor_records,
)
from src.seeds.question_bank_seed import _SEED_AUTHOR, _build_rubric_records
from tests.modules.interviews.test_question_fallback import _db, rollback_db  # noqa: F401


def test_seed_rubric_keeps_its_shape_and_is_written_in_vietnamese() -> None:
    records = _build_rubric_records("some-question", uuid.uuid4())

    assert [item["stable_key"] for item in records["criteria"]] == ["relevance", "technical_depth", "clarity"]
    assert sum(item["weight"] for item in records["criteria"]) == Decimal("1.00")
    assert [item["critical"] for item in records["criteria"]] == [False, True, False]
    assert records["criteria"][0]["name"] == "Đúng trọng tâm"
    for key, anchors in records["anchors_by_criterion"].items():
        assert [anchor["level"] for anchor in anchors] == [0, 1, 2, 3], key
    # Translating must not change the ids existing rows were inserted under.
    again = _build_rubric_records("some-question", uuid.uuid4())
    assert [item["id"] for item in again["criteria"]] == [item["id"] for item in records["criteria"]]


def test_every_translated_text_differs_from_the_english_it_replaces() -> None:
    for criterion in SEED_CRITERIA:
        assert criterion["name"] != criterion["en_name"]
        assert criterion["description"] != criterion["en_description"]
    assert set(SEED_ANCHORS) == {criterion["stable_key"] for criterion in SEED_CRITERIA}
    assert set(seed_anchor_records()) == set(SEED_ANCHORS)
    for anchors in SEED_ANCHORS.values():
        assert len(anchors) == 4
        assert all(english != description for english, description, _pos, _neg in anchors)


_SET_ANCHOR = "UPDATE rubric_anchors SET description = :text WHERE criterion_id = :id AND level = :level"


async def _criterion_ids(db, rubric_version_id) -> dict[str, object]:
    rows = await db.execute(
        text("SELECT stable_key, id FROM rubric_criteria WHERE rubric_version_id = :id"),
        {"id": rubric_version_id},
    )
    return {row.stable_key: row.id for row in rows.all()}


@_db
@pytest.mark.asyncio
async def test_localizing_translates_untouched_seed_text_once_and_spares_edits(rollback_db) -> None:  # noqa: F811
    db = rollback_db
    rubric_version_id = await db.scalar(
        text(
            "SELECT rv.id FROM rubric_versions rv WHERE rv.created_by = :author AND "
            "(SELECT count(*) FROM rubric_criteria c WHERE c.rubric_version_id = rv.id "
            "AND c.stable_key = ANY(:keys)) = 3 LIMIT 1"
        ),
        {"author": _SEED_AUTHOR, "keys": [item["stable_key"] for item in SEED_CRITERIA]},
    )
    if rubric_version_id is None:
        pytest.skip("no seeded rubric in this database")
    ids = await _criterion_ids(db, rubric_version_id)

    # Put one seeded rubric back into the wording older databases hold.
    for criterion in SEED_CRITERIA:
        key = criterion["stable_key"]
        await db.execute(
            text("UPDATE rubric_criteria SET name = :name, description = :description WHERE id = :id"),
            {"name": criterion["en_name"], "description": criterion["en_description"], "id": ids[key]},
        )
        for level, (english, _description, _pos, _neg) in enumerate(SEED_ANCHORS[key]):
            await db.execute(
                text(_SET_ANCHOR), {"text": english, "id": ids[key], "level": level}
            )
    # Somebody rewrote one criterion and one anchor by hand: both must survive.
    await db.execute(
        text("UPDATE rubric_criteria SET description = 'Edited by an admin.' WHERE id = :id"),
        {"id": ids["clarity"]},
    )
    await db.execute(text(_SET_ANCHOR), {"text": "Edited anchor.", "id": ids["relevance"], "level": 0})

    first = await localize_seed_rubrics(db)
    second = await localize_seed_rubrics(db)

    # Two of three criteria and eleven of twelve anchors were still untouched.
    assert first == {"criteria": 2, "anchors": 11}
    assert second == {"criteria": 0, "anchors": 0}

    names = dict(
        (
            await db.execute(
                text("SELECT stable_key, name FROM rubric_criteria WHERE rubric_version_id = :id"),
                {"id": rubric_version_id},
            )
        ).all()
    )
    assert names["relevance"] == "Đúng trọng tâm" and names["technical_depth"] == "Chiều sâu kỹ thuật"
    assert names["clarity"] == "Clarity"
    edited = await db.scalar(
        text("SELECT description FROM rubric_criteria WHERE id = :id"), {"id": ids["clarity"]}
    )
    assert edited == "Edited by an admin."
    anchors = [
        row.description
        for row in (
            await db.execute(
                text("SELECT description FROM rubric_anchors WHERE criterion_id = :id ORDER BY level"),
                {"id": ids["relevance"]},
            )
        ).all()
    ]
    assert anchors == ["Edited anchor.", *[item[1] for item in SEED_ANCHORS["relevance"][1:]]]
