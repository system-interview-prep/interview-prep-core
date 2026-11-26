"""Adaptive endpointing: wait on what was said, not on a word count.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §8 and the V2 correction brief §4. The fixed
1.8/2.2 s windows wait the same after a finished sentence as mid-thought. These
tests pin the two things that matter: a finished answer may commit quickly, and
a candidate who is still talking must not be cut off.
"""

from __future__ import annotations

import pytest

from src.modules.interviews.realtime.endpointing import (
    ENDPOINT_COMPLETE,
    ENDPOINT_HESITANT,
    ENDPOINT_UNSURE,
    HESITANT_COMMIT_SECONDS,
    AnswerWindow,
    adaptive_commit_delay,
    adaptive_endpointing_enabled,
    completeness,
)


@pytest.fixture
def adaptive(monkeypatch):
    monkeypatch.setenv("VOICE_ADAPTIVE_ENDPOINTING", "1")
    assert adaptive_endpointing_enabled()


@pytest.mark.parametrize(
    "text",
    [
        "Em dùng Redis làm cache, đặt TTL năm phút và xoá key sau khi ghi database.",
        "Dạ em chưa làm với Kubernetes ạ",          # closing particle, no full stop
        "Em nghĩ vậy là đủ rồi",                      # "rồi" closes the thought
        "That is how I handled the race condition.",
    ],
)
def test_a_finished_answer_is_complete(text: str) -> None:
    assert completeness(text) == ENDPOINT_COMPLETE


@pytest.mark.parametrize(
    "text",
    [
        "Em dùng Redis làm cache và",               # clause waiting for its other half
        "Ừm",                                         # pure filler
        "Em đặt TTL năm phút, tức là",              # still explaining
        "Em xử lý bằng cách",                        # trailing preposition
        "I would use a lock because",
    ],
)
def test_a_half_finished_clause_is_hesitant(text: str) -> None:
    """These are the false cuts: committing here truncates the candidate."""
    assert completeness(text) == ENDPOINT_HESITANT


def test_an_unstable_transcript_is_never_complete() -> None:
    """Qwen is still revising the end, so the end is not known yet."""
    finished = "Em đã xử lý xong việc đó rồi."
    assert completeness(finished, stable=True) == ENDPOINT_COMPLETE
    assert completeness(finished, stable=False) == ENDPOINT_HESITANT


def test_low_confidence_lowers_a_verdict_but_never_raises_one() -> None:
    """A misheard transcript ending in a full stop is not proof of silence."""
    text = "Em đã xử lý xong việc đó rồi."
    assert completeness(text, confidence=0.9) == ENDPOINT_COMPLETE
    assert completeness(text, confidence=0.2) == ENDPOINT_UNSURE
    # It cannot promote a hesitant tail into something committable.
    assert completeness("Em dùng Redis và", confidence=0.99) == ENDPOINT_HESITANT


def test_the_bands_match_the_spec_and_never_rush_a_hesitation() -> None:
    """§8: 250-500 ms complete, 500-1000 ms unsure, 1000-2200 ms hesitating."""
    complete, _ = adaptive_commit_delay("Em làm xong rồi.")
    unsure, _ = adaptive_commit_delay("Em dùng Redis làm cache")
    hesitant, _ = adaptive_commit_delay("Em dùng Redis và")

    assert 0.25 <= complete <= 0.5
    assert 0.5 <= unsure <= 1.0
    assert 1.0 <= hesitant <= 2.2
    assert complete < unsure < hesitant


def test_the_window_keeps_the_fixed_waits_until_the_flag_is_set(monkeypatch) -> None:
    """Brief §4: do not reduce thresholds blindly. Default behaviour is unchanged."""
    monkeypatch.delenv("VOICE_ADAPTIVE_ENDPOINTING", raising=False)
    window = AnswerWindow(commit_silence=1.8, short_commit_silence=2.2)
    window.add("Em dùng Redis làm cache, đặt TTL năm phút và xoá key khi ghi.", message_id="m1")

    assert window.commit_delay() == 1.8
    assert window.last_endpoint_verdict is None


def test_the_window_commits_a_finished_answer_sooner_when_adaptive(adaptive) -> None:
    window = AnswerWindow(commit_silence=1.8, short_commit_silence=2.2)
    window.note_interim("Em dùng Redis làm cache, đặt TTL năm phút và xoá key khi ghi.")
    window.note_interim("Em dùng Redis làm cache, đặt TTL năm phút và xoá key khi ghi.")
    window.add("Em dùng Redis làm cache, đặt TTL năm phút và xoá key khi ghi.", message_id="m1")

    delay = window.commit_delay()

    assert window.last_endpoint_verdict == ENDPOINT_COMPLETE
    assert delay < 1.8


def test_a_short_unfinished_buffer_still_waits_the_long_window(adaptive) -> None:
    """A lone mis-heard word must not end an answer just for lacking a cue."""
    window = AnswerWindow(commit_silence=1.8, short_commit_silence=2.2)
    window.note_interim("Redis")
    window.note_interim("Redis")
    window.add("Redis", message_id="m1")

    assert window.commit_delay() >= min(2.2, HESITANT_COMMIT_SECONDS)


def test_a_still_moving_transcript_waits_the_hesitant_band(adaptive) -> None:
    window = AnswerWindow(commit_silence=1.8, short_commit_silence=2.2)
    window.note_interim("Em dùng Redis làm cache, đặt TTL")
    window.note_interim("Em dùng Redis làm cache, đặt TTL năm phút và xoá key khi ghi.")
    window.add("Em dùng Redis làm cache, đặt TTL năm phút và xoá key khi ghi.", message_id="m1")

    assert window.transcript_stable is False
    assert window.commit_delay() >= 1.0
    assert window.last_endpoint_verdict == ENDPOINT_HESITANT
