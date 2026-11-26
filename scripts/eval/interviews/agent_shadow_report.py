"""Shadow-mode report: how often the model decider agrees with the rule, and where not.

    uv run python scripts/eval/interviews/agent_shadow_report.py --days 14
    uv run python scripts/eval/interviews/agent_shadow_report.py --from-file records.jsonl

Reads the ``agent_decision`` rows of ``interview_pipeline_events`` (or a JSONL of
their payloads) and prints agreement by action, stage, pace and mode, the
violation and error rates, and the disagreements with the model's reason, for a
reviewer (INTERVIEW_AGENT_DECISION_PLAN_V1 §6.2, INTERVIEW_AGENT_TEST_PLAN_V1 T4.2.1).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable


def _live(records: list[dict[str, Any]]) -> dict[str, Any]:
    """LIVE turns: the model decided and no rule ran next to it, so there is no
    agreement to report -- only what the decider did: how often its action
    was executed, refused (menu violation, error → rule fallback), which
    actions it chose, and the Unified Thinker gate on those turns. Parity
    with the rule needs a SHADOW run; until then this is what live says."""
    rows = [r for r in records if str(r.get("mode")) == "live" and r.get("agent_action")]
    if not rows:
        return {"turns": 0}
    executed = [r for r in rows if r.get("executed") == "agent"]
    return {
        "turns": len(rows),
        "sessions": len({str(r.get("session_id")) for r in rows}),
        "executed_by_agent_rate": round(len(executed) / len(rows), 3),
        "fell_back_to_rule": len(rows) - len(executed),
        "menu_violation_rate": round(sum(1 for r in rows if r.get("menu_violation")) / len(rows), 4),
        "decide_error_rate": round(sum(1 for r in rows if r.get("decide_error")) / len(rows), 4),
        "by_agent_action": dict(Counter(str(r.get("agent_action")) for r in rows).most_common()),
        "by_execution_path": dict(Counter(str(r.get("execution_path") or "-") for r in rows).most_common()),
        "latency_ms_p50": sorted(int(r.get("latency_ms") or 0) for r in rows)[len(rows) // 2],
        "unified": _unified(rows),
    }


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pure: the numbers of the report from decision summaries (payload of agent_decision)."""
    records = list(records)
    rows = [r for r in records if r.get("rule_action")]
    total = len(rows)
    with_agent = [r for r in rows if r.get("agent_action")]
    agree = [r for r in with_agent if r.get("agreement") is True]

    def rate(group_key: str) -> dict[str, dict[str, Any]]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in with_agent:
            groups[str(r.get(group_key) or "-")].append(r)
        return {
            key: {
                "n": len(items),
                "agreement": round(sum(1 for i in items if i.get("agreement") is True) / len(items), 3),
            }
            for key, items in sorted(groups.items())
        }

    disagreements = [
        {
            "session_id": r.get("session_id"),
            "turn_index": r.get("turn_index"),
            "stage": r.get("stage"),
            "pace": r.get("pace"),
            "rule": r.get("rule_action"),
            "agent": r.get("agent_action"),
            "reason": r.get("agent_reason"),
        }
        for r in with_agent
        if r.get("agreement") is False
    ]
    return {
        "turns": total,
        "with_agent_decision": len(with_agent),
        "agreement": round(len(agree) / len(with_agent), 3) if with_agent else None,
        "menu_violation_rate": round(sum(1 for r in rows if r.get("menu_violation")) / total, 4) if total else None,
        "decide_error_rate": round(sum(1 for r in rows if r.get("decide_error")) / total, 4) if total else None,
        "executed_by_agent": sum(1 for r in rows if r.get("executed") == "agent"),
        "by_rule_action": rate("rule_action"),
        "by_stage": rate("stage"),
        "by_pace": rate("pace"),
        "by_mode": dict(Counter(str(r.get("mode")) for r in rows)),
        "unified": _unified(with_agent),
        "disagreements": disagreements,
        "live": _live(records),
    }


