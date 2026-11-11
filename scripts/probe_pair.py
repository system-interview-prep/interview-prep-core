"""Dump requirement results either side of any relation, for one pair.

Used to decide whether a violation is the system misbehaving or the harness
feeding it something malformed. Reports the parsing fields the evaluator keys
off (raw_text_coverage, warning codes) alongside each requirement's status and
reason code.

    python scripts/probe_pair.py jobscan_0986 MR5
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys

import requests

sys.path.insert(0, "/app/scripts")

from metamorphic_corpus_v1 import (  # noqa: E402
    CV_MUTATORS,
    JD_MUTATORS,
    JD_SIDE,
    clone_cv,
    clone_jd,
    drop_clone,
    login,
    match,
    observed,
    requirement_labels,
)

BASE = "/app/trace-logs/phase1"
FOCUS = sys.argv[3] if len(sys.argv) > 3 else None


def dump(tag: str, body: dict) -> None:
    print(f"\n===== {tag} =====")
    obs = observed(body)
    print(f"eligibility={obs['eligibility']}  coverage={obs['coverage']}  "
          f"diagnostic={obs['diagnostic']}  profile_match={obs['profile_match']}")
    warn = body.get("matchWarningCodes") or body.get("warningCodes") or []
    if warn:
        print(f"warnings: {warn}")
    for key in ("resumeParsing", "parsing"):
        if isinstance(body.get(key), dict):
            print(f"{key}: {json.dumps(body[key], ensure_ascii=False)[:220]}")
    print(f"{'requirementId':<24} {'status':<12} {'reasonCode':<36} ev  score")
    print("-" * 92)
    for r in body.get("requirementResults") or []:
        print(f"{str(r.get('requirementId'))[:24]:<24} "
              f"{str(r.get('status')):<12} "
              f"{str(r.get('reasonCode'))[:36]:<36} "
              f"{len(r.get('evidenceRefs') or []):<3} "
              f"{r.get('score')}")
        if FOCUS and r.get("requirementId") == FOCUS:
            print(f"    evidenceRefs : {r.get('evidenceRefs')}")
            cands = r.get("retrievalCandidates") or []
            print(f"    candidates   : {len(cands)}")
            for c in cands[:8]:
                ev = c.get("evidenceId") or (c.get("evidence") or {}).get("evidenceId")
                print(f"      {str(ev)[:34]:<34} score={c.get('semanticScore')} "
                      f"method={c.get('retrievalMethod')}")


async def main() -> None:
    pair_id, relation = sys.argv[1], sys.argv[2]
    cp = json.loads(open(f"{BASE}/checkpoint.json", encoding="utf-8").read())
    with open(f"{BASE}/matching_results.csv", encoding="utf-8-sig", newline="") as fh:
        row = next(r for r in csv.DictReader(fh) if r["pair_id"] == pair_id)
    cv_id = cp["cv_ids"][row["cv_id"]]
    jd_id = cp["jd_ids"][f"published:{row['jd_id']}"]
    print(f"{pair_id}: cv={row['cv_id']} jd={row['jd_id']}  relation={relation}")

    session = requests.Session()
    session.headers["User-Agent"] = "probe-pair/1.0"
    login(session)

    control = "MR0" if relation in JD_SIDE else "MR0_CV"
    ctx: dict = {"jd_id": jd_id, "labels": await requirement_labels(jd_id), "control": {}}

    for tag, rel in ((f"CONTROL ({control})", control), (relation, relation)):
        if rel in JD_SIDE:
            mutate = JD_MUTATORS[rel]
            clone = await clone_jd(jd_id, lambda d: mutate(d, ctx))
            table, args = "job_descriptions", (cv_id, clone)
        else:
            mutate = CV_MUTATORS[rel]
            clone = await clone_cv(cv_id, lambda p, t: mutate(p, t, ctx))
            table, args = "user_cvs", (clone, jd_id)
        try:
            code, body = match(session, *args)
            if code != 200:
                print(f"{tag}: HTTP {code} {str(body)[:300]}")
                return
            dump(tag, body)
            if rel == control:
                ctx["control"] = observed(body)
        finally:
            await drop_clone(table, clone)


if __name__ == "__main__":
    asyncio.run(main())
