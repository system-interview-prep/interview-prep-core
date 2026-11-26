"""LiveKit turn metrics for V2 spec §13, from events a real session already stored.

No provider is called and no room is opened: this reads what the voice agent
reported to ``POST /ai/voice-lab/livekit/runtime/events`` (table
``interview_pipeline_events``) and the model calls Interview Core logged
(``ai_call_logs``) for one session, pairs them into turns, and prints the
spec's measurements with P50/P95/P99:

- **TTFA**   speech end → first audio of any utterance after the answer
             (early acknowledgment, filler, or the reply itself);
- **TTMR**   speech end → first audio of the *official* reply (question /
             closing); fillers and early acknowledgments do not count;
- **Core**   answer submitted → Interview Core answered (``core_ms``);
- **Qwen**   finalization latency (``transcription_delay_ms``) and the
             end-of-turn wait (``end_of_turn_delay_ms``) LiveKit measured;
- **VieNeu** TTS first byte per utterance kind (``ttfb_ms``), and reply
             playback after Core (``since_core_ms``);
- **Calls**  model calls per turn by purpose, speculative ones separately,
             and wasted speculation (anticipation discarded, or started and
             never reused);
- **Anticipation** hit rate = reused / (reused + discarded);
- **Early ack** availability = turns where the Talker's line played;
- **False cut (proxy)** turns where the candidate's speech arrived after
  the answer had already been submitted (``voice_late_speech_captured``).
  A true false-cut rate needs human labelling of the audio; this proxy
  counts the cases the runtime could itself see.

Timing marks come from the agent's own clock (``voice_reply_started`` and
``voice_answer_submitted`` carry ``since_speech_end_ms``), so batching and
network delay in event delivery do not distort them. What the agent cannot
see is the client's playback leg: TTFA/TTMR here end when LiveKit starts
playing the utterance at the agent, not when the candidate hears it.

Run after a real voice session, in interview-prep-core:

    docker compose exec backend python scripts/eval/interviews/voice/livekit_turn_metrics.py <session id>
    docker compose exec backend python scripts/eval/interviews/voice/livekit_turn_metrics.py latest --json

Guard rejection and response-quality parity are per decision, not per
utterance; ``scripts/eval/interviews/agent_shadow_report.py`` reports them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter
from datetime import datetime
from typing import Any

#: Utterance kinds that count as "first audio" (TTFA) after an answer.
FIRST_AUDIO_KINDS = frozenset({"early_ack", "filler", "question", "closing"})
#: Utterance kinds that are the official reply (TTMR).
REPLY_KINDS = frozenset({"question", "closing"})
#: ai_call_logs purposes that are speculation (anticipation), by substring.
SPECULATIVE_PURPOSE_MARKERS = ("preview", "anticipat", "speculat")


# ----------------------------------------------------------------- statistics


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None when there is nothing to rank."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def summary(values: list[float]) -> dict[str, Any]:
    clean = [float(v) for v in values if v is not None]
    return {
        "n": len(clean),
        "p50": percentile(clean, 50),
        "p95": percentile(clean, 95),
        "p99": percentile(clean, 99),
        "min": min(clean) if clean else None,
        "max": max(clean) if clean else None,
    }


# --------------------------------------------------------------------- turns


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _when(event: dict[str, Any]) -> datetime | None:
    value = event.get("created_at")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _is_anticipation(event: dict[str, Any]) -> bool:
    return str(event.get("event") or "").startswith("voice_anticipation_")


def split_turns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per answer: a submit, what followed it, and the speculation
    that preceded it.

    Speculation happens while the candidate is still speaking, i.e. *before*
    the submit it is about and after the previous turn's Core answer. Those
    events are carried forward to the turn they speculated about. Everything
    else before the first submit (the opening question, its TTS) belongs to
    no turn and is ignored.
    """
    ordered = sorted(events, key=lambda e: (_when(e) or datetime.min, int(e.get("id") or 0)))
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    carried: list[dict[str, Any]] = []  # speculation for the answer not yet submitted
    answered = False  # has the current turn's Core answered yet?
    for event in ordered:
        name = event.get("event")
        if name == "voice_answer_submitted":
            current = {"submitted": event, "events": [*carried, event], "turn_index": event.get("turn_index")}
            carried = []
            answered = False
            turns.append(current)
        elif _is_anticipation(event) and (current is None or answered):
            carried.append(event)
        elif current is not None:
            current["events"].append(event)
            if name in ("voice_core_completed", "voice_core_failed"):
                answered = True
    return turns


