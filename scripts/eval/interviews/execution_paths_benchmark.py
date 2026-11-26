"""Compare Baseline, Unified, Unified+Anticipation and Hybrid on real turns.

V2 correction brief §4 ("Quality and performance"). Every number here is what
actually happened in-process: the same fixture turns run through the real
turn graph, with the provider either real or stubbed, and every provider call
captured at the one place they are all recorded — ``record_ai_call`` — so the
count includes the Thinker, the Talker, speculation and any fallback speech
call, tagged by model and purpose.

    uv run --extra dev --extra matching python -m scripts.eval.interviews.execution_paths_benchmark --turns 10
    uv run ... execution_paths_benchmark --turns 10 --stub      # plumbing check, zero spend

Configurations:

    baseline      LIVE, flag off: decider call + speech call
    unified       INTERVIEW_UNIFIED_THINKER=1: one inference carries the line
    anticipation  unified, with a speculative preview run on the same text
                  first (counted) and then redeemed on the Fast path (0 new)
    hybrid        unified + Talker concurrently; early ack captured

What is NOT measured, and why: TTFA and TTMR (§13) are speech-end-to-audio
numbers that need a LiveKit room, VieNeu and a speaking candidate. This
harness measures the inference leg and, for Hybrid, the wall time from turn
start to the early acknowledgment being *available* — which is not TTFA and
must not be reported as it. Response-quality parity is left to the shadow
report (`agent_shadow_report.py`); a benchmark cannot judge wording.

Every real call costs money: the run is capped and prints its token usage.
Request authorization before a paid run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from scripts.eval.interviews.unified_thinker_benchmark import FIXTURES, _percentile, _session_state

CONFIGS = ("baseline", "unified", "anticipation", "hybrid")


@dataclass
class Call:
    purpose: str
    model: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    status: str


@dataclass
class Turn:
    config: str
    fixture: str
    action: str
    path: str | None
    wall_ms: float
    calls: list[Call] = field(default_factory=list)
    speculative_calls: int = 0
    carried_text: bool = False
    rejected: list[str] = field(default_factory=list)
    talker_outcome: str | None = None
    early_ack_ms: float | None = None
    error: str | None = None

    @property
    def total_calls(self) -> int:
        return len(self.calls) + self.speculative_calls

    @property
    def tokens(self) -> tuple[int, int]:
        return sum(c.input_tokens for c in self.calls), sum(c.output_tokens for c in self.calls)


class _CallSink:
    """Collects what the facade records, which is every provider call there is."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def __call__(self, **row: Any) -> None:
        self.calls.append(
            Call(
                purpose=str(row.get("purpose") or ""),
                model=str(row.get("model") or ""),
                duration_ms=int(row.get("duration_ms") or 0),
                input_tokens=int(row.get("input_tokens") or 0),
                output_tokens=int(row.get("output_tokens") or 0),
                status=str(row.get("status") or ""),
            )
        )


def _set_flags(config: str) -> dict[str, str | None]:
    """Flip the real env/settings for one configuration; returns what to restore."""
    from src.core.config import get_settings

    settings = get_settings()
    previous = {
        "INTERVIEW_UNIFIED_THINKER": os.environ.get("INTERVIEW_UNIFIED_THINKER"),
        "talker_enabled": settings.interview_talker_enabled,
        "talker_model": settings.interview_talker_model,
    }
    os.environ["INTERVIEW_UNIFIED_THINKER"] = "0" if config == "baseline" else "1"
    object.__setattr__(settings, "interview_talker_enabled", config == "hybrid")
    if config == "hybrid" and not settings.interview_talker_model:
        object.__setattr__(settings, "interview_talker_model", "gpt-5.6-luna")
    return previous


def _restore(previous: dict[str, Any]) -> None:
    from src.core.config import get_settings

    settings = get_settings()
    if previous["INTERVIEW_UNIFIED_THINKER"] is None:
        os.environ.pop("INTERVIEW_UNIFIED_THINKER", None)
    else:
        os.environ["INTERVIEW_UNIFIED_THINKER"] = previous["INTERVIEW_UNIFIED_THINKER"]
    object.__setattr__(settings, "interview_talker_enabled", previous["talker_enabled"])
    object.__setattr__(settings, "interview_talker_model", previous["talker_model"])


async def _speculate(fixture: dict[str, Any], sink: _CallSink) -> tuple[dict | None, int]:
    """Run the Unified decider as a preview would, on the exact final text.

    Returns the decision record a validated preview would carry, and how many
    calls it cost — those are charged to the turn as speculative calls so the
    'total provider calls including speculation' number is honest.
    """
    from src.modules.interviews.agents.interviewer.menu import build_menu
    from src.modules.interviews.agents.interviewer.pacing import pacing_block
    from src.modules.interviews.agents.interviewer.thinker import decide_by_model, is_violation
    from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage
    from src.modules.interviews.core.conversation.turn_context import build_turn_context

    before = len(sink.calls)
    state = _session_state(fixture, InterviewStage.DEEP_DIVE)
    ctx = build_turn_context(
        CandidateTurnInput(session_id="bench", turn_index=3, text_content=fixture["answer"], modality="VOICE"),
        state,
    )
    menu = build_menu(ctx, memory_enabled=False)
    try:
        decision = await decide_by_model(ctx, allowed_actions=menu.allowed, pacing=pacing_block(ctx), unified=True)
    except Exception:  # noqa: BLE001 - a failed speculation is just "no preview"
        return None, len(sink.calls) - before
    if is_violation(decision) is None and decision.action in menu.allowed:
        return decision.as_record(), len(sink.calls) - before
    return None, len(sink.calls) - before


