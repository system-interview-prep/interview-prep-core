"""The human-speaker benchmark's pure parts: flags, budget, labels. No room, no provider."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "eval" / "interviews" / "voice" / "livekit_human_benchmark.py"
_spec = importlib.util.spec_from_file_location("livekit_human_benchmark", _SCRIPT)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_every_configuration_keeps_hybrid_speculation_and_adaptive_endpointing_off() -> None:
    for config, flags in bench.FLAGS.items():
        assert flags["VOICE_LLM_SPECULATIVE_PREVIEW"] == "0", config
        assert flags["VOICE_ADAPTIVE_ENDPOINTING"] == "0", config
    assert bench.ALWAYS["INTERVIEW_TALKER_ENABLED"] == "false"
    # The two proposed changes are each their own arm, so a false cut has one cause.
    assert bench.FLAGS["unified_speech_end"]["VOICE_ANSWER_COMMIT_ANCHOR"] == "speech_end"
    assert bench.FLAGS["unified_speech_end"]["VOICE_ANSWER_PAUSE_NUDGE_ENABLED"] == "1"
    assert bench.FLAGS["unified_no_nudge"]["VOICE_ANSWER_COMMIT_ANCHOR"] == "transcript"
    assert bench.FLAGS["unified_no_nudge"]["VOICE_ANSWER_PAUSE_NUDGE_ENABLED"] == "0"


def test_env_mismatches_name_each_flag_the_container_lacks() -> None:
    env = {"INTERVIEW_UNIFIED_THINKER": "true", "VOICE_LLM_SPECULATIVE_PREVIEW": "0",
           "VOICE_ADAPTIVE_ENDPOINTING": "off", "VOICE_ANSWER_COMMIT_ANCHOR": "transcript",
           "INTERVIEW_TALKER_ENABLED": "false", "INTERVIEW_AGENT_MODE": "live"}
    # the nudge flag is unset: default on, so "1" is satisfied by absence? No -- absence reads as "0".
    problems = bench.env_mismatches("unified", env)
    assert problems == ["VOICE_ANSWER_PAUSE_NUDGE_ENABLED: have None, need '1'"]
    assert bench.env_mismatches("unified", {**env, "VOICE_ANSWER_PAUSE_NUDGE_ENABLED": "yes"}) == []
    assert any(p.startswith("VOICE_ANSWER_COMMIT_ANCHOR") for p in bench.env_mismatches("unified_speech_end", env))


def test_the_budget_check_and_the_label_template() -> None:
    assert bench.budget_check(16, 40) == {"calls": 16, "budget": 40, "within": True, "over_by": 0}
    assert bench.budget_check(44, 40)["within"] is False and bench.budget_check(44, 40)["over_by"] == 4
    template = bench.labels_template([{"turn_index": 0, "words": 40, "speech_end_to_submit_ms": 2860}])
    assert template[0]["turn_index"] == 0 and template[0]["false_cut"] is None and template[0]["notes"] == ""


def test_labelled_false_cuts_come_from_the_listener_not_the_proxy() -> None:
    labels = [
        {"turn_index": 0, "false_cut": False},
        {"turn_index": 1, "false_cut": True, "cut_pause_s": 2.1, "reply_overlapped_speech": True},
        {"turn_index": 2, "false_cut": True, "cut_pause_s": 1.7},
        {"turn_index": 3, "false_cut": None},  # not judged
    ]
    assert bench.labelled_false_cuts(labels) == {
        "labelled": 3, "false_cuts": 2, "rate": round(2 / 3, 3), "shortest_cut_pause_s": 1.7, "overlapped_replies": 1,
    }
    assert bench.labelled_false_cuts([])["rate"] is None


def test_the_checklist_makes_the_person_leave_the_warm_up_and_pause_on_purpose() -> None:
    text = bench.render_checklist("unified")
    assert "self-introduction" in text and "thinking pauses" in text and "INTERVIEW_UNIFIED_THINKER=1" in text
