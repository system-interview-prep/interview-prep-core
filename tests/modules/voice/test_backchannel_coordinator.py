"""When the Talker is asked and whether its answer may still be played (realtime/backchannel.py).

No templates, no words: the coordinator holds the cost bounds and the
staleness rules. The Talker is a scripted stream here.
"""

from __future__ import annotations

import asyncio

import pytest

from src.modules.interviews.realtime import backchannel as bc


class _Talker:
    def __init__(self, script=None, *, delay: float = 0.0) -> None:
        self.script = script or [{"type": "unit", "text": "Ừ, mình hình dung được.", "kind": "reflective", "index": 1, "ttft_ms": 300, "at_ms": 420},
                                 {"type": "done", "kind": "reflective", "ttft_ms": 300, "total_ms": 500, "units": 1, "rejected": [], "reason": None}]
        self.delay = delay
        self.requests: list[dict] = []

    async def __call__(self, request):
        self.requests.append(request)
        for item in self.script:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield item


class _Handle:
    def __init__(self) -> None:
        self.playing = True

    def done(self) -> bool:
        return not self.playing


def _coordinator(talker, *, enabled=True, **kw):
    played: list[tuple[str, str]] = []
    events: list[tuple[str, dict]] = []
    handles: list[_Handle] = []

    def play(text, kind):
        played.append((text, kind))
        handle = _Handle()
        handles.append(handle)
        return handle

    coordinator = bc.BackchannelCoordinator(
        talker, play, enabled=enabled, min_gap_seconds=kw.pop("min_gap", 0.0), hold_seconds=kw.pop("hold", 0.3),
        on_event=lambda event, **fields: events.append((event, fields)), **kw,
    )
    return coordinator, played, events, handles


async def _settle(coordinator):
    if coordinator._task is not None:
        with pytest.raises(asyncio.CancelledError) if False else _nullcontext():
            await asyncio.gather(coordinator._task, return_exceptions=True)


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_a_unit_is_played_when_the_candidate_is_quiet_and_the_signals_travel() -> None:
    talker = _Talker()
    coordinator, played, events, _ = _coordinator(talker)
    coordinator.on_user_stopped()
    assert coordinator.observe("Em dùng Redis làm cache", phase="listening", answer_seconds=6.2, fragments=2)
    await _settle(coordinator)
    assert played == [("Ừ, mình hình dung được.", "reflective")]
    request = talker.requests[0]
    assert request["phase"] == "listening" and request["speaking"] is False
    assert request["signals"]["answer_seconds"] == 6.2 and request["signals"]["fragments"] == 2
    assert request["signals"]["seconds_since_last_backchannel"] is None and request["signals"]["last_backchannels"] == []
    names = [e for e, _ in events]
    assert names == ["backchannel_spoken", "backchannel"]
    spoken = dict(events[0][1])
    assert spoken["kind"] == "reflective" and spoken["words"] == 5 and spoken["ttft_ms"] == 300
    done = dict(events[1][1])
    assert done["spoken"] == 1 and done["units"] == 1 and done["ok"] is True
    assert coordinator.stats.as_dict()["spoken"] == 1 and coordinator.stats.as_dict()["stale_rate"] == 0.0
    # The next request tells the model what was already said and how long ago.
    coordinator.observe("Em dùng Redis làm cache và TTL", phase="listening")
    await _settle(coordinator)
    assert talker.requests[1]["signals"]["last_backchannels"] == ["Ừ, mình hình dung được."]
    assert talker.requests[1]["signals"]["seconds_since_last_backchannel"] is not None


@pytest.mark.asyncio
async def test_disabled_never_asks() -> None:
    talker = _Talker()
    coordinator, played, _, _ = _coordinator(talker, enabled=False)
    assert not coordinator.observe("Em dùng Redis", phase="listening")
    assert talker.requests == [] and played == []


