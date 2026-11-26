"""The Talker as a context-aware backchanneler (brief 2026-11-26).

Same model, same facade, same acknowledgement guard as the Hybrid Talker; a
different job. These tests script the token stream and check what leaves:
complete validated units in order, silence when the model chooses it, nothing
on a bad line, no exception on a failure -- and that the Talker never sees a
rubric, a menu or a probe target.
"""

from __future__ import annotations

import json

import pytest

from src.modules.interviews.agents.interviewer import talker
from src.modules.interviews.agents.interviewer.arbiter import arbitrate_backchannel

CTX = talker.BackchannelContext(
    session_id="s1",
    question="Bạn xử lý cache invalidation thế nào?",
    transcript="Em dùng Redis làm cache và đặt TTL năm phút cho từng key, còn khi dữ liệu đổi thì",
    phase="listening",
    speaking=False,
    recent_lines=["Bạn xử lý cache invalidation thế nào?"],
    last_backchannels=["Ừm."],
    seconds_since_last=7.5,
    answer_seconds=12.0,
    fragments=2,
)


def _stream(*chunks: str):
    async def generate(**kwargs):
        generate.kwargs = kwargs
        for chunk in chunks:
            yield chunk

    generate.kwargs = {}
    return generate


async def _collect(generate, ctx=CTX, **kw):
    return [item async for item in talker.backchannel_by_talker(ctx, generate_stream=generate, model="gpt-5.6-luna", **kw)]


# ----------------------------------------------------------------- parsing


def test_the_first_line_names_the_kind_and_silence_is_a_kind() -> None:
    assert talker.parse_kind("kind: silence") == "silence"
    assert talker.parse_kind("Kind: Reflective ") == "reflective"
    assert talker.parse_kind("kind: verdict") is None
    assert talker.parse_kind("Ừ, mình hiểu.") is None


def test_units_are_complete_sentences_and_the_tail_waits() -> None:
    done, tail = talker.split_units("Ừ, mình hình dung được. Bạn cứ")
    assert done == ["Ừ, mình hình dung được."] and tail == "Bạn cứ"
    assert talker.split_units("Chưa hết câu") == ([], "Chưa hết câu")
    assert talker.split_units("Một câu. Hai câu! ") == (["Một câu.", "Hai câu!"], "")


# ------------------------------------------------------------- the stream


@pytest.mark.asyncio
async def test_a_reflective_line_arrives_as_one_validated_unit_with_its_timing() -> None:
    generate = _stream("kind: reflec", "tive\n", "À, TTL theo ", "từng key.", "")
    items = await _collect(generate)
    assert [i["type"] for i in items] == ["unit", "done"]
    unit, done = items
    assert unit["text"] == "À, TTL theo từng key." and unit["kind"] == "reflective" and unit["index"] == 1
    assert unit["ttft_ms"] is not None and unit["at_ms"] >= unit["ttft_ms"]
    assert done["kind"] == "reflective" and done["units"] == 1 and done["reason"] is None and done["rejected"] == []
    # What the model was asked, and with what.
    kwargs = generate.kwargs
    assert kwargs["model"] == "gpt-5.6-luna" and kwargs["purpose"] == talker.BACKCHANNEL_PURPOSE
    payload = json.loads(kwargs["input_text"])
    assert payload["candidate_so_far"].startswith("Em dùng Redis") and payload["phase"] == "listening"
    assert payload["signals"]["seconds_since_last_backchannel"] == 7.5 and payload["last_backchannels"] == ["Ừm."]
    for forbidden in ("rubric", "criteria", "menu", "allowed_actions", "missing_aspect", "evaluation", "cv", "jd"):
        assert forbidden not in payload, forbidden
    assert "tiêu chí" not in kwargs["input_text"]


@pytest.mark.asyncio
async def test_silence_yields_no_unit_and_stops_reading() -> None:
    read: list[str] = []

    async def generate(**_kw):
        for chunk in ("kind: silence\n", "should not be read"):
            read.append(chunk)
            yield chunk

    items = [i async for i in talker.backchannel_by_talker(CTX, generate_stream=generate, model="gpt-5.6-luna")]
    assert items == [{"type": "done", "kind": "silence", "ttft_ms": items[0]["ttft_ms"], "total_ms": items[0]["total_ms"],
                      "units": 0, "rejected": [], "reason": None}]
    assert read == ["kind: silence\n"], "the stream is left as soon as the choice is known"


@pytest.mark.asyncio
async def test_silence_on_a_single_line_without_newline_is_silence_too() -> None:
    items = await _collect(_stream("kind: silence"))
    assert items[-1]["kind"] == "silence" and items[-1]["units"] == 0 and items[-1]["reason"] is None


