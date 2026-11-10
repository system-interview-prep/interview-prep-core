"""Diff two API benchmark runs per pair_id.

Usage (inside the backend container):
    python scripts/api_run_diff.py <before results.jsonl> <after results.jsonl>

Written to show what removing the in-memory fallback changed on the ParadeDB
path: which pairs stopped being scored 0.0 for a document that was never
searched, and whether anything else moved.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def load(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        res = rec.get("result") or {}
        sem = next(
            (f for f in (res.get("factorResults") or []) if f.get("factor") == "semantic"), {}
        )
        rows[rec["pair_id"]] = {
            "cv": rec.get("cv_id"),
            "jd": rec.get("jd_id"),
            "eligibility": res.get("eligibility"),
            "decision": res.get("decision"),
            "fit_band": res.get("fitBand"),
            "suitability": res.get("suitabilityScore"),
            "diagnostic": res.get("diagnosticScore"),
            "semantic_status": sem.get("status"),
            "semantic": sem.get("rawScore"),
            "sparse": sem.get("sparseScore"),
            "dense": sem.get("denseScore"),
            "reliability": sem.get("reliability"),
            "warnings": sorted(
                w.get("code") for w in (res.get("warnings") or []) if isinstance(w, dict)
            ),
        }
    return rows


before = load(Path(sys.argv[1]))
after = load(Path(sys.argv[2]))
shared = sorted(set(before) & set(after))
print(f"before={len(before)} after={len(after)} shared={len(shared)}\n")

for field in ("eligibility", "decision", "fit_band", "semantic_status"):
    same = sum(1 for p in shared if before[p][field] == after[p][field])
    moved = [(p, before[p][field], after[p][field]) for p in shared if before[p][field] != after[p][field]]
    print(f"  {field:<16} {same}/{len(shared)} unchanged  {moved[:3] if moved else ''}")

print("\nsparse score")
for label, rows in (("before", before), ("after", after)):
    vals = [rows[p]["sparse"] for p in shared]
    nones = sum(1 for v in vals if v is None)
    zeros = sum(1 for v in vals if v == 0.0)
    numeric = [v for v in vals if isinstance(v, (int, float))]
    mean = statistics.mean(numeric) if numeric else float("nan")
    print(f"  {label:<7} null={nones:<3} zero={zeros:<3} scored={len(numeric):<3} mean={mean:.4f}")

flipped = [p for p in shared if (before[p]["sparse"] == 0.0) and (after[p]["sparse"] is None)]
print(f"\n  pairs that went 0.0 -> null (searched nothing, no longer claimed as a zero): {len(flipped)}")
for p in flipped[:12]:
    print(f"    {p} {before[p]['cv']} x {before[p]['jd']}  semantic {before[p]['semantic']} -> {after[p]['semantic']}")

warned = [p for p in shared if "semantic_sparse_unavailable" in (after[p]["warnings"] or [])]
print(f"\n  pairs now carrying semantic_sparse_unavailable: {len(warned)}")
rel = {p: after[p]["reliability"] for p in shared if after[p]["reliability"] is not None}
if rel:
    degraded = [p for p, v in rel.items() if v < 1.0]
    print(f"  pairs with semantic reliability < 1.0: {len(degraded)}")

print("\nscores, after minus before")
for field in ("semantic", "diagnostic", "suitability"):
    diffs = [
        after[p][field] - before[p][field]
        for p in shared
        if isinstance(after[p][field], (int, float)) and isinstance(before[p][field], (int, float))
    ]
    if not diffs:
        print(f"  {field:<12} no comparable pairs")
        continue
    absd = [abs(d) for d in diffs]
    print(
        f"  {field:<12} n={len(diffs)} identical={sum(1 for d in absd if d < 1e-12)} "
        f"mean_signed={statistics.mean(diffs):+.4f} max_abs={max(absd):.4f}"
    )
