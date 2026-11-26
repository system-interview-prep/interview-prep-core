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


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pure: the numbers of the report from decision summaries (payload of agent_decision)."""
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
    out = []
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
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
