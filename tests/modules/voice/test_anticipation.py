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
    PreviewKey,
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


@pytest.mark.asyncio
async def test_speculation_cannot_outlive_a_cancel() -> None:
    """§5: speculative tasks must be cancellable and must not leak.

    A turn that ends while a preview is in flight has to be able to stop it;
    otherwise a finished session keeps paying the provider.
    """
    cancelled = asyncio.Event()

    async def slow(_transcript, _key):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    engine = AnticipationEngine(slow, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache cho hệ thống", _key())
    await asyncio.sleep(0.05)

    await engine.aclose()

    assert cancelled.is_set()
    assert engine.take(_key()) is None


@pytest.mark.asyncio
async def test_a_failed_speculation_is_invisible_to_the_turn() -> None:
    """Speculation is an optimisation; its failure must never surface."""
    async def boom(_transcript, _key):
        raise RuntimeError("provider down")

    engine = AnticipationEngine(boom, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache cho hệ thống", _key())
    await asyncio.sleep(0.05)

    assert engine.take(_key()) is None
    assert engine.stats.as_dict()["reasons"].get("error") == 1


@pytest.mark.asyncio
async def test_only_the_newest_transcript_is_spoken_for() -> None:
    """A result computed for text the candidate has since extended is wasted.

    It must be discarded rather than reused, and counted, because the wasted
    calls of §13 are how the speculation budget gets judged.
    """
    release = asyncio.Event()

    async def slow(transcript, _key):
        await release.wait()
        return f"token:{transcript}"

    engine = AnticipationEngine(slow, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache", _key("em dùng redis làm cache"))
    await asyncio.sleep(0.02)
    # The candidate keeps talking while the first call is still running.
    engine.observe("em dùng redis làm cache và memcached", _key("em dùng redis làm cache và memcached"))
    release.set()
    await asyncio.sleep(0.05)

    assert engine.stats.as_dict()["reasons"].get("superseded") == 1
    await engine.aclose()


# --- V2 review, part 1: what each side can truthfully validate --------------


def test_the_validator_is_strict_on_the_server_and_honest_on_the_client() -> None:
    """Seven fields in a dataclass are not seven-field validation.

    The worker knows only the session and the transcript; the server knows all
    seven. Strict mode (server) rejects anything missing as `incomplete`;
    lenient mode (worker) compares what it knows and names the rest as
    unchecked, so a reader of the record can see that the authoritative check
    happened elsewhere.
    """
    client = preview_key(session_id="s1", transcript="em dùng redis làm cache")
    server = _key()  # all seven populated

    assert client.missing() == ["turn_id", "question_id", "state_version", "rubric_version", "policy_version"]
    # Server, strict: the client-side key is not enough on its own.
    rejection = validate_preview(preview="t", computed_for=client, final=server, strict=True)
    assert rejection is not None and rejection.reason == "incomplete"
    assert set(rejection.fields) == set(client.missing())
    # Worker, lenient: session and transcript agree, the rest is unchecked.
    assert validate_preview(preview="t", computed_for=client, final=server, strict=False) is None
    assert client.unchecked(server) == client.missing()
    # The transcript is never skippable, on either side.
    blank = PreviewKey(session_id="s1", transcript_revision=None)
    assert validate_preview(preview="t", computed_for=blank, final=server, strict=False).reason == "incomplete"


def test_a_token_without_a_key_is_rejected_not_waved_through() -> None:
    """An older token format carries no key; strict mode treats that as no key."""
    assert PreviewKey.from_dict(None) is None
    assert PreviewKey.from_dict("garbage") is None
    assert validate_preview(preview="t", computed_for=PreviewKey.from_dict(None), final=_key()).reason == "no_key"
    # A round trip through the token payload is lossless.
    assert PreviewKey.from_dict(_key().as_dict()) == _key()


@pytest.mark.asyncio
async def test_an_unchanged_interim_does_not_cost_a_second_call() -> None:
    """Qwen re-emits the same interim while the candidate pauses; that is not new information."""
    calls: list[str] = []

    async def infer(transcript, key):
        calls.append(transcript)
        return "x"

    engine = AnticipationEngine(infer, enabled=True, debounce_seconds=0)
    for _ in range(4):
        engine.observe("em dùng redis làm cache cho hệ thống", _key())
        await asyncio.sleep(0.02)

    assert calls == ["em dùng redis làm cache cho hệ thống"]
    await engine.aclose()


@pytest.mark.asyncio
async def test_a_preview_is_consumed_exactly_once() -> None:
    async def infer(_transcript, _key):
        return "preview-token"

    engine = AnticipationEngine(infer, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache", _key())
    await asyncio.sleep(0.05)

    assert engine.take(_key()) == "preview-token"
    assert engine.take(_key()) is None, "the same token must not be handed to a second commit"
    assert engine.stats.as_dict()["reused"] == 1


@pytest.mark.asyncio
async def test_take_cancels_speculation_still_in_flight() -> None:
    """Canonical inference has started: a result that arrives now can never be
    used, and every further second of it is provider spend for nothing."""
    cancelled = asyncio.Event()

    async def slow(_transcript, _key):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    engine = AnticipationEngine(slow, enabled=True, debounce_seconds=0)
    engine.observe("em dùng redis làm cache cho hệ thống", _key())
    await asyncio.sleep(0.05)

    assert engine.take(_key()) is None
    await asyncio.sleep(0.01)
    assert cancelled.is_set()
    assert engine.stats.as_dict()["reasons"].get("cancelled_at_commit") == 1
    await engine.aclose()


@pytest.mark.asyncio
async def test_the_worker_key_claims_only_what_the_worker_knows() -> None:
    """runtime._preview_key once filled turn/question/state with a local counter
    and a prompt hash. It now leaves them None; the engine reports them as
    unchecked and the server enforces them at redemption."""
    async def infer(_transcript, _key):
        return "tok"

    seen: list[dict] = []
    engine = AnticipationEngine(infer, enabled=True, debounce_seconds=0,
                                on_event=lambda event, **f: seen.append({"event": event, **f}))
    worker_key = preview_key(session_id="s1", transcript="em dùng redis làm cache")
    engine.observe("em dùng redis làm cache", worker_key)
    await asyncio.sleep(0.05)

    assert engine.take(preview_key(session_id="s1", transcript="em dùng redis làm cache")) == "tok"
    reused = next(e for e in seen if e["event"] == "anticipation_reused")
    assert set(reused["unchecked_by_client"].split(",")) == {
        "turn_id", "question_id", "state_version", "rubric_version", "policy_version"
    }
