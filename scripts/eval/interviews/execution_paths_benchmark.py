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
    hybrid_plain  Talker concurrently with the flag OFF: the Thinker carries no
                  line, so Luna's prediction can substitute for the speech
                  call when the Arbiter proves it equivalent. The measured
                  'hybrid' run showed Luna is never reused with Unified on
                  (thinker_has_text), so this is the configuration in which
                  the prediction half of §6.3 can earn its cost at all

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

CONFIGS = ("baseline", "unified", "anticipation", "hybrid", "hybrid_plain")


@dataclass
class Call:
    purpose: str
    model: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    status: str

    @property
    def role(self) -> str:
        """thinker | talker | evaluate | speech (a generated line) | other.

        `_generate_interviewer_line` forwards a short purpose ("confirm_abort",
        "redirect_off_topic_answer", "candidate_question_<intent>"...), while
        the probe and clarify generators let the facade derive theirs
        ("interviews.response_generation.InterviewResponseMixin._generate_probe_question").
        Both are speech. The evaluator's JSON call is its own role so a
        baseline/rule turn is not mistaken for a fallback.
        """
        purpose = self.purpose
        if purpose.endswith("agent.decide"):
            return "thinker"
        if purpose.endswith("agent.talker"):
            return "talker"
        if "_evaluate_candidate_response" in purpose:
            return "evaluate"
        if (
            ".response_generation." in purpose
            or purpose in _SHORT_LINE_PURPOSES
            or purpose.startswith("candidate_question_")
        ):
            return "speech"
        return "other"


#: Purposes `_generate_interviewer_line` is called with (turn_engine.py); a
#: line generated under any of these is a speech call.
_SHORT_LINE_PURPOSES = frozenset(
    {
        "close_candidate_qna",
        "complete_after_skip",
        "complete_interview",
        "confirm_abort",
        "end_after_repeated_give_up",
        "hard_timeout_close",
        "redirect_off_topic_answer",
        "decline_system_instruction_request",
        "coding_result_and_follow_up",
        "candidate_give_up",
    }
)


@dataclass
class Turn:
    config: str
    fixture: str
    action: str
    path: str | None
    wall_ms: float
    calls: list[Call] = field(default_factory=list)
    speculative_calls: int = 0
    speculative_ms: float = 0.0
    speculative_tokens: tuple[int, int] = (0, 0)
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
        """On-path plus speculative tokens: what the turn cost in total."""
        return (
            sum(c.input_tokens for c in self.calls) + self.speculative_tokens[0],
            sum(c.output_tokens for c in self.calls) + self.speculative_tokens[1],
        )


class BudgetExceeded(RuntimeError):
    """Raised before a call is sent once the authorized budget would be exceeded."""


class _SentCounter:
    """Counts provider calls at the moment they are sent.

    record_ai_call fires when a call *finishes*; a Talker call cancelled at
    fan-in never gets there but was still sent (and may still be billed). An
    authorized budget has to be enforced on sends, so the facade's two entry
    points are wrapped and the counter is checked before each one goes out.
    """

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.sent = 0

    def check(self) -> None:
        if self.limit is not None and self.sent >= self.limit:
            raise BudgetExceeded(f"authorized budget of {self.limit} provider calls reached")
        self.sent += 1

    def can_afford(self, worst_case: int) -> bool:
        return self.limit is None or self.sent + worst_case <= self.limit


_SENT = _SentCounter(None)


