"""Human-speaker LiveKit benchmark: prepare, then report, under a hard call budget.

The §13 pilot drove a synthetic candidate (``livekit_pilot.py``). Its audio is
easier for STT than a person, its pauses are a TTS engine's, and its answers
kept the interview in the warm-up (47 of 53 live decisions were
``continue_warm_up``), so it never exercised the Unified Thinker's probe,
clarify or redirect lines. This is the protocol for the measurement that can
settle what the pilot could not: a person speaking, false cuts labelled by a
listener, and the commit anchor compared.

    # 1. before the session: the running containers carry the configuration,
    #    the hard budgets are set, and the checklist is printed
    uv run python -m scripts.eval.interviews.voice.livekit_human_benchmark prepare --config unified

    # 2. the person runs one voice interview through the product (8 answers,
    #    see the checklist), then:
    uv run python -m scripts.eval.interviews.voice.livekit_human_benchmark report <session-id> \\
        --config unified --budget-calls 40 --out human_unified.json

    # 3. a listener fills `human_labels` in the record (false_cut / interrupted
    #    per turn) from the recording; `report --labels human_unified.json`
    #    recomputes the false-cut rate from those labels.

No provider call is made by this script. The session's calls are counted
from ``ai_call_logs`` and compared with the budget; the runtime itself
refuses anything past ``INTERVIEW_TURN_CALL_BUDGET`` /
``INTERVIEW_SESSION_CALL_BUDGET`` (src/modules/ai/budget.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONFIGS = ("baseline", "unified", "unified_speech_end", "unified_no_nudge")

#: Per configuration: the flags the containers must carry. The first two are
#: the pilot's; the last two are the two changes this profile proposes,
#: each on its own so a false cut can be traced to one of them.
FLAGS: dict[str, dict[str, str]] = {
    "baseline": {"INTERVIEW_UNIFIED_THINKER": "0", "VOICE_LLM_SPECULATIVE_PREVIEW": "0",
                 "VOICE_ADAPTIVE_ENDPOINTING": "0", "VOICE_ANSWER_COMMIT_ANCHOR": "transcript",
                 "VOICE_ANSWER_PAUSE_NUDGE_ENABLED": "1"},
    "unified": {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "0",
                "VOICE_ADAPTIVE_ENDPOINTING": "0", "VOICE_ANSWER_COMMIT_ANCHOR": "transcript",
                "VOICE_ANSWER_PAUSE_NUDGE_ENABLED": "1"},
    "unified_speech_end": {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "0",
                           "VOICE_ADAPTIVE_ENDPOINTING": "0", "VOICE_ANSWER_COMMIT_ANCHOR": "speech_end",
                           "VOICE_ANSWER_PAUSE_NUDGE_ENABLED": "1"},
    "unified_no_nudge": {"INTERVIEW_UNIFIED_THINKER": "1", "VOICE_LLM_SPECULATIVE_PREVIEW": "0",
                         "VOICE_ADAPTIVE_ENDPOINTING": "0", "VOICE_ANSWER_COMMIT_ANCHOR": "transcript",
                         "VOICE_ANSWER_PAUSE_NUDGE_ENABLED": "0"},
}
ALWAYS = {"INTERVIEW_TALKER_ENABLED": "false", "INTERVIEW_AGENT_MODE": "live"}

CHECKLIST = (
    "One person, one quiet room, a headset (no speaker echo); the same person for every configuration.",
    "Eight answers. The FIRST answer is a complete self-introduction: name, current role, years of "
    "experience, the last project, and why this position -- so the interview leaves the warm-up "
    "(the pilot never did) and the probe / clarify lines of the Unified Thinker are exercised.",
    "Answers 2-8: answer the question asked, 20-40 seconds each, and include at least two deliberate "
    "thinking pauses of 1.5-2.5 s inside one answer -- these are what the commit anchor must survive.",
    "Do not talk over the interviewer except once, on purpose, during answer 6 (one deliberate barge-in).",
    "Record the session audio on the client side (the listener needs it to label false cuts).",
    "Stop at the eighth answer; close the session from the product; wait ten seconds; then run `report`.",
)

#: What a listener fills per turn, from the recording.
LABEL_FIELDS = {
    "false_cut": None,        # true: the interviewer replied while the person was still mid-answer
    "cut_pause_s": None,      # the pause (seconds) that was cut, if any
    "interrupted_filler": None,  # true: the nudge/filler was audibly cut by the reply (expected, not a defect)
    "reply_overlapped_speech": None,  # true: the reply started over the person's speech
    "notes": "",
}


def env_mismatches(config: str, env: dict[str, str]) -> list[str]:
    """Flags the running container does not carry as the configuration needs them."""
    expected = {**FLAGS[config], **ALWAYS}
    out = []
    for key, want in expected.items():
        have = env.get(key)
        if _norm(have) != _norm(want):
            out.append(f"{key}: have {have!r}, need {want!r}")
    return out


def _norm(value: str | None) -> str:
    v = (value or "").strip().lower()
    return {"true": "1", "yes": "1", "on": "1", "false": "0", "no": "0", "off": "0", "": "0"}.get(v, v)


def budget_check(calls: int, budget: int) -> dict[str, Any]:
    return {"calls": calls, "budget": budget, "within": calls <= budget, "over_by": max(0, calls - budget)}


def labels_template(per_turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"turn_index": t.get("turn_index"), "words": t.get("words"),
             "speech_end_to_submit_ms": t.get("speech_end_to_submit_ms"), **LABEL_FIELDS} for t in per_turn]


def labelled_false_cuts(labels: list[dict[str, Any]]) -> dict[str, Any]:
    """The human-labelled false-cut rate, which the proxy in the metrics cannot give."""
    judged = [l for l in labels if l.get("false_cut") in (True, False)]
    cuts = [l for l in judged if l.get("false_cut") is True]
    pauses = sorted(float(l["cut_pause_s"]) for l in cuts if isinstance(l.get("cut_pause_s"), (int, float)))
    return {
        "labelled": len(judged), "false_cuts": len(cuts),
        "rate": round(len(cuts) / len(judged), 3) if judged else None,
        "shortest_cut_pause_s": pauses[0] if pauses else None,
        "overlapped_replies": sum(1 for l in judged if l.get("reply_overlapped_speech") is True),
    }


def render_checklist(config: str) -> str:
    lines = [f"Human-speaker benchmark -- configuration `{config}`", "flags the containers must carry:"]
    for key, value in {**FLAGS[config], **ALWAYS}.items():
        lines.append(f"  {key}={value}")
    lines.append("protocol:")
    for i, item in enumerate(CHECKLIST, 1):
        lines.append(f"  {i}. {item}")
    return "\n".join(lines)


# --------------------------------------------------------------------- run


def _pilot_module():
    from scripts.eval.interviews.voice import livekit_pilot

    return livekit_pilot


def _metrics_module():
    from scripts.eval.interviews.voice import livekit_turn_metrics

    return livekit_turn_metrics


def prepare(args: argparse.Namespace) -> int:
    print(render_checklist(args.config))
    flags = _pilot_module().container_flags(args.compose_file)
    problems = []
    for service, env in flags.items():
        if "error" in env:
            problems.append(f"{service}: {env['error']}")
            continue
        for mismatch in env_mismatches(args.config, env):
            problems.append(f"{service}: {mismatch}")
    print("containers:", json.dumps(flags, ensure_ascii=False))
    if problems:
        print("NOT READY:")
        for p in problems:
            print("  " + p)
        return 1
    print("READY: flags match; budget caps are enforced by the runtime (INTERVIEW_TURN_CALL_BUDGET / "
          "INTERVIEW_SESSION_CALL_BUDGET).")
    return 0


async def report(args: argparse.Namespace) -> int:
    metrics = _metrics_module()
    session_id, events, calls = await metrics.load(args.session)
    summary = metrics.summarize(events, calls)
    record: dict[str, Any] = {
        "benchmark": "human_speaker", "config": args.config, "session_id": session_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "container_flags": _pilot_module().container_flags(args.compose_file),
        "budget": budget_check(len(calls), args.budget_calls),
        "models": sorted({str(c.get("model")) for c in calls if c.get("model")}),
        "purposes": sorted({str(c.get("purpose")) for c in calls}),
        "metrics": summary,
        "human_labels": labels_template(summary["per_turn"]),
        "note": "one person, one session: a pilot measurement, not a P95",
    }
    if args.labels:
        labelled = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        record["human_labels"] = labelled.get("human_labels") or []
        record["labelled_false_cuts"] = labelled_false_cuts(record["human_labels"])
    Path(args.out).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"session: {session_id}")
    print(metrics.render(summary))
    print("budget:", record["budget"])
    if "labelled_false_cuts" in record:
        print("labelled false cuts:", record["labelled_false_cuts"])
    print(f"record: {args.out}")
    return 0 if record["budget"]["within"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="print the checklist and verify the running containers' flags")
    p.add_argument("--config", required=True, choices=CONFIGS)
    p.add_argument("--compose-file", action="append", default=["docker-compose.yml"])
    r = sub.add_parser("report", help="summarize a finished session and write the record with a label template")
    r.add_argument("session")
    r.add_argument("--config", required=True, choices=CONFIGS)
    r.add_argument("--budget-calls", type=int, default=40)
    r.add_argument("--compose-file", action="append", default=["docker-compose.yml"])
    r.add_argument("--labels", help="a record whose human_labels were filled by a listener")
    r.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        return prepare(args)
    return asyncio.run(report(args))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
