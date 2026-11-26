"""Anticipation must help, and must never be able to hurt.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §7 and the V2 correction brief §5. Two
properties carry the whole feature: a pending preview never delays the turn,
and a preview that does not match the final state is never reused.
"""

from __future__ import annotations

import asyncio

import pytest

from src.modules.interviews.realtime.anticipation import (
    AnticipationEngine,
    preview_key,
    validate_preview,
)


def _key(transcript: str = "em dùng redis làm cache", **over):
    base = dict(
        session_id="s1", turn_id="t1", question_id="q1",
        transcript=transcript, state_version="7", rubric_version="r1", policy_version="p1",
    )
    base.update(over)
    return preview_key(**base)


@pytest.mark.asyncio
async def test_a_preview_still_running_does_not_delay_the_turn() -> None:
    """The §7 rule, as a property: take() is synchronous and returns nothing.

    The runtime used to `await asyncio.wait_for(preview, timeout=1.0)` here,
    so a slow speculation added up to a second to every answer.
    """
    started = asyncio.Event()

    async def never_finishes(_transcript, _key):
        started.set()
        await asyncio.sleep(30)

    engine = AnticipationEngine(never_finishes, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache", _key())
    await asyncio.wait_for(started.wait(), timeout=1.0)

    loop = asyncio.get_running_loop()
    before = loop.time()
    result = engine.take(_key())
    elapsed = loop.time() - before

    assert result is None
    assert elapsed < 0.05, "take() waited for the in-flight preview"
    await engine.aclose()


@pytest.mark.asyncio
async def test_a_finished_matching_preview_is_reused() -> None:
    async def infer(_transcript, _key):
        return "preview-token"

    engine = AnticipationEngine(infer, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache", _key())
    await asyncio.sleep(0.05)

    assert engine.take(_key()) == "preview-token"
    assert engine.stats.as_dict()["reused"] == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"transcript": "em dùng redis làm cache và memcached"},   # candidate kept talking
        {"turn_id": "t2"},
        {"question_id": "q2"},
        {"state_version": "8"},
        {"rubric_version": "r2"},
        {"policy_version": "p2"},
        {"session_id": "s2"},
    ],
)
@pytest.mark.asyncio
async def test_a_preview_is_discarded_when_anything_it_assumed_changed(changed: dict) -> None:
    """§7: reuse only when the final transcript and every version still match."""
    async def infer(_transcript, _key):
        return "preview-token"

    engine = AnticipationEngine(infer, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache", _key())
    await asyncio.sleep(0.05)

    assert engine.take(_key(**changed)) is None
    assert engine.stats.as_dict()["reasons"].get("stale") == 1


@pytest.mark.asyncio
async def test_speculation_is_off_unless_enabled() -> None:
    calls = []

    async def infer(transcript, key):
        calls.append(transcript)
        return "x"

    engine = AnticipationEngine(infer, enabled=False, debounce_seconds=0)
    engine.observe("em dùng redis làm cache", _key())
    await asyncio.sleep(0.05)

    assert calls == []
    assert engine.take(_key()) is None


@pytest.mark.asyncio
async def test_a_turn_cannot_run_up_unbounded_speculative_cost() -> None:
    """§7 caps calls started, because cancelling does not stop provider billing."""
    calls = []

    async def infer(transcript, key):
        calls.append(transcript)
        return None

    engine = AnticipationEngine(infer, enabled=True, debounce_seconds=0, max_calls_per_turn=3)
    for i in range(10):
        engine.observe(f"em dùng redis làm cache phiên bản {i}", _key())
        await asyncio.sleep(0.02)

    assert len(calls) <= 3
    engine.reset_turn()
    engine.observe("câu trả lời của lượt tiếp theo dài hơn", _key(turn_id="t2"))
    await asyncio.sleep(0.05)
    assert len(calls) == 4, "the cap must reset with the turn"
    await engine.aclose()


@pytest.mark.asyncio
async def test_a_short_partial_transcript_is_not_worth_a_call() -> None:
    calls = []

    async def infer(transcript, key):
        calls.append(transcript)
        return "x"

    engine = AnticipationEngine(infer, enabled=True, debounce_seconds=0, min_words=4)
    engine.observe("em", _key("em"))
    await asyncio.sleep(0.05)
    assert calls == []


def test_validate_names_the_fields_that_went_stale() -> None:
    rejection = validate_preview(
        preview="token", computed_for=_key(), final=_key(turn_id="t2", state_version="9")
    )
    assert rejection is not None and rejection.reason == "stale"
    assert set(rejection.fields) == {"turn_id", "state_version"}
    # A rejection is falsey, so `if validate_preview(...)` cannot read as reusable.
    assert not rejection


def test_a_missing_preview_is_not_an_error() -> None:
    assert validate_preview(preview=None, computed_for=None, final=_key()).reason == "no_preview"