async def _run_turn(config: str, fixture: dict[str, Any], sink: _CallSink) -> Turn:
    from src.modules.interviews.agents.interviewer import graph as turn_graph
    from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
    from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage

    previous = _set_flags(config)
    try:
        preview, speculative = (None, 0)
        if config == "anticipation":
            preview, speculative = await _speculate(fixture, sink)

        state = _session_state(fixture, InterviewStage.DEEP_DIVE)
        turn_input = CandidateTurnInput(
            session_id="bench", turn_index=3, text_content=fixture["answer"], modality="VOICE",
            precomputed_decision=preview,
        )
        engine = InterviewCoreEngine()
        first_call = len(sink.calls)
        early: list[float] = []
        started = time.perf_counter()

        def on_early(_text: str) -> None:
            early.append((time.perf_counter() - started) * 1000)

        error = None
        output = None
        try:
            output = await turn_graph.run_turn(
                engine, turn_input, state, mode=turn_graph.MODE_LIVE, on_early_speech=on_early
            )
        except Exception as exc:  # noqa: BLE001 - a provider failure is a result
            error = f"{type(exc).__name__}: {exc}"
        wall = (time.perf_counter() - started) * 1000

        record = ((output.metadata or {}).get("decision") if output else None) or {}
        agent = record.get("agent") or {}
        unified = record.get("unified") or {}
        arbiter = record.get("arbiter") or {}
        return Turn(
            config=config,
            fixture=fixture["name"],
            action=agent.get("action") or (record.get("rule") or {}).get("action") or "(none)",
            path=record.get("execution_path"),
            wall_ms=round(wall, 1),
            calls=sink.calls[first_call:],
            speculative_calls=speculative,
            carried_text=bool(unified.get("carried_text")),
            rejected=list(unified.get("rejected") or []),
            talker_outcome=arbiter.get("outcome"),
            early_ack_ms=round(early[0], 1) if early else None,
            error=error,
        )
    finally:
        _restore(previous)