def _install_budget(limit: int | None) -> None:
    """Wrap both facade entry points so every send is counted and capped."""
    global _SENT
    _SENT = _SentCounter(limit)
    from src.modules.ai import facade
    from src.modules.interviews.agents.interviewer import response_generation, talker, thinker

    from src.core import pipeline_events

    real_tools, real_text = facade.generate_tool_calls, facade.generate_text

    # The facade derives a missing `purpose` from its caller's frame. Wrapped,
    # that frame is this wrapper, and every speech call was filed under
    # "execution_paths_benchmark.text" -- which is how the hybrid_plain run
    # reported fallback=0 while gpt-4o-mini showed 19 calls for 10 turns. The
    # wrapper's caller *is* the real caller, so deriving the purpose here, one
    # frame up, yields the exact string production would.
    async def tools(**kw):
        _SENT.check()
        kw.setdefault("purpose", pipeline_events.caller_purpose(2))
        return await real_tools(**kw)

    async def text(**kw):
        _SENT.check()
        kw.setdefault("purpose", pipeline_events.caller_purpose(2))
        return await real_text(**kw)

    for module in (facade, thinker, talker):
        module.generate_tool_calls = tools  # type: ignore[assignment]
    for module in (facade, response_generation):
        module.generate_text = text  # type: ignore[assignment]


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
    os.environ["INTERVIEW_UNIFIED_THINKER"] = "0" if config in ("baseline", "hybrid_plain") else "1"
    object.__setattr__(settings, "interview_talker_enabled", config in ("hybrid", "hybrid_plain"))
    if config in ("hybrid", "hybrid_plain") and not settings.interview_talker_model:
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


async def _speculate(fixture: dict[str, Any], sink: _CallSink) -> tuple[dict | None, int, float, tuple[int, int]]:
    """Run the Unified decider as a preview would, on the exact final text.

    Returns the decision record a validated preview would carry, how many calls
    it cost, how long it took, and its tokens. All three are charged to the turn:
    the on-path figure for Anticipation is only meaningful next to the
    speculation it depends on, and in a real room that speculation must finish
    before the candidate stops speaking or the turn falls back to Unified. The
    harness also speculates on the exact final text, so its hit rate is 100% by
    construction -- a ceiling, never an expectation.
    """
    from src.modules.interviews.agents.interviewer.menu import build_menu
    from src.modules.interviews.agents.interviewer.pacing import pacing_block
    from src.modules.interviews.agents.interviewer.thinker import decide_by_model, is_violation
    from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage
    from src.modules.interviews.core.conversation.turn_context import build_turn_context

    before = len(sink.calls)
    started = time.perf_counter()

    def charged(record: dict | None) -> tuple[dict | None, int, float, tuple[int, int]]:
        spent = sink.calls[before:]
        return (
            record,
            len(spent),
            round((time.perf_counter() - started) * 1000, 1),
            (sum(c.input_tokens for c in spent), sum(c.output_tokens for c in spent)),
        )

    state = _session_state(fixture, InterviewStage.DEEP_DIVE)
    ctx = build_turn_context(
        CandidateTurnInput(session_id="bench", turn_index=3, text_content=fixture["answer"], modality="VOICE"),
        state,
    )
    menu = build_menu(ctx, memory_enabled=False)
    try:
        decision = await decide_by_model(ctx, allowed_actions=menu.allowed, pacing=pacing_block(ctx), unified=True)
    except Exception:  # noqa: BLE001 - a failed speculation is just "no preview"
        return charged(None)
    if is_violation(decision) is None and decision.action in menu.allowed:
        return charged(decision.as_record())
    return charged(None)


