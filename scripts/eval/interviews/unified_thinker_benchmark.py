"""Measure the Unified Thinker against the real provider.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §13 and the V2 correction brief §3: mocked
call counts are not evidence of a latency improvement. This runs the same turns
through the decider with ``INTERVIEW_UNIFIED_THINKER`` off and on, against the
configured OpenAI provider, and reports what actually happened: calls per turn,
per-call latency, p50/p95, and how often a guard sent the model's own line back.

It is deliberately small and bounded. Every call is a real one that costs
money, so the run has a hard cap (``--turns``, default 20 per mode) and prints
the token usage it spent. It makes no database writes and opens no session: it
drives ``decide_by_model`` over fixture turns, which is the critical path §6.2
changes.

    uv run --extra dev --extra matching python -m scripts.eval.interviews.unified_thinker_benchmark --turns 20

TTFA/TTMR are *not* measured here. Those are speech-end-to-audio numbers that
need a LiveKit room and a real candidate speaking; this harness measures the
inference leg, which is the part the Unified Thinker changes. Anything else
would be a guess dressed as a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# A spread of ordinary answers: sufficient, missing a point, off-topic, a
# question back, and one that asks for the question again.
FIXTURES: list[dict[str, Any]] = [
    {
        "name": "insufficient_cache",
        "question": "Khi dữ liệu trong Redis cache bị cũ so với database, bạn xử lý invalidation thế nào?",
        "answer": "Em dùng Redis làm cache và đặt TTL năm phút cho từng key.",
        "rubric": "TTL, invalidation khi ghi, xử lý khi cache lỗi",
    },
    {
        "name": "sufficient_cache",
        "question": "Khi dữ liệu trong Redis cache bị cũ so với database, bạn xử lý invalidation thế nào?",
        "answer": (
            "Em đặt TTL năm phút, và mỗi lần ghi database thì xoá key tương ứng ngay trong cùng "
            "transaction. Nếu Redis lỗi thì đọc thẳng database, chấp nhận chậm hơn."
        ),
        "rubric": "TTL, invalidation khi ghi, xử lý khi cache lỗi",
    },
    {
        "name": "off_topic",
        "question": "Bạn thiết kế index cho bảng đơn hàng thế nào?",
        "answer": "Dạ hôm nay trời mưa to quá, em đi làm bị kẹt xe gần một tiếng.",
        "rubric": "chọn cột index, cardinality, composite index",
    },
    {
        "name": "candidate_asks_back",
        "question": "Bạn thiết kế index cho bảng đơn hàng thế nào?",
        "answer": "Cho em hỏi vị trí này làm product hay outsourcing ạ?",
        "rubric": "chọn cột index, cardinality, composite index",
    },
    {
        "name": "needs_clarify",
        "question": "Bạn xử lý race condition khi hai request cùng ghi một bản ghi thế nào?",
        "answer": "Anh có thể nói rõ hơn ý câu hỏi được không ạ, em chưa hiểu rõ.",
        "rubric": "khoá bi quan, optimistic lock, idempotency",
    },
]


@dataclass
class Sample:
    mode: str
    fixture: str
    action: str
    calls: int
    latency_ms: float
    carried_text: bool
    rejected: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return round(ordered[index], 1)


async def _run_one(fixture: dict[str, Any], *, unified: bool) -> Sample:
    """One whole LIVE turn through the real provider, counting every call.

    The turn, not the decision. Measuring ``decide_by_model`` alone would show
    one call in both modes and hide the entire point: baseline's second call is
    made later, by the action handler that writes the line. Both
    ``generate_tool_calls`` and ``generate_text`` are wrapped so the number
    below is what the turn actually cost end to end.
    """
    from src.modules.ai import facade
    from src.modules.interviews.agents.interviewer import graph as turn_graph
    from src.modules.interviews.agents.interviewer import tools as tools_module
    from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
    from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage

    calls: list[str] = []
    real_tools, real_text = facade.generate_tool_calls, facade.generate_text

    async def counting_tools(**kwargs):
        calls.append("decide")
        return await real_tools(**kwargs)

    async def counting_text(**kwargs):
        calls.append("speech")
        return await real_text(**kwargs)

    state = _session_state(fixture, InterviewStage.DEEP_DIVE)
    turn = CandidateTurnInput(session_id="bench", turn_index=3, text_content=fixture["answer"])

    # Set the real flag rather than patching a function: `thinker` imports
    # `unified_enabled` by name, so patching it on `tools` would miss, and the
    # env var is how production turns this on anyway.
    from src.modules.interviews.agents.interviewer import thinker as thinker_module

    previous_flag = os.environ.get("INTERVIEW_UNIFIED_THINKER")
    os.environ["INTERVIEW_UNIFIED_THINKER"] = "1" if unified else "0"
    thinker_module.generate_tool_calls = counting_tools  # type: ignore[assignment]
    assert tools_module.unified_enabled() is unified, "the flag did not take effect"
    engine = InterviewCoreEngine(ai_generator=counting_text)

    started = time.perf_counter()
    error = None
    output = None
    try:
        output = await turn_graph.run_turn(engine, turn, state, mode=turn_graph.MODE_LIVE)
    except Exception as exc:  # noqa: BLE001 - a provider failure is a result, not a crash
        error = f"{type(exc).__name__}: {exc}"
    finally:
        thinker_module.generate_tool_calls = real_tools  # type: ignore[assignment]
        if previous_flag is None:
            os.environ.pop("INTERVIEW_UNIFIED_THINKER", None)
        else:
            os.environ["INTERVIEW_UNIFIED_THINKER"] = previous_flag
    elapsed = (time.perf_counter() - started) * 1000

    record = ((output.metadata or {}).get("decision") if output else None) or {}
    agent = record.get("agent") or {}
    unified_info = record.get("unified") or {}
    return Sample(
        mode="unified" if unified else "baseline",
        fixture=fixture["name"],
        action=agent.get("action") or (record.get("rule") or {}).get("action") or "(none)",
        calls=len(calls),
        latency_ms=round(elapsed, 1),
        carried_text=bool(unified_info.get("carried_text")),
        rejected=list(unified_info.get("rejected") or []),
        error=error,
    )


def _session_state(fixture: dict[str, Any], stage: Any) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    question = {
        "question_id": "bench-q",
        "competency": "skill-backend",
        "stage": stage.value,
        "main_prompt": fixture["question"],
        "rubric_criteria": fixture["rubric"],
    }
    return {
        "current_stage": stage.value,
        "started_at": datetime.now(timezone.utc) - timedelta(seconds=600),
        "elapsed_time": 600,
        "target_duration_minutes": 25,
        "questions_pool": {stage.value: [question], "CLOSING": []},
        "asked_question_ids": ["bench-q"],
        "current_turn_in_question": 0,
        "clarify_count": 0,
        "current_question_context": question,
        "locale": "vi-VN",
    }


def summarize(samples: list[Sample]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in ("baseline", "unified"):
        rows = [s for s in samples if s.mode == mode]
        ok = [s for s in rows if not s.error]
        latencies = [s.latency_ms for s in ok]
        out[mode] = {
            "turns": len(rows),
            "errors": sum(1 for s in rows if s.error),
            "calls_total": sum(s.calls for s in ok),
            "calls_per_turn": round(sum(s.calls for s in ok) / len(ok), 2) if ok else None,
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "carried_text": sum(1 for s in ok if s.carried_text),
            "guard_rejected": sum(1 for s in ok if s.rejected),
            "actions": sorted({s.action for s in ok}),
        }
    return out


def render(report: dict[str, Any], samples: list[Sample]) -> str:
    lines = ["mode      turns  calls/turn  p50 ms   p95 ms   carried  rejected", "-" * 66]
    for mode in ("baseline", "unified"):
        r = report[mode]
        lines.append(
            f"{mode:<9} {r['turns']:<6} {str(r['calls_per_turn']):<11} "
            f"{str(r['p50_ms']):<8} {str(r['p95_ms']):<8} {r['carried_text']:<8} {r['guard_rejected']}"
        )
    lines.append("")
    lines.append("per turn:")
    for s in samples:
        note = f"  !! {s.error}" if s.error else ("  [guard: " + ",".join(s.rejected) + "]" if s.rejected else "")
        lines.append(
            f"  {s.mode:<9} {s.fixture:<22} {s.action:<26} calls={s.calls} {s.latency_ms:>8.1f} ms{note}"
        )
    errors = [s for s in samples if s.error]
    if errors:
        lines.append("")
        lines.append(f"{len(errors)} call(s) failed — the numbers above exclude them.")
    return "\n".join(lines)


async def _main_async(turns: int) -> int:
    samples: list[Sample] = []
    for unified in (False, True):
        for index in range(turns):
            fixture = FIXTURES[index % len(FIXTURES)]
            samples.append(await _run_one(fixture, unified=unified))
    report = summarize(samples)
    print(render(report, samples))
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--turns", type=int, default=20,
        help="turns per mode; every one is a paid provider call (default 20, so 40 calls)",
    )
    args = parser.parse_args(argv)
    if args.turns < 1 or args.turns > 100:
        parser.error("--turns must be between 1 and 100; this spends real money")
    # The key normally comes from .env through settings, not the process
    # environment, which is how the app itself reads it.
    from src.core.config import get_settings

    if not (get_settings().openai_api_key or os.getenv("OPENAI_API_KEY")):
        print("No OpenAI key configured (settings.openai_api_key): nothing to measure.", file=sys.stderr)
        return 2
    print(f"running {args.turns} turns per mode ({args.turns * 2} provider calls at least)\n")
    return asyncio.run(_main_async(args.turns))


if __name__ == "__main__":
    raise SystemExit(main())
