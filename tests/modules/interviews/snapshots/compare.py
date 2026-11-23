"""Compare two turn snapshots (T2.1.6 / T2.D2.1 drift check).

    uv run python tests/modules/interviews/snapshots/compare.py <reference.jsonl> <candidate.jsonl>

Exit 1 on any difference outside the volatile fields. ``session_id`` is volatile
because the tests draw a fresh uuid4 per run; ``time_remaining_seconds`` because
``started_at`` is ``now()`` in most tests.
"""

from __future__ import annotations

import io
import json
import sys

VOLATILE = {"session_id", "time_remaining_seconds"}


def load(path: str) -> dict[str, list[tuple[str, dict]]]:
    out: dict[str, list[tuple[str, dict]]] = {}
    for line in io.open(path, encoding="utf-8"):
        row = json.loads(line)
        output = {k: v for k, v in row["output"].items() if k not in VOLATILE}
        output["metadata"] = {k: v for k, v in (output.get("metadata") or {}).items() if k != "decision"}
        out.setdefault(row["test"], []).append((row["turn_input"]["text_content"][:60], output))
    return out


def compare(reference: str, candidate: str) -> int:
    before, after = load(reference), load(candidate)
    missing, extra = set(before) - set(after), set(after) - set(before)
    diffs = 0
    for test in sorted(set(before) & set(after)):
        if len(before[test]) != len(after[test]):
            diffs += 1
            print(f"DIFF {test}: {len(before[test])} turns => {len(after[test])}")
            continue
        for (_, ob), (_, oa) in zip(before[test], after[test]):
            if ob != oa:
                diffs += 1
                print(f"DIFF {test}")
                for key in sorted(set(ob) | set(oa)):
                    if ob.get(key) != oa.get(key):
                        print(f"    {key}: {str(ob.get(key))[:100]!s} => {str(oa.get(key))[:100]!s}")
    for test in sorted(missing):
        print(f"MISSING {test}")
    for test in sorted(extra):
        print(f"EXTRA {test}")
    total = sum(len(v) for v in before.values())
    print(f"turns compared: {total}  diffs: {diffs}  missing: {len(missing)}  extra: {len(extra)}")
    return 1 if diffs or missing else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(compare(sys.argv[1], sys.argv[2]))
