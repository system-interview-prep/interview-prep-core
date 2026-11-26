"""T4.2.1 — the shadow report from 50 decision summaries."""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "agent_shadow_report", ROOT / "scripts" / "eval" / "interviews" / "agent_shadow_report.py"
)
report_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report_module)  # type: ignore[union-attr]


def _records(n: int = 50) -> list[dict]:
    rng = random.Random(7)
    actions = ["acknowledge_and_probe", "move_to_next_question", "clarify_question"]
    rows = []
    for i in range(n):
        rule = rng.choice(actions)
        agree = rng.random() < 0.8
        agent = rule if agree else rng.choice([a for a in actions if a != rule])
        rows.append({
            "session_id": f"s{i % 5}", "turn_index": i, "mode": "shadow", "executed": "rule",
            "rule_action": rule, "agent_action": agent, "agent_reason": f"reason {i}",
            "agreement": agree, "menu_violation": "outside_menu" if i % 25 == 0 else None,
            "decide_error": None, "stage": rng.choice(["DEEP_DIVE", "VALIDATE"]),
            "pace": rng.choice(["behind", "on_track", "ahead"]), "in_menu": True, "latency_ms": 900,
        })
    return rows


def test_t4_2_1_agreement_by_action_stage_pace_and_the_disagreement_list() -> None:
    rows = _records()
    report = report_module.summarize(rows)
    assert report["turns"] == 50 and report["with_agent_decision"] == 50
    expected = round(sum(1 for r in rows if r["agreement"]) / 50, 3)
    assert report["agreement"] == expected
    assert set(report["by_rule_action"]) == {"acknowledge_and_probe", "move_to_next_question", "clarify_question"}
    assert set(report["by_pace"]) <= {"behind", "on_track", "ahead"} and set(report["by_stage"]) <= {"DEEP_DIVE", "VALIDATE"}
    assert sum(r["n"] for r in report["by_rule_action"].values()) == 50
    assert report["menu_violation_rate"] == 2 / 50
    assert len(report["disagreements"]) == sum(1 for r in rows if not r["agreement"])
    assert all(d["reason"] for d in report["disagreements"])
    text = report_module.render(report)
    assert "agreement:" in text and "disagreements (" in text


def test_live_turns_are_reported_on_their_own_not_dropped() -> None:
    """A LIVE turn has no rule decision beside it (rule_action is None), so the
    agreement section cannot use it. Before 2026-11-26 it vanished from the
    report entirely; now it has its own section, with the Unified gate."""
    rows = [
        {"session_id": "s1", "mode": "live", "executed": "agent", "rule_action": None,
         "agent_action": "acknowledge_and_probe", "menu_violation": None, "decide_error": None,
         "execution_path": "unified", "latency_ms": 3200, "unified_carried_text": True, "unified_rejected": None},
        {"session_id": "s1", "mode": "live", "executed": "rule", "rule_action": "acknowledge_and_probe",
         "agent_action": "acknowledge_and_probe", "menu_violation": "outside_menu", "decide_error": None,
         "execution_path": "unified", "latency_ms": 4100, "unified_carried_text": False, "unified_rejected": ["probe_text_rejected"]},
        {"session_id": "s2", "mode": "live", "executed": "agent", "rule_action": None,
         "agent_action": "move_to_next_question", "menu_violation": None, "decide_error": None,
         "execution_path": "unified", "latency_ms": 2900, "unified_carried_text": None, "unified_rejected": None},
    ]
    live = report_module.summarize(rows)["live"]
    assert live["turns"] == 3 and live["sessions"] == 2
    assert live["executed_by_agent_rate"] == round(2 / 3, 3) and live["fell_back_to_rule"] == 1
    assert live["menu_violation_rate"] == round(1 / 3, 4)
    assert live["by_agent_action"] == {"acknowledge_and_probe": 2, "move_to_next_question": 1}
    assert live["latency_ms_p50"] == 3200
    assert live["unified"] == {"turns": 2, "carried_text_rate": 0.5, "rejected_rate": 0.5, "rejected_by": {"probe_text_rejected": 1}}
    text = report_module.render(report_module.summarize(rows))
    assert "live turns" in text and "parity needs a shadow run" in text


def test_the_report_tolerates_turns_without_an_agent_decision() -> None:
    rows = [{"rule_action": "clarify_question", "agent_action": None, "agreement": None, "mode": "rule", "executed": "rule"}]
    report = report_module.summarize(rows)
    assert report["turns"] == 1 and report["with_agent_decision"] == 0 and report["agreement"] is None


def test_the_report_measures_the_unified_thinker_gate() -> None:
    """Phase 3's exit gate asks for response-quality parity from live turns.

    The report is where that becomes readable: how often §6.2's single
    inference spoke for itself, and which guard sent the rest back.
    """
    from scripts.eval.interviews.agent_shadow_report import render, summarize

    rows = [
        {"mode": "live", "executed": "agent", "agent_action": "acknowledge_and_probe",
         "rule_action": "acknowledge_and_probe", "agreement": True,
         "unified_carried_text": True, "unified_rejected": None},
        {"mode": "live", "executed": "agent", "agent_action": "clarify_question",
         "rule_action": "clarify_question", "agreement": True,
         "unified_carried_text": True, "unified_rejected": None},
        {"mode": "live", "executed": "agent", "agent_action": "acknowledge_and_probe",
         "rule_action": "acknowledge_and_probe", "agreement": True,
         "unified_carried_text": False, "unified_rejected": ["probe_text_rejected"]},
        # A rule-mode turn contributes nothing: no model decision ran.
        {"mode": "rule", "executed": "rule", "rule_action": "move_to_next_question"},
    ]

    unified = summarize(rows)["unified"]

    assert unified["turns"] == 3
    assert unified["carried_text_rate"] == round(2 / 3, 3)
    assert unified["rejected_rate"] == round(1 / 3, 3)
    assert unified["rejected_by"] == {"probe_text_rejected": 1}
    assert "unified thinker" in render(summarize(rows))


def test_turns_without_a_unified_signal_report_nothing() -> None:
    """With the flag off there is no gate to report, not a zero to misread."""
    from scripts.eval.interviews.agent_shadow_report import summarize

    rows = [{"mode": "live", "executed": "agent", "agent_action": "move_to_next_question",
             "rule_action": "move_to_next_question", "agreement": True}]

    assert summarize(rows)["unified"] == {"turns": 0}