@pytest.mark.asyncio
async def test_two_sentences_become_two_units_in_order() -> None:
    items = await _collect(_stream("kind: contextual\n", "Ừ. ", "Redis với TTL, mình theo được.\n"))
    assert [i["text"] for i in items if i["type"] == "unit"] == ["Ừ.", "Redis với TTL, mình theo được."]
    assert items[-1]["units"] == 2


@pytest.mark.asyncio
async def test_a_praising_or_judging_line_is_stopped_by_the_guard() -> None:
    items = await _collect(_stream("kind: contextual\n", "Rất tốt, bạn nắm vấn đề khá chắc."))
    assert [i["type"] for i in items] == ["done"]
    assert items[0]["units"] == 0 and items[0]["rejected"] and items[0]["reason"] == "all_rejected"


@pytest.mark.asyncio
async def test_a_question_and_an_over_long_line_are_stopped_too() -> None:
    items = await _collect(_stream("kind: contextual\n", "Vậy bạn xoá cache khi nào?"))
    assert items[-1]["rejected"] == ["is_question"]
    long_line = " ".join(["ừ"] * 20) + "."
    items = await _collect(_stream("kind: minimal\n", long_line), max_words=12)
    assert items[-1]["rejected"] == ["too_long"]


@pytest.mark.asyncio
async def test_a_line_that_does_not_follow_the_format_is_dropped() -> None:
    items = await _collect(_stream("Ừ, mình hiểu rồi.\n", "Tiếp đi."))
    assert items == [{"type": "done", "kind": None, "ttft_ms": items[0]["ttft_ms"], "total_ms": items[0]["total_ms"],
                      "units": 0, "rejected": [], "reason": "bad_format"}]


@pytest.mark.asyncio
async def test_a_provider_failure_never_raises() -> None:
    async def boom(**_kw):
        raise RuntimeError("luna down")
        yield  # pragma: no cover

    items = [i async for i in talker.backchannel_by_talker(CTX, generate_stream=boom, model="gpt-5.6-luna")]
    assert items[-1]["type"] == "done" and items[-1]["reason"] == "error:RuntimeError"


@pytest.mark.asyncio
async def test_no_model_means_no_call() -> None:
    called = False

    async def generate(**_kw):
        nonlocal called
        called = True
        yield "x"

    items = [i async for i in talker.backchannel_by_talker(CTX, generate_stream=generate, model="")]
    assert items == [{"type": "done", "kind": None, "ttft_ms": None, "total_ms": 0, "units": 0, "rejected": [], "reason": "no_model"}]
    assert not called


def test_the_feature_needs_both_the_setting_and_a_talker_model(monkeypatch) -> None:
    settings = talker.get_settings()
    monkeypatch.setattr(settings, "interview_backchannel_enabled", True)
    monkeypatch.setattr(settings, "interview_talker_model", "")
    assert not talker.backchannel_enabled()
    monkeypatch.setattr(settings, "interview_talker_model", "gpt-5.6-luna")
    assert talker.backchannel_enabled()
    monkeypatch.setattr(settings, "interview_backchannel_enabled", False)
    assert not talker.backchannel_enabled()


def test_the_prompt_asks_for_a_decision_and_forbids_evaluation_without_templates() -> None:
    prompt = talker.backchannel_prompt(True)
    assert "kind: silence" in prompt and "Im lặng là lựa chọn đúng" in prompt
    assert "Không nhận xét đúng/sai" in prompt and "không gợi ý đáp án" in prompt
    # No canned lines: the prompt names kinds, never utterances to pick from.
    assert "Mình nghe rồi" not in prompt and "cứ nói tiếp" not in prompt


# ------------------------------------------------------------- the guard


def test_the_backchannel_guard_is_the_acknowledgement_guard_plus_length_and_repeats() -> None:
    assert arbitrate_backchannel("Ừ, mình hình dung được.", answer="Em dùng Redis.") is None
    assert arbitrate_backchannel("", answer="") == "empty"
    assert arbitrate_backchannel(" ".join(["ừ"] * 15) + ".", max_words=14) == "too_long"
    assert arbitrate_backchannel("Ừ, mình hình dung được", last_backchannels=["Ừ, mình hình dung được."]) == "repeats_backchannel"
    assert arbitrate_backchannel("Vậy bạn làm gì tiếp?") == "is_question"
    assert arbitrate_backchannel("Rất tốt, bạn nắm vấn đề khá chắc.") is not None