def summarize(turns: list[Turn]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for config in CONFIGS:
        rows = [t for t in turns if t.config == config]
        ok = [t for t in rows if not t.error]
        if not rows:
            continue
        walls = [t.wall_ms for t in ok]
        tokens_in = sum(t.tokens[0] for t in ok)
        tokens_out = sum(t.tokens[1] for t in ok)
        by_model: dict[str, int] = {}
        for t in ok:
            for c in t.calls:
                by_model[c.model or "?"] = by_model.get(c.model or "?", 0) + 1
        early = [t.early_ack_ms for t in ok if t.early_ack_ms is not None]
        out[config] = {
            "turns": len(rows),
            "errors": len(rows) - len(ok),
            "calls_per_turn_total": round(sum(t.total_calls for t in ok) / len(ok), 2) if ok else None,
            "calls_per_turn_on_path": round(sum(len(t.calls) for t in ok) / len(ok), 2) if ok else None,
            "speculative_calls": sum(t.speculative_calls for t in ok),
            "calls_by_model": by_model,
            "p50_ms": _percentile(walls, 50),
            "p95_ms": _percentile(walls, 95),
            "paths": {p: sum(1 for t in ok if t.path == p) for p in {t.path for t in ok}},
            "carried_text": sum(1 for t in ok if t.carried_text),
            "guard_rejected": sum(1 for t in ok if t.rejected),
            "fallback_rate": round(sum(1 for t in ok if t.rejected) / len(ok), 3) if ok else None,
            "talker_outcomes": {o: sum(1 for t in ok if t.talker_outcome == o) for o in {t.talker_outcome for t in ok if t.talker_outcome}},
            "early_ack_available_p50_ms": _percentile(early, 50),
            "early_ack_rate": round(len(early) / len(ok), 3) if ok else None,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }
    return out


def render(report: dict[str, Any], turns: list[Turn]) -> str:
    head = f"{'config':<13}{'turns':<7}{'calls/turn':<12}{'(spec)':<8}{'p50 ms':<10}{'p95 ms':<10}{'carried':<9}{'rejected':<10}{'early p50':<11}tokens in/out"
    lines = [head, "-" * len(head)]
    for config in CONFIGS:
        r = report.get(config)
        if not r:
            continue
        lines.append(
            f"{config:<13}{r['turns']:<7}{str(r['calls_per_turn_total']):<12}{r['speculative_calls']:<8}"
            f"{str(r['p50_ms']):<10}{str(r['p95_ms']):<10}{r['carried_text']:<9}{r['guard_rejected']:<10}"
            f"{str(r['early_ack_available_p50_ms']):<11}{r['tokens_in']}/{r['tokens_out']}"
        )
    lines.append("")
    lines.append("not measured here: TTFA / TTMR (need a LiveKit room), response-quality parity (shadow report).")
    lines.append("'early p50' = ms from turn start until the Talker's guarded line was AVAILABLE; it is not TTFA.")
    lines.append("")
    for t in turns:
        extra = []
        if t.rejected:
            extra.append("guard:" + ",".join(t.rejected))
        if t.talker_outcome:
            extra.append(f"talker:{t.talker_outcome}")
        if t.error:
            extra.append(f"!! {t.error}")
        lines.append(
            f"  {t.config:<13}{t.fixture:<22}{t.action:<26}path={str(t.path):<9}calls={t.total_calls} "
            f"{t.wall_ms:>8.1f} ms  {' '.join(extra)}"
        )
    return "\n".join(lines)


def _install_stub() -> None:
    """A provider that answers instantly, so the plumbing can be checked for free."""
    from src.modules.ai import facade
    from src.core import pipeline_events

    async def tool_calls(**kw):
        tools = kw.get("tools") or []
        name = (kw.get("tool_choice") or {}).get("name") if isinstance(kw.get("tool_choice"), dict) else None
        timer = pipeline_events.ai_call_timer(kw.get("purpose") or "stub", provider="stub", model=kw.get("model") or "stub-model")
        with timer as call:
            # Luna's short line lands before the Thinker's long structured
            # call in practice; mirror that so the early-ack path is exercised.
            await asyncio.sleep(0.005 if name == "talker_prediction" else 0.03)
            if name == "talker_prediction":
                args = {"acknowledgment": "Mình nghe rồi.",
                        "prediction": {"action": "acknowledge_and_probe", "target": "cache invalidation",
                                       "response": "Khi dữ liệu trong database đổi, em xoá key cache theo cách nào?"}}
                out = [{"name": name, "arguments": args, "call_id": "stub"}]
            else:
                probe = next((t for t in tools if t.get("name") == "acknowledge_and_probe"), None)
                args = {"kind": "gap", "missing_aspect": "cache invalidation",
                        "rationale": "stub", "evidence_refs": ["answer:sent:1"]}
                if probe and "probe_text" in probe["parameters"]["properties"]:
                    args["probe_text"] = "Khi dữ liệu trong database đổi, em xoá key cache theo cách nào?"
                    args["evaluation"] = {"covered": ["TTL"], "missing": ["cache invalidation"]}
                out = [{"name": "acknowledge_and_probe", "arguments": args, "call_id": "stub"}]
            call.done(json.dumps(out), None)
        return out

    async def text(**kw):
        timer = pipeline_events.ai_call_timer(kw.get("purpose") or "stub", provider="stub", model=kw.get("model") or "stub-model")
        with timer as call:
            await asyncio.sleep(0.01)
            call.done("Khi database đổi, em xử lý cache thế nào?", None)
        return "Khi database đổi, em xử lý cache thế nào?"

    facade.generate_tool_calls = tool_calls  # type: ignore[assignment]
    facade.generate_text = text  # type: ignore[assignment]
    # Modules that imported the names by value.
    from src.modules.interviews.agents.interviewer import talker, thinker
    from src.modules.interviews.agents.interviewer import response_generation
    thinker.generate_tool_calls = tool_calls  # type: ignore[assignment]
    talker.generate_tool_calls = tool_calls  # type: ignore[assignment]
    response_generation.generate_text = text  # type: ignore[assignment]


async def _main_async(turns: int, configs: list[str], stub: bool) -> int:
    from src.core import pipeline_events

    if stub:
        _install_stub()
    sink = _CallSink()
    pipeline_events.record_ai_call = sink  # type: ignore[assignment]
    results: list[Turn] = []
    for config in configs:
        for index in range(turns):
            results.append(await _run_turn(config, FIXTURES[index % len(FIXTURES)], sink))
    report = summarize(results)
    print(render(report, results))
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--turns", type=int, default=10, help="turns per configuration (1-50)")
    parser.add_argument("--configs", default=",".join(CONFIGS), help="comma-separated subset of " + ",".join(CONFIGS))
    parser.add_argument("--stub", action="store_true", help="instant fake provider: checks the plumbing, spends nothing")
    args = parser.parse_args(argv)
    if not 1 <= args.turns <= 50:
        parser.error("--turns must be between 1 and 50; this spends real money")
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown:
        parser.error(f"unknown config(s): {unknown}")
    if not args.stub:
        from src.core.config import get_settings

        if not (get_settings().openai_api_key or os.getenv("OPENAI_API_KEY")):
            print("No OpenAI key configured: nothing to measure (use --stub to check plumbing).", file=sys.stderr)
            return 2
        worst = args.turns * sum({"baseline": 2, "unified": 2, "anticipation": 3, "hybrid": 3}[c] for c in configs)
        print(f"running {args.turns} turns x {configs} — up to ~{worst} paid provider calls\n")
    return asyncio.run(_main_async(args.turns, configs, args.stub))


if __name__ == "__main__":
    raise SystemExit(main())