@pytest.mark.asyncio
async def test_one_inference_in_flight_and_a_turn_cap_bound_the_calls() -> None:
    talker = _Talker(delay=0.05)
    coordinator, played, _, _ = _coordinator(talker, calls_per_turn=2)
    assert coordinator.observe("một", phase="listening")
    assert not coordinator.observe("một hai", phase="listening"), "a second request while one runs is skipped, not queued"
    await _settle(coordinator)
    assert coordinator.observe("một hai ba", phase="listening")
    await _settle(coordinator)
    assert not coordinator.observe("một hai ba bốn", phase="listening"), "two calls this turn: the cap"
    assert coordinator.stats.reasons.get("skipped:in_flight") == 1 and coordinator.stats.reasons.get("skipped:turn_cap") == 1
    coordinator.reset_turn()
    assert coordinator.observe("câu trả lời mới", phase="listening")
    await _settle(coordinator)
    assert len(talker.requests) == 3 and len(played) == 3


@pytest.mark.asyncio
async def test_the_minimum_gap_and_an_unchanged_transcript_skip_while_listening_but_not_processing() -> None:
    talker = _Talker()
    coordinator, _, _, _ = _coordinator(talker, min_gap=60.0)
    assert coordinator.observe("Em dùng Redis", phase="listening")
    await _settle(coordinator)
    assert not coordinator.observe("Em dùng Redis", phase="listening")
    assert coordinator.stats.reasons.get("skipped:unchanged") == 1
    assert not coordinator.observe("Em dùng Redis và TTL", phase="listening")
    assert coordinator.stats.reasons.get("skipped:min_gap") == 1
    # The processing acknowledgment is its own moment: the gap does not apply.
    assert coordinator.observe("Em dùng Redis và TTL", phase="processing")
    await _settle(coordinator)
    assert talker.requests[-1]["phase"] == "processing"


@pytest.mark.asyncio
async def test_a_unit_waits_briefly_for_a_pause_and_is_dropped_if_the_candidate_keeps_talking() -> None:
    talker = _Talker()
    coordinator, played, events, _ = _coordinator(talker, hold=0.25)
    coordinator.observe("Em dùng Redis", phase="listening")
    coordinator.on_user_speaking()  # they resumed before the unit arrived
    await _settle(coordinator)
    assert played == []
    discarded = [f for e, f in events if e == "backchannel_discarded"]
    assert discarded and discarded[0]["reason"] == "candidate_speaking" and discarded[0]["held_ms"] >= 200
    assert coordinator.stats.as_dict()["stale_rate"] == 1.0

    # ...but a pause inside the hold lets it play: a line prepared during
    # speech (enough new words) waits for the pause, then plays.
    talker2 = _Talker()
    coordinator2, played2, _, _ = _coordinator(talker2, hold=0.5)
    coordinator2.on_user_speaking()
    assert coordinator2.observe("Em dùng Redis làm cache và đặt TTL năm phút cho từng key", phase="listening")

    async def pause_soon():
        await asyncio.sleep(0.15)
        coordinator2.on_user_stopped()

    await asyncio.gather(pause_soon(), coordinator2._task)
    assert len(played2) == 1


@pytest.mark.asyncio
async def test_while_speaking_the_talker_is_asked_only_after_enough_new_words() -> None:
    talker = _Talker(delay=0.0)
    coordinator, _, _, _ = _coordinator(talker, calls_per_turn=5)
    coordinator.on_user_speaking()
    assert not coordinator.observe("Em dùng Redis", phase="listening")
    assert coordinator.stats.reasons.get("skipped:few_new_words") == 1
    assert coordinator.observe("Em dùng Redis làm cache và đặt TTL năm phút cho", phase="listening")
    await _settle(coordinator)
    assert talker.requests[-1]["speaking"] is True and talker.requests[-1]["prepared_for_words"] == 11


@pytest.mark.asyncio
async def test_a_prepared_line_is_revalidated_against_the_current_transcript_before_playback() -> None:
    """Audit item 8: the candidate said a lot more since the line was written -> stale."""
    talker = _Talker(delay=0.05)
    coordinator, played, events, _ = _coordinator(talker)
    coordinator.observe("Em dùng Redis làm cache", phase="listening")
    coordinator.note_transcript("Em dùng Redis làm cache " + " ".join(["và"] * 20))
    await _settle(coordinator)
    assert played == []
    assert [f for e, f in events if e == "backchannel_discarded"][0]["reason"] == "context_moved"
    # A little drift is fine.
    talker2 = _Talker(delay=0.05)
    coordinator2, played2, _, _ = _coordinator(talker2)
    coordinator2.observe("Em dùng Redis làm cache", phase="listening")
    coordinator2.note_transcript("Em dùng Redis làm cache và TTL")
    await _settle(coordinator2)
    assert len(played2) == 1


