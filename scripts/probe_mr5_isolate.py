"""Which half of the MR5 mutation makes an unrelated requirement lose evidence?

Under MR5 on jobscan_0986, req-llm-002 went from unknown with one fallback
evidence span to not_met with none, and the candidate from review_required to
ineligible - with zero LLM calls, so the path is deterministic.

MR5 changes two things at once: it appends a line to raw_text AND adds an
evidence record for that line. This runs each half alone, plus a variant whose
injected record carries text that matches nothing, to separate three
candidate causes: the longer text, the extra record, or that record's content.
"""
from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import json
import sys

import requests

sys.path.insert(0, "/app/scripts")

from metamorphic_corpus_v1 import (  # noqa: E402
    clone_cv,
    drop_clone,
    login,
    match,
    observed,
    requirement_labels,
)

BASE = "/app/trace-logs/phase1"
PAIR = "jobscan_0986"
FOCUS = "req-llm-002"


def evidence_record(parsed, raw_text, line):
    existing = parsed.get("evidence") or []
    sample = existing[0]
    start = len(raw_text) + 1
    return {
        "evidenceId": "ev-mr5-" + hashlib.sha256(line.encode()).hexdigest()[:12],
        "text": line,
        "charStart": start,
        "charEnd": start + len(line),
        "page": sample.get("page", 1),
        "section": sample.get("section"),
        "documentId": sample.get("documentId"),
        "documentSha256": sample.get("documentSha256"),
        "evidenceSource": sample.get("evidenceSource", "document"),
        "sourceBlockId": sample.get("sourceBlockId"),
        "readingOrder": max((e.get("readingOrder") or 0) for e in existing) + 1,
        "boundingBox": None,
        "sourceRequirementId": None,
    }


async def main() -> None:
    cp = json.loads(open(f"{BASE}/checkpoint.json", encoding="utf-8").read())
    with open(f"{BASE}/matching_results.csv", encoding="utf-8-sig", newline="") as fh:
        row = next(r for r in csv.DictReader(fh) if r["pair_id"] == PAIR)
    cv_id = cp["cv_ids"][row["cv_id"]]
    jd_id = cp["jd_ids"][f"published:{row['jd_id']}"]
    labels = await requirement_labels(jd_id)

    session = requests.Session()
    session.headers["User-Agent"] = "probe-mr5-isolate/1.0"
    login(session)

    # The unmet requirement MR5 aims at, and a string that matches nothing.
    line = labels.get("req-llm-001") or next(iter(labels.values()))
    noise = "Zzyzx qwghlm vorbital"

    def v_control(parsed, raw):
        return parsed, raw

    def v_text_only(parsed, raw):
        return parsed, raw + "\n" + line

    def v_record_only(parsed, raw):
        p = copy.deepcopy(parsed)
        p["evidence"] = (p.get("evidence") or []) + [evidence_record(p, raw, line)]
        return p, raw

    def v_both(parsed, raw):
        p = copy.deepcopy(parsed)
        p["evidence"] = (p.get("evidence") or []) + [evidence_record(p, raw, line)]
        return p, raw + "\n" + line

    def v_noise_record(parsed, raw):
        p = copy.deepcopy(parsed)
        p["evidence"] = (p.get("evidence") or []) + [evidence_record(p, raw, noise)]
        return p, raw + "\n" + noise

    variants = [
        ("control (no change)", v_control),
        ("raw_text only", v_text_only),
        ("evidence record only", v_record_only),
        ("both (= MR5)", v_both),
        ("record with nonsense text", v_noise_record),
    ]

    print(f"{PAIR}  cv={row['cv_id']}  jd={row['jd_id']}  focus={FOCUS}")
    print(f"{'variant':<28}{'coverage':>10}{'eligibility':>18}   {FOCUS}")
    print("-" * 86)
    for tag, fn in variants:
        clone = await clone_cv(cv_id, fn)
        try:
            code, body = match(session, clone, jd_id)
            if code != 200:
                print(f"{tag:<28}  HTTP {code} {str(body)[:120]}")
                continue
            obs = observed(body)
            focus = next((r for r in body.get("requirementResults") or []
                          if r.get("requirementId") == FOCUS), {})
            print(f"{tag:<28}{obs['coverage']:>10}{str(obs['eligibility']):>18}   "
                  f"{focus.get('status')}/{len(focus.get('evidenceRefs') or [])}ev "
                  f"({str(focus.get('reasonCode'))[:30]})")
        finally:
            await drop_clone("user_cvs", clone)


if __name__ == "__main__":
    asyncio.run(main())