async def _run_turn(config: str, fixture: dict[str, Any], sink: _CallSink) -> Turn:
    from src.modules.interviews.agents.interviewer import graph as turn_graph
    from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
    from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage

    previous = _set_flags(config)
    try:
        preview, speculative, speculative_ms, speculative_tokens = (None, 0, 0.0, (0, 0))
        if config == "anticipation":
            preview, speculative, speculative_ms, speculative_tokens = await _speculate(fixture, sink)

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
            speculative_ms=speculative_ms,
            speculative_tokens=speculative_tokens,
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
        all_calls = [c for t in ok for c in t.calls]
        by_role_ms = {
            role: _percentile([c.duration_ms for c in all_calls if c.role == role], 50)
            for role in ("thinker", "talker", "evaluate", "speech")
        }
        tokens_by_model: dict[str, dict[str, int]] = {}
        for c in all_calls:
            slot = tokens_by_model.setdefault(c.model or "?", {"in": 0, "out": 0, "calls": 0})
            slot["in"] += c.input_tokens
            slot["out"] += c.output_tokens
            slot["calls"] += 1
        with_talker = [t for t in ok if t.talker_outcome and t.talker_outcome != "no_talker"]
        n_talker = len(with_talker) or 1
        out[config] = {
            "turns": len(rows),
            "errors": len(rows) - len(ok),
            "calls_per_turn_total": round(sum(t.total_calls for t in ok) / len(ok), 2) if ok else None,
            "calls_per_turn_on_path": round(sum(len(t.calls) for t in ok) / len(ok), 2) if ok else None,
            "speculative_calls": sum(t.speculative_calls for t in ok),
            # Wall time of the speculation itself. In a room it overlaps the
            # candidate's speech; here it ran before the turn and is reported
            # beside the on-path number so neither can be read alone.
            "speculation_p50_ms": _percentile([t.speculative_ms for t in ok if t.speculative_calls], 50),
            "calls_by_model": by_model,
            "p50_ms": _percentile(walls, 50),
            "p95_ms": _percentile(walls, 95),
            "paths": {p: sum(1 for t in ok if t.path == p) for p in {t.path for t in ok}},
            "carried_text": sum(1 for t in ok if t.carried_text),
            "guard_rejected": sum(1 for t in ok if t.rejected),
            "fallback_rate": round(sum(1 for t in ok if t.rejected) / len(ok), 3) if ok else None,
            "talker_outcomes": {o: sum(1 for t in ok if t.talker_outcome == o) for o in {t.talker_outcome for t in ok if t.talker_outcome}},
            "early_ack_available_p50_ms": _percentile(early, 50),
            # Per-role p50 of the provider's own duration_ms (facade timer).
            "latency_by_role_p50_ms": by_role_ms,
            # Speech calls are only ever made after a guard rejected the carried
            # line (or on baseline, where there is no carried line).
            "fallback_speech_calls": sum(1 for c in all_calls if c.role == "speech"),
            "unclassified_calls": sum(1 for c in all_calls if c.role == "other"),
            "tokens_by_model": tokens_by_model,
            # Luna match rate of §13, and why it missed, as separate rates over
            # turns where a Talker output existed.
            "talker_reuse_rate": round(sum(1 for t in with_talker if t.talker_outcome == "reused") / n_talker, 3) if with_talker else None,
            "action_mismatch_rate": round(sum(1 for t in with_talker if t.talker_outcome == "action_mismatch") / n_talker, 3) if with_talker else None,
            "target_mismatch_rate": round(sum(1 for t in with_talker if t.talker_outcome == "target_mismatch") / n_talker, 3) if with_talker else None,
            "talker_guard_rejected_rate": round(sum(1 for t in with_talker if str(t.talker_outcome).startswith("guard:")) / n_talker, 3) if with_talker else None,
            "early_ack_rate": round(len(early) / len(ok), 3) if ok else None,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }
    return out