def _unified(with_agent: list[dict[str, Any]]) -> dict[str, Any]:
    """The Phase 3 exit gate, from live turns.

    "Action/response quality parity" cannot be read off a unit test: it is how
    often the one inference of §6.2 produced a line the guards accepted, and
    which guard sent back the rest. ``carried_text`` false with no rejection
    means the flag was off or the action does not speak.
    """
    seen = [r for r in with_agent if r.get("unified_carried_text") is not None]
    if not seen:
        return {"turns": 0}
    carried = [r for r in seen if r.get("unified_carried_text")]
    rejected = [r for r in seen if r.get("unified_rejected")]
    reasons: Counter = Counter()
    for row in rejected:
        for note in row.get("unified_rejected") or []:
            reasons[str(note)] += 1
    return {
        "turns": len(seen),
        # Share of decisions that spoke for themselves: one call, not two.
        "carried_text_rate": round(len(carried) / len(seen), 3),
        # Share whose text a guard refused, so the turn paid the second call.
        "rejected_rate": round(len(rejected) / len(seen), 3),
        "rejected_by": dict(reasons.most_common()),
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"turns: {report['turns']}  with agent decision: {report['with_agent_decision']}  "
        f"agreement: {report['agreement']}  violations: {report['menu_violation_rate']}  "
        f"decide errors: {report['decide_error_rate']}  executed by agent: {report['executed_by_agent']}",
        "",
    ]
    for title, key in (("by rule action", "by_rule_action"), ("by stage", "by_stage"), ("by pace", "by_pace")):
        lines.append(title)
        for name, row in report[key].items():
            lines.append(f"  {name:<28} n={row['n']:<4} agreement={row['agreement']}")
        lines.append("")
    unified = report.get("unified") or {}
    if unified.get("turns"):
        lines.append(
            f"unified thinker: n={unified['turns']}  carried its own line={unified['carried_text_rate']}  "
            f"guard rejected={unified['rejected_rate']}  {unified['rejected_by'] or ''}"
        )
        lines.append("")
    live = report.get("live") or {}
    if live.get("turns"):
        lines.append(
            f"live turns (no rule ran beside the model; parity needs a shadow run): n={live['turns']} "
            f"sessions={live['sessions']}  executed by agent={live['executed_by_agent_rate']}  "
            f"fell back to rule={live['fell_back_to_rule']}  violations={live['menu_violation_rate']}  "
            f"errors={live['decide_error_rate']}  graph p50={live['latency_ms_p50']} ms"
        )
        lines.append(f"  actions: {live['by_agent_action']}")
        lines.append(f"  paths: {live['by_execution_path']}")
        lu = live.get("unified") or {}
        if lu.get("turns"):
            lines.append(
                f"  unified thinker on live turns: n={lu['turns']}  carried its own line={lu['carried_text_rate']}  "
                f"guard rejected={lu['rejected_rate']}  {lu['rejected_by'] or ''}"
            )
        lines.append("")
    lines.append(f"disagreements ({len(report['disagreements'])}) — for the reviewer:")
    for d in report["disagreements"][:200]:
        lines.append(f"  {d['session_id']} #{d['turn_index']} [{d['stage']}/{d['pace']}] rule={d['rule']} agent={d['agent']} :: {d['reason']}")
    return "\n".join(lines)


async def _load_from_db(days: int) -> list[dict[str, Any]]:
    from sqlalchemy import text

    from src.infrastructure.database import SessionFactory

    async with SessionFactory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT session_id, turn_index, payload FROM interview_pipeline_events "
                    "WHERE event = 'agent_decision' AND created_at >= now() - make_interval(days => :d) "
                    "ORDER BY created_at"
                ),
                {"d": days},
            )
        ).mappings().all()
    from src.modules.interviews.agents.interviewer import record as agent_record

    out = []
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
        if "rule_action" not in payload and ("rule" in payload or "agent" in payload):
            # The event stores the full decision record (record.py); this
            # report reads its summary form. Before this mapping every LIVE
            # turn counted as "no agent decision" (2026-11-26).
            payload = agent_record.summary(payload)
        out.append({"session_id": str(row["session_id"]), "turn_index": row["turn_index"], **payload})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--from-file", help="JSONL of agent_decision payloads instead of the database")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    else:
        records = asyncio.run(_load_from_db(args.days))
    report = summarize(records)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