def _first(turn: dict[str, Any], name: str, **match: Any) -> dict[str, Any] | None:
    for event in turn["events"]:
        if event.get("event") != name:
            continue
        payload = _payload(event)
        if all(payload.get(k) in v if isinstance(v, (set, frozenset)) else payload.get(k) == v for k, v in match.items()):
            return event
    return None


def _count(turn: dict[str, Any], name: str) -> int:
    return sum(1 for e in turn["events"] if e.get("event") == name)


def measure_turn(turn: dict[str, Any]) -> dict[str, Any]:
    submitted = _payload(turn["submitted"])
    first_audio = _first(turn, "voice_reply_started", kind=FIRST_AUDIO_KINDS)
    reply = _first(turn, "voice_reply_started", kind=REPLY_KINDS)
    core = _first(turn, "voice_core_completed")
    failed = _first(turn, "voice_core_failed")
    early = _first(turn, "voice_early_acknowledgment")
    tts_by_kind: dict[str, list[float]] = {}
    for event in turn["events"]:
        if event.get("event") == "voice_tts_metrics":
            payload = _payload(event)
            if payload.get("ttfb_ms") is not None:
                tts_by_kind.setdefault(str(payload.get("kind") or "unknown"), []).append(float(payload["ttfb_ms"]))
    return {
        "turn_index": turn.get("turn_index"),
        "ttfa_ms": _payload(first_audio).get("since_speech_end_ms") if first_audio else None,
        "ttfa_kind": _payload(first_audio).get("kind") if first_audio else None,
        "ttmr_ms": _payload(reply).get("since_speech_end_ms") if reply else None,
        "reply_after_core_ms": _payload(reply).get("since_core_ms") if reply else None,
        "core_ms": _payload(core).get("core_ms") if core else None,
        "action": _payload(core).get("action") if core else None,
        "core_failed": failed is not None,
        "early_ack_ms": _payload(early).get("after_ms") if early else None,
        "speech_end_to_submit_ms": submitted.get("since_speech_end_ms"),
        "transcription_delay_ms": submitted.get("transcription_delay_ms"),
        "end_of_turn_delay_ms": submitted.get("end_of_turn_delay_ms"),
        "words": submitted.get("words"),
        "fragments": submitted.get("fragments"),
        "late_speech": _count(turn, "voice_late_speech_captured") > 0,
        "anticipation_reused": _count(turn, "voice_anticipation_reused"),
        "anticipation_discarded": _count(turn, "voice_anticipation_discarded"),
        "anticipation_ready": _count(turn, "voice_anticipation_ready"),
        "discard_reasons": Counter(
            str(_payload(e).get("reason") or "unknown")
            for e in turn["events"]
            if e.get("event") == "voice_anticipation_discarded"
        ),
        "tts_ttfb_ms_by_kind": tts_by_kind,
    }


# --------------------------------------------------------------------- calls