@pytest.mark.asyncio
async def test_a_cancelled_inference_never_overlaps_the_next_one() -> None:
    """Audit item 7: the new request waits for the cancelled stream to unwind."""
    order: list[str] = []

    async def slow(request):
        order.append(f"start:{request['transcript']}")
        try:
            await asyncio.sleep(0.2)
            yield {"type": "done", "kind": "silence", "ttft_ms": 1, "total_ms": 2, "units": 0, "rejected": [], "reason": None}
        finally:
            await asyncio.sleep(0.05)  # the provider connection closing
            order.append(f"end:{request['transcript']}")

    coordinator, _, _, _ = _coordinator(slow)
    coordinator.observe("một", phase="listening")
    await asyncio.sleep(0.02)
    coordinator.reset_turn()  # cancels the first
    coordinator.observe("hai", phase="listening")
    await asyncio.sleep(0.01)
    await asyncio.gather(*[t for t in coordinator._closing], coordinator._task, return_exceptions=True)
    assert order == ["start:một", "end:một", "start:hai", "end:hai"]


@pytest.mark.asyncio
async def test_the_authoritative_reply_wins_over_a_pending_backchannel() -> None:
    talker = _Talker(delay=0.1)
    coordinator, played, events, _ = _coordinator(talker)
    coordinator.observe("Em dùng Redis", phase="processing")
    coordinator.on_reply_ready()  # Core answered first
    await _settle(coordinator)
    assert played == []
    assert coordinator.stats.reasons.get("cancelled:reply_ready") == 1
    # Nothing is asked for this turn while listening once the reply exists.
    assert not coordinator.observe("Em dùng Redis thêm", phase="listening")
    assert coordinator.stats.reasons.get("skipped:reply_ready") == 1


@pytest.mark.asyncio
async def test_a_new_answer_discards_the_previous_turns_inference() -> None:
    talker = _Talker(delay=0.1)
    coordinator, played, _, _ = _coordinator(talker)
    coordinator.observe("Em dùng Redis", phase="listening")
    coordinator.reset_turn()
    await _settle(coordinator)
    assert played == [] and coordinator.stats.reasons.get("cancelled:new_turn") == 1


@pytest.mark.asyncio
async def test_silence_and_errors_are_counted_not_spoken() -> None:
    quiet = _Talker([{"type": "done", "kind": "silence", "ttft_ms": 250, "total_ms": 260, "units": 0, "rejected": [], "reason": None}])
    coordinator, played, events, _ = _coordinator(quiet)
    coordinator.observe("Em", phase="listening")
    await _settle(coordinator)
    assert played == [] and coordinator.stats.silent == 1
    assert dict(events[-1][1])["kind"] == "silence"

    async def broken(_request):
        raise RuntimeError("api down")
        yield  # pragma: no cover

    coordinator2, played2, events2, _ = _coordinator(broken)
    coordinator2.observe("Em", phase="listening")
    await _settle(coordinator2)
    assert played2 == [] and coordinator2.stats.errors == 1
    assert dict(events2[-1][1])["ok"] is False and dict(events2[-1][1])["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_the_candidate_talking_over_a_playing_backchannel_is_counted() -> None:
    talker = _Talker()
    coordinator, _, events, handles = _coordinator(talker)
    coordinator.observe("Em dùng Redis", phase="listening")
    await _settle(coordinator)
    coordinator.on_user_speaking()  # while the unit is still playing
    assert coordinator.stats.overlaps == 1 and ("backchannel_overlap", {}) in events
    handles[0].playing = False
    coordinator.on_user_stopped()
    coordinator.on_user_speaking()
    assert coordinator.stats.overlaps == 1, "a finished backchannel is not an overlap"
