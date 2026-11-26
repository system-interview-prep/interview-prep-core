"""Bounded LiveKit benchmark for V2 spec §13 -- four configurations, real audio.

This is the runbook and the comparison tool. It opens no room and calls no
provider itself: a human (or a recorded candidate played into a microphone)
speaks the same answer script to the real pipeline -- Qwen STT, the OpenAI
Thinker, VieNeu TTS, the existing LiveKit deployment -- once per
configuration, and this script reads what the agent reported
(``livekit_turn_metrics.py``) and prints the comparison. Real partial
transcripts, real endpointing, no forced cache hits.

Configurations (feature flags on the **voice agent** and the **API**; both
processes must see the same values):

    baseline      INTERVIEW_UNIFIED_THINKER=0 VOICE_LLM_SPECULATIVE_PREVIEW=0 VOICE_ADAPTIVE_ENDPOINTING=0
    unified       INTERVIEW_UNIFIED_THINKER=1 VOICE_LLM_SPECULATIVE_PREVIEW=0 VOICE_ADAPTIVE_ENDPOINTING=0
    anticipation  INTERVIEW_UNIFIED_THINKER=1 VOICE_LLM_SPECULATIVE_PREVIEW=1 VOICE_ADAPTIVE_ENDPOINTING=0
    endpointing   INTERVIEW_UNIFIED_THINKER=1 VOICE_LLM_SPECULATIVE_PREVIEW=1 VOICE_ADAPTIVE_ENDPOINTING=1

Hybrid (``INTERVIEW_TALKER_ENABLED``) stays off in every configuration.

Bound: one session of N turns per configuration (default 8), the same job,
the same frozen plan where possible, the same answer script read by the same
speaker, in the same order, in one sitting. Cost per session is roughly N
Thinker calls plus speculation (anticipation: up to 3 per turn, debounced)
plus N VieNeu syntheses; the harness counts the actual calls afterwards from
``ai_call_logs``. **Paid provider calls: do not run without authorization.**

Run:

    python scripts/eval/interviews/voice/livekit_benchmark.py check baseline
        -> prints the env to export and refuses if the current env differs

    python scripts/eval/interviews/voice/livekit_benchmark.py compare \\
        --baseline <session id> --unified <session id> \\
        --anticipation <session id> --endpointing <session id> [--json]
        -> TTFA, TTMR, STT finalization, endpointing wait, Core, TTS first
           byte, anticipation hit rate, wasted speculation, false-cut proxy,
           calls per turn, per configuration side by side

Response-quality parity is measured separately and per decision, not per
utterance: run ``scripts/eval/interviews/agent_shadow_report.py`` on the
same sessions (carried-text rate, guard rejections, action agreement).

What this cannot measure: the candidate's own playback latency (timings end
when LiveKit starts playing at the agent) and true false cuts (the proxy is
speech captured after the submit; a human listening to the recording decides
the real number).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

CONFIGS: dict[str, dict[str, str]] = {
    "baseline": {"INTERVIEW_UNIFIED_THINKER": "0", "VOICE_LLM_SPECULATIVE_PREVIEW": "0", "VOICE_ADAPTIVE_ENDPOINTING": "0"},
    "unified": {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "0", "VOICE_ADAPTIVE_ENDPOINTING": "0"},
    "anticipation": {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "1", "VOICE_ADAPTIVE_ENDPOINTING": "0"},
    "endpointing": {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "1", "VOICE_ADAPTIVE_ENDPOINTING": "1"},
}
ALWAYS_OFF = {"INTERVIEW_TALKER_ENABLED": "false"}
ORDER = ("baseline", "unified", "anticipation", "endpointing")

#: (label, report key, stat) rows of the comparison table.
ROWS: tuple[tuple[str, str, str], ...] = (
    ("TTFA p50 (ms)", "ttfa_ms", "p50"),
    ("TTFA p95 (ms)", "ttfa_ms", "p95"),
    ("TTMR p50 (ms)", "ttmr_ms", "p50"),
    ("TTMR p95 (ms)", "ttmr_ms", "p95"),
    ("TTMR p99 (ms)", "ttmr_ms", "p99"),
    ("STT finalization p50 (ms)", "transcription_delay_ms", "p50"),
    ("endpointing wait p50 (ms)", "speech_end_to_submit_ms", "p50"),
    ("end-of-turn delay p50 (ms)", "end_of_turn_delay_ms", "p50"),
    ("Core p50 (ms)", "core_ms", "p50"),
    ("Core p95 (ms)", "core_ms", "p95"),
    ("reply after Core p50 (ms)", "reply_after_core_ms", "p50"),
)


def _truthy(value: str | None) -> bool:
    return str(value or "0").strip().lower() in {"1", "true", "yes", "on"}


def env_mismatches(config: str, env: dict[str, str] | None = None) -> list[str]:
    """Which of the configuration's flags the current environment does not match."""
    env = os.environ if env is None else env
    expected = {**CONFIGS[config], **ALWAYS_OFF}
    wrong: list[str] = []
    for key, value in expected.items():
        if _truthy(env.get(key)) != _truthy(value):
            wrong.append(f"{key}={env.get(key, '<unset>')} (want {value})")
    return wrong


