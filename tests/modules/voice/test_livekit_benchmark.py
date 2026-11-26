"""The bounded LiveKit benchmark tool: configuration gate and comparison table.

Opens no room, calls nothing: the tool only checks flags and reads reports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "eval" / "interviews" / "voice" / "livekit_benchmark.py"
_spec = importlib.util.spec_from_file_location("livekit_benchmark", _SCRIPT)
bench = importlib.util.module_from_spec(_spec)
sys.modules["livekit_benchmark"] = bench
_spec.loader.exec_module(bench)
metrics = bench._metrics_module()


def test_the_four_configurations_differ_only_in_the_three_flags_and_hybrid_is_always_off() -> None:
    keys = {tuple(sorted(c)) for c in bench.CONFIGS.values()}
    assert keys == {("INTERVIEW_UNIFIED_THINKER", "VOICE_ADAPTIVE_ENDPOINTING", "VOICE_LLM_SPECULATIVE_PREVIEW")}
    assert bench.CONFIGS["baseline"] == {"INTERVIEW_UNIFIED_THINKER": "0", "VOICE_LLM_SPECULATIVE_PREVIEW": "0", "VOICE_ADAPTIVE_ENDPOINTING": "0"}
    assert bench.CONFIGS["endpointing"] == {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "1", "VOICE_ADAPTIVE_ENDPOINTING": "1"}
    assert bench.ALWAYS_OFF == {"INTERVIEW_TALKER_ENABLED": "false"}


def test_the_check_refuses_an_environment_that_does_not_match() -> None:
    env = {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "0", "VOICE_ADAPTIVE_ENDPOINTING": "0", "INTERVIEW_TALKER_ENABLED": "false"}
    assert bench.env_mismatches("unified", env) == []
    assert bench.env_mismatches("baseline", env) == ["INTERVIEW_UNIFIED_THINKER=1 (want 0)"]
    hybrid_on = {**env, "INTERVIEW_TALKER_ENABLED": "true"}
    assert bench.env_mismatches("unified", hybrid_on) == ["INTERVIEW_TALKER_ENABLED=true (want false)"]
    assert bench.main(["check", "baseline"]) in (0, 1)  # prints the exports; verdict depends on the shell env


def test_the_comparison_table_lines_up_the_configurations() -> None:
    empty = metrics.summarize([], [])
    reports = {"baseline": dict(empty), "anticipation": dict(empty)}
    reports["anticipation"]["ttmr_ms"] = {"n": 8, "p50": 2100.0, "p95": 3900.0, "p99": 4200.0, "min": 1800.0, "max": 4200.0}
    reports["anticipation"]["anticipation"] = {"reused": 5, "discarded": 3, "ready": 6, "hit_rate": 0.625, "discard_reasons": {}}
    table = bench.compare_table(reports)
    header, *rows = table.splitlines()
    assert header.split() == ["metric", "baseline", "anticipation"]
    ttmr = next(r for r in rows if r.startswith("TTMR p50"))
    assert ttmr.split()[-2:] == ["—", "2100"]
    hit = next(r for r in rows if r.startswith("anticipation hit rate"))
    assert hit.split()[-1] == "0.62"