def attribute_calls(turns: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Model calls per turn: everything logged after the previous turn's Core
    answer (or the session start) up to and including this turn's Core answer.
    Speculation during the candidate's speech therefore counts against the
    turn it was speculating about."""
    bounds: list[datetime | None] = []
    for turn in turns:
        core = _first(turn, "voice_core_completed") or _first(turn, "voice_core_failed")
        bounds.append(_when(core) if core else None)
    ordered = sorted(calls, key=lambda c: _when(c) or datetime.min)
    per_turn: list[list[dict[str, Any]]] = [[] for _ in turns]
    previous: datetime | None = None
    for index, upper in enumerate(bounds):
        for call in ordered:
            at = _when(call)
            if at is None:
                continue
            if previous is not None and at <= previous:
                continue
            if upper is not None and at > upper:
                continue
            if upper is None and index < len(bounds) - 1:
                continue
            per_turn[index].append(call)
        if upper is not None:
            previous = upper
    return per_turn


def is_speculative(call: dict[str, Any]) -> bool:
    purpose = str(call.get("purpose") or "").lower()
    return any(marker in purpose for marker in SPECULATIVE_PURPOSE_MARKERS)


# ------------------------------------------------------------------- report


def summarize(events: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    turns = split_turns(events)
    measured = [measure_turn(turn) for turn in turns]
    per_turn_calls = attribute_calls(turns, calls)
    for item, turn_calls in zip(measured, per_turn_calls):
        item["calls"] = len(turn_calls)
        item["speculative_calls"] = sum(1 for c in turn_calls if is_speculative(c))
        item["purposes"] = Counter(str(c.get("purpose") or "?") for c in turn_calls)
        item["call_ms"] = [float(c.get("duration_ms") or 0) for c in turn_calls]
        item["tokens_in"] = sum(int(c.get("input_tokens") or 0) for c in turn_calls)
        item["tokens_out"] = sum(int(c.get("output_tokens") or 0) for c in turn_calls)

    reused = sum(t["anticipation_reused"] for t in measured)
    discarded = sum(t["anticipation_discarded"] for t in measured)
    speculative = sum(t.get("speculative_calls", 0) for t in measured)
    n = len(measured)
    tts: dict[str, list[float]] = {}
    for t in measured:
        for kind, values in t["tts_ttfb_ms_by_kind"].items():
            tts.setdefault(kind, []).extend(values)
    discard_reasons: Counter = Counter()
    for t in measured:
        discard_reasons.update(t["discard_reasons"])
    purposes: Counter = Counter()
    for t in measured:
        purposes.update(t.get("purposes", Counter()))

    return {
        "turns": n,
        "ttfa_ms": summary([t["ttfa_ms"] for t in measured]),
        "ttmr_ms": summary([t["ttmr_ms"] for t in measured]),
        "core_ms": summary([t["core_ms"] for t in measured]),
        "reply_after_core_ms": summary([t["reply_after_core_ms"] for t in measured]),
        "speech_end_to_submit_ms": summary([t["speech_end_to_submit_ms"] for t in measured]),
        "transcription_delay_ms": summary([t["transcription_delay_ms"] for t in measured]),
        "end_of_turn_delay_ms": summary([t["end_of_turn_delay_ms"] for t in measured]),
        "early_ack_ms": summary([t["early_ack_ms"] for t in measured]),
        "early_ack_available": sum(1 for t in measured if t["early_ack_ms"] is not None),
        "ttfa_by_kind": dict(Counter(str(t["ttfa_kind"]) for t in measured if t["ttfa_kind"])),
        "tts_ttfb_ms_by_kind": {kind: summary(values) for kind, values in tts.items()},
        "calls_per_turn": (sum(t.get("calls", 0) for t in measured) / n) if n else None,
        "speculative_calls": speculative,
        # Speculative calls that bought nothing. When the call log cannot tell
        # speculation apart (no marker in the purpose), fall back to the
        # engine's own discard count.
        "wasted_speculation": max(0, speculative - reused) if speculative else discarded,
        "anticipation": {
            "reused": reused,
            "discarded": discarded,
            "ready": sum(t["anticipation_ready"] for t in measured),
            "hit_rate": (reused / (reused + discarded)) if (reused + discarded) else None,
            "discard_reasons": dict(discard_reasons),
        },
        "false_cut_proxy": {
            "turns": sum(1 for t in measured if t["late_speech"]),
            "rate": (sum(1 for t in measured if t["late_speech"]) / n) if n else None,
        },
        "core_failures": sum(1 for t in measured if t["core_failed"]),
        "purposes": dict(purposes),
        "tokens": {
            "in": sum(t.get("tokens_in", 0) for t in measured),
            "out": sum(t.get("tokens_out", 0) for t in measured),
        },
        "per_turn": [
            {k: (dict(v) if isinstance(v, Counter) else v) for k, v in t.items() if k != "tts_ttfb_ms_by_kind"}
            for t in measured
        ],
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.0f}"
    return str(value)


def render(report: dict[str, Any]) -> str:
    lines = [f"turns: {report['turns']}"]
    rows = [
        ("TTFA (speech end → first audio)", report["ttfa_ms"]),
        ("TTMR (speech end → official reply)", report["ttmr_ms"]),
        ("speech end → submit (endpointing)", report["speech_end_to_submit_ms"]),
        ("end-of-turn delay (LiveKit)", report["end_of_turn_delay_ms"]),
        ("Qwen finalization", report["transcription_delay_ms"]),
        ("Core (submit → answer)", report["core_ms"]),
        ("reply playback after Core", report["reply_after_core_ms"]),
        ("early ack after submit", report["early_ack_ms"]),
    ]
    lines.append(f"{'metric':38} {'n':>3} {'p50':>7} {'p95':>7} {'p99':>7}")
    for label, s in rows:
        lines.append(f"{label:38} {s['n']:>3} {_fmt(s['p50']):>7} {_fmt(s['p95']):>7} {_fmt(s['p99']):>7}")
    for kind, s in sorted(report["tts_ttfb_ms_by_kind"].items()):
        lines.append(f"{'VieNeu first byte [' + kind + ']':38} {s['n']:>3} {_fmt(s['p50']):>7} {_fmt(s['p95']):>7} {_fmt(s['p99']):>7}")
    a = report["anticipation"]
    lines.append(
        f"calls/turn: {_fmt(report['calls_per_turn'])}  speculative: {report['speculative_calls']}  "
        f"wasted speculation: {report['wasted_speculation']}  tokens in/out: {report['tokens']['in']}/{report['tokens']['out']}"
    )
    lines.append(
        f"anticipation: reused {a['reused']}  discarded {a['discarded']}  hit rate "
        f"{_fmt(a['hit_rate'] * 100) + '%' if a['hit_rate'] is not None else '—'}  reasons {a['discard_reasons']}"
    )
    lines.append(
        f"early ack available: {report['early_ack_available']}/{report['turns']}  "
        f"first audio by kind: {report['ttfa_by_kind']}"
    )
    fc = report["false_cut_proxy"]
    lines.append(
        f"false-cut proxy (late speech after submit): {fc['turns']}/{report['turns']}"
        f"{'  (' + _fmt(fc['rate'] * 100) + '%)' if fc['rate'] is not None else ''}  core failures: {report['core_failures']}"
    )
    if report["purposes"]:
        lines.append("purposes: " + ", ".join(f"{k}×{v}" for k, v in sorted(report["purposes"].items())))
    return "\n".join(lines)


# ----------------------------------------------------------------------- db


async def load(session_id: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    from sqlalchemy import text

    from src.infrastructure.database import StandaloneSessionFactory

    async with StandaloneSessionFactory() as db:
        if session_id == "latest":
            session_id = await db.scalar(
                text(
                    "SELECT session_id FROM interview_pipeline_events WHERE event = 'voice_session_started' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
            if not session_id:
                raise SystemExit("no voice session has reported events yet")
        events = [
            dict(r)
            for r in (
                await db.execute(
                    text(
                        "SELECT id, event, turn_index, payload, created_at FROM interview_pipeline_events "
                        "WHERE session_id = :sid AND event LIKE 'voice_%' ORDER BY created_at, id"
                    ),
                    {"sid": session_id},
                )
            ).mappings().all()
        ]
        calls = [
            dict(r)
            for r in (
                await db.execute(
                    text(
                        "SELECT id, purpose, model, status, duration_ms, input_tokens, output_tokens, created_at "
                        "FROM ai_call_logs WHERE session_id = :sid ORDER BY created_at, id"
                    ),
                    {"sid": session_id},
                )
            ).mappings().all()
        ]
    return str(session_id), events, calls


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", help="interview session id, or 'latest'")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    session_id, events, calls = await load(args.session)
    report = summarize(events, calls)
    report["session_id"] = session_id
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"session: {session_id}")
        print(render(report))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
