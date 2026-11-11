"""Turn an API benchmark run into the CSV the non-circular eval harness reads.

The harness (DOC_AND_PLAN/data/eval/matching/code/eval_harness_noncircular_v1.py)
was written against the in-process harness's CSV. The API run stores raw
MatchResult JSON instead, so this maps one onto the other without changing
either. Scores are published on the 0-100 scale the harness asserts.

`resume_skill_count` is not part of MatchResult - it comes from the parsed CV,
so it is read back from user_cvs. The harness uses it only to flag degenerate
inputs, and leaving it blank would mark every pair degenerate.

Usage (inside the backend container):
    python scripts/api_results_to_eval_csv.py \
        --results <results.jsonl> --checkpoint <checkpoint.json> \
        --manifest <matching_manifest_1000.csv> --out <matching_results.csv>
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path

from sqlalchemy import text

from src.infrastructure.database import SessionFactory

FIELDNAMES = [
    "pair_id",
    "cv_id",
    "jd_id",
    "industry",
    "jobscan_match_rate",
    "jobscan_status",
    "status",
    "eligibility",
    "decision",
    "fit_band",
    "diagnostic_score",
    "suitability_score",
    "profile_match_score",
    "score_provenance",
    "requirement_count",
    "resume_skill_count",
    "unknown_requirement_count",
    "failed_must_have_count",
    "factor_scores",
    "match_warning_codes",
    "latency_ms",
    "semantic_sparse_unavailable",
    "error_code",
]


def centesimal(value):
    """MatchResult publishes 0-1; the harness asserts 0-100."""
    return "" if value is None else round(float(value) * 100, 4)


async def resume_skill_counts(cv_api_ids: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with SessionFactory() as db:
        for label, db_id in cv_api_ids.items():
            row = (
                await db.execute(
                    text("SELECT parsed_data FROM user_cvs WHERE id = :id"), {"id": db_id}
                )
            ).mappings().one_or_none()
            parsed = row["parsed_data"] if row else None
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            counts[label] = len((parsed or {}).get("skills") or [])
    return counts


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    checkpoint = json.loads(Path(args.checkpoint).read_text(encoding="utf-8"))
    skills = await resume_skill_counts(checkpoint.get("cv_ids") or {})

    manifest: dict[str, dict] = {}
    with open(args.manifest, encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            manifest[row["pair_id"]] = row

    written = skipped = 0
    seen: set[str] = set()
    with open(args.out, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
        writer.writeheader()
        for line in Path(args.results).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            res = rec.get("result") or {}
            seen.add(rec["pair_id"])
            source = manifest.get(rec["pair_id"], {})
            if rec.get("status_code") != 200:
                skipped += 1
            factors = {f.get("factor"): f for f in (res.get("factorResults") or [])}
            reqs = res.get("requirementResults") or []
            warnings = [w.get("code") for w in (res.get("warnings") or []) if isinstance(w, dict)]
            semantic = factors.get("semantic") or {}
            writer.writerow(
                {
                    "pair_id": rec["pair_id"],
                    "cv_id": rec.get("cv_id", ""),
                    "jd_id": rec.get("jd_id", ""),
                    "industry": source.get("industry", ""),
                    "jobscan_match_rate": source.get("jobscan_match_rate", ""),
                    "jobscan_status": source.get("jobscan_status", ""),
                    "status": "ok" if rec.get("status_code") == 200 else "error",
                    "eligibility": res.get("eligibility", ""),
                    "decision": res.get("decision", ""),
                    "fit_band": res.get("fitBand", ""),
                    "diagnostic_score": centesimal(res.get("diagnosticScore")),
                    "suitability_score": centesimal(res.get("suitabilityScore")),
                    # profile_match is the semantic factor: verified equal on
                    # 1000/1000 pairs of the earlier baseline.
                    "profile_match_score": centesimal(
                        res.get("profileMatchScore")
                        if res.get("profileMatchScore") is not None
                        else semantic.get("rawScore")
                    ),
                    "score_provenance": json.dumps(
                        res.get("scoreProvenance") or {}, ensure_ascii=False
                    ),
                    "requirement_count": len(reqs),
                    "resume_skill_count": skills.get(rec.get("cv_id", ""), 0),
                    "unknown_requirement_count": sum(
                        1 for r in reqs if r.get("status") == "unknown"
                    ),
                    "failed_must_have_count": len(res.get("failedMustHaveRequirements") or []),
                    "factor_scores": json.dumps(
                        {
                            name: (
                                None
                                if factor.get("rawScore") is None
                                else round(float(factor["rawScore"]) * 100, 2)
                            )
                            for name, factor in factors.items()
                        },
                        ensure_ascii=False,
                    ),
                    "match_warning_codes": "|".join(code for code in warnings if code),
                    "latency_ms": rec.get("elapsed_ms", ""),
                    "semantic_sparse_unavailable": (
                        "1" if "semantic_sparse_unavailable" in warnings else ""
                    ),
                    "error_code": "" if rec.get("status_code") == 200 else rec.get("status_code"),
                }
            )
            written += 1

    print(f"wrote {written} rows to {args.out} ({skipped} non-200)")
    missing = [pair_id for pair_id in manifest if pair_id not in seen]
    if missing:
        print(f"manifest pairs with no result: {len(missing)} (first: {missing[:3]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