def export_lines(config: str) -> list[str]:
    return [f"export {k}={v}" for k, v in {**CONFIGS[config], **ALWAYS_OFF}.items()]


def _metrics_module():
    path = Path(__file__).with_name("livekit_turn_metrics.py")
    spec = importlib.util.spec_from_file_location("livekit_turn_metrics", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("livekit_turn_metrics", module)
    spec.loader.exec_module(module)
    return module


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.0f}" if abs(value) >= 10 else f"{value:.2f}"
    return str(value)


def compare_table(reports: dict[str, dict[str, Any]]) -> str:
    """Side-by-side table of the §13 measurements for the configurations given."""
    names = [name for name in ORDER if name in reports]
    width = 30
    lines = [f"{'metric':{width}} " + " ".join(f"{n:>14}" for n in names)]
    for label, key, stat in ROWS:
        lines.append(f"{label:{width}} " + " ".join(f"{_fmt(reports[n][key][stat]):>14}" for n in names))
    extras: list[tuple[str, Any]] = [
        ("turns", lambda r: r["turns"]),
        ("calls / turn", lambda r: r["calls_per_turn"]),
        ("speculative calls", lambda r: r["speculative_calls"]),
        ("wasted speculation", lambda r: r["wasted_speculation"]),
        ("anticipation hit rate", lambda r: r["anticipation"]["hit_rate"]),
        ("early ack available", lambda r: f"{r['early_ack_available']}/{r['turns']}"),
        ("false-cut proxy rate", lambda r: r["false_cut_proxy"]["rate"]),
        ("core failures", lambda r: r["core_failures"]),
        ("tokens in", lambda r: r["tokens"]["in"]),
        ("tokens out", lambda r: r["tokens"]["out"]),
    ]
    for label, pick in extras:
        lines.append(f"{label:{width}} " + " ".join(f"{_fmt(pick(reports[n])):>14}" for n in names))
    tts_kinds = sorted({k for r in reports.values() for k in r["tts_ttfb_ms_by_kind"]})
    for kind in tts_kinds:
        lines.append(
            f"{'VieNeu first byte p50 [' + kind + ']':{width}} "
            + " ".join(f"{_fmt((reports[n]['tts_ttfb_ms_by_kind'].get(kind) or {}).get('p50')):>14}" for n in names)
        )
    return "\n".join(lines)


async def _compare(sessions: dict[str, str], as_json: bool) -> None:
    metrics = _metrics_module()
    reports: dict[str, dict[str, Any]] = {}
    for name, session_id in sessions.items():
        _, events, calls = await metrics.load(session_id)
        reports[name] = metrics.summarize(events, calls)
        reports[name]["session_id"] = session_id
    if as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    else:
        print(compare_table(reports))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="print the env for a configuration and verify the current env")
    check.add_argument("config", choices=ORDER)
    compare = sub.add_parser("compare", help="compare recorded sessions, one per configuration")
    for name in ORDER:
        compare.add_argument(f"--{name}", metavar="SESSION_ID")
    compare.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "check":
        print("\n".join(export_lines(args.config)))
        wrong = env_mismatches(args.config)
        if wrong:
            print("current environment does NOT match this configuration:", file=sys.stderr)
            for item in wrong:
                print("  " + item, file=sys.stderr)
            return 1
        print("current environment matches", args.config)
        return 0

    sessions = {name: getattr(args, name) for name in ORDER if getattr(args, name)}
    if not sessions:
        parser.error("give at least one --<config> SESSION_ID")
    asyncio.run(_compare(sessions, args.json))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