def render(report: dict[str, Any], turns: list[Turn]) -> str:
    head = f"{'config':<13}{'turns':<7}{'calls/turn':<12}{'(spec)':<8}{'p50 ms':<10}{'p95 ms':<10}{'spec p50':<10}{'carried':<9}{'rejected':<10}{'early p50':<11}tokens in/out"
    lines = [head, "-" * len(head)]
    for config in CONFIGS:
        r = report.get(config)
        if not r:
            continue
        lines.append(
            f"{config:<13}{r['turns']:<7}{str(r['calls_per_turn_total']):<12}{r['speculative_calls']:<8}"
            f"{str(r['p50_ms']):<10}{str(r['p95_ms']):<10}{str(r['speculation_p50_ms']):<10}{r['carried_text']:<9}{r['guard_rejected']:<10}"
            f"{str(r['early_ack_available_p50_ms']):<11}{r['tokens_in']}/{r['tokens_out']}"
        )
    lines.append("")
    lines.append("per config: role p50 (thinker/talker/evaluate/speech) · fallback speech calls · unclassified · talker reuse/action-miss/target-miss · tokens by model")
    for config in CONFIGS:
        r = report.get(config)
        if not r:
            continue
        roles = r["latency_by_role_p50_ms"]
        toks = "  ".join(f"{m}: {v['calls']} calls {v['in']}/{v['out']}" for m, v in r["tokens_by_model"].items())
        lines.append(
            f"  {config:<13}roles={roles['thinker']}/{roles['talker']}/{roles['evaluate']}/{roles['speech']} ms  "
            f"fallback={r['fallback_speech_calls']} unclassified={r['unclassified_calls']}  reuse={r['talker_reuse_rate']} "
            f"action-miss={r['action_mismatch_rate']} target-miss={r['target_mismatch_rate']} "
            f"talker-guard={r['talker_guard_rejected_rate']}  |  {toks}"
        )
    lines.append("")
    lines.append("not measured here: TTFA / TTMR (need a LiveKit room), response-quality parity (shadow report).")
    lines.append("'early p50' = ms from turn start until the Talker's guarded line was AVAILABLE; it is not TTFA.")
    lines.append("'spec p50' = wall time of the speculative call itself (anticipation only). Read p50 and spec p50 together:")
    lines.append("  the on-path number is what remains AFTER speculation has landed, and the harness forces a 100% hit rate.")
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


WORST_CASE_CALLS = {"baseline": 2, "unified": 2, "anticipation": 3, "hybrid": 3, "hybrid_plain": 3}


async def _main_async(turns: int, configs: list[str], stub: bool, max_calls: int | None) -> int:
    from src.core import pipeline_events

    if stub:
        _install_stub()
    # The budget wraps whatever generate_* is at this point (real or stub), so
    # it counts sends in both modes and the cap can be proven for free.
    _install_budget(max_calls)
    sink = _CallSink()
    pipeline_events.record_ai_call = sink  # type: ignore[assignment]
    results: list[Turn] = []
    truncated = None
    try:
        for config in configs:
            for index in range(turns):
                if not _SENT.can_afford(WORST_CASE_CALLS[config]):
                    truncated = f"stopped before {config} turn {index + 1}: {_SENT.sent} sent, next could exceed {max_calls}"
                    raise StopIteration
                results.append(await _run_turn(config, FIXTURES[index % len(FIXTURES)], sink))
    except StopIteration:
        pass
    except BudgetExceeded as exc:
        truncated = str(exc)
    report = summarize(results)
    report["_budget"] = {"max_calls": max_calls, "sent": _SENT.sent, "truncated": truncated}
    if truncated:
        print(f"!! BUDGET: {truncated}\n")
    print(render(report, results))
    print(f"budget: sent={_SENT.sent} cap={max_calls}")
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--turns", type=int, default=10, help="turns per configuration (1-50)")
    parser.add_argument("--configs", default=",".join(CONFIGS), help="comma-separated subset of " + ",".join(CONFIGS))
    parser.add_argument("--stub", action="store_true", help="instant fake provider: checks the plumbing, spends nothing")
    parser.add_argument("--max-calls", type=int, default=None,
                        help="hard cap on provider calls SENT; the run stops before a turn that could exceed it")
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
        worst = args.turns * sum({"baseline": 2, "unified": 2, "anticipation": 3, "hybrid": 3, "hybrid_plain": 3}[c] for c in configs)
        print(f"running {args.turns} turns x {configs} — up to ~{worst} paid provider calls\n")
    return asyncio.run(_main_async(args.turns, configs, args.stub, args.max_calls))


if __name__ == "__main__":
    raise SystemExit(main())
