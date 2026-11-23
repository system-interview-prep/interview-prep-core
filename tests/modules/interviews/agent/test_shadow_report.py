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


def test_the_report_tolerates_turns_without_an_agent_decision() -> None:
    rows = [{"rule_action": "clarify_question", "agent_action": None, "agreement": None, "mode": "rule", "executed": "rule"}]
    report = report_module.summarize(rows)
    assert report["turns"] == 1 and report["with_agent_decision"] == 0 and report["agreement"] is None
