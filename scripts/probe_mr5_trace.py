"""Read the reparse's own trace for exactly one match, to see where evidence is lost.

Under MR5 on jobscan_0986, adding ONE valid evidence record for requirement A
made requirement B lose the only evidence it had - deterministically, with no
LLM call. Two hypotheses were tested against the trace history and both failed:
the `evidence_complete` early return has never fired once in the whole log, and
the fallback evidence id is a pure hash of (document, span, text), so it cannot
drift when unrelated records are added.

Rather than search a 100 MB log, this records the file offset before each match
and reads only the bytes that match appended, so every event is attributable.
It prints what the recovery pass actually recovered, per requirement, for the
control and for the variant that adds the record.
"""
from __future__ import annotations

import asyncio
import copy
import csv
import hashlib
import json
import os
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
TRACE = "/app/trace-logs/matching/trace.jsonl"
PAIR = "jobscan_0986"
FOCUS = "req-llm-002"

WANTED = {"targeted_reparse", "targeted_reparse_skipped", "targeted_reparse_completed"}


def tail_events(offset: int) -> tuple[list[dict], int]:
    with open(TRACE, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
        new_offset = fh.tell()
    events = []
    for raw in chunk.decode("utf-8", "replace").splitlines():
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("event") in WANTED:
            events.append(d)
    return events, new_offset


def record_for(parsed, raw_text, line):
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


def show(tag: str, body: dict, events: list[dict]) -> None:
    obs = observed(body)
    focus = next((r for r in body.get("requirementResults") or []
                  if r.get("requirementId") == FOCUS), {})
    print(f"\n===== {tag} =====")
    print(f"coverage={obs['coverage']}  eligibility={obs['eligibility']}  "
          f"{FOCUS}: {focus.get('status')}/{len(focus.get('evidenceRefs') or [])}ev "
          f"({focus.get('reasonCode')})")
    for e in events:
        name = e.get("event")
        if name == "targeted_reparse_skipped":
            print(f"  SKIPPED: {e.get('skipped_reason')}")
        elif name == "targeted_reparse":
            print(f"  status={e.get('status')}  recovered={e.get('recovered_count')}  "
                  f"canonical_evidence={e.get('canonical_evidence_count')}  "
                  f"llm={e.get('llm_search_status')}")
            for rec in e.get("recovered_by_requirement") or []:
                print(f"    alias  {rec.get('requirement_id'):<16} "
                      f"aliases={rec.get('matched_aliases')} refs={len(rec.get('evidence_refs') or [])}")
            for rec in e.get("llm_recovered_by_requirement") or []:
                print(f"    llm    {rec.get('requirement_id'):<16} refs={rec.get('evidence_refs')}")
        elif name == "targeted_reparse_completed":
            print(f"  alias_recovered={e.get('alias_recovered_count')}  "
                  f"llm_requested={e.get('llm_requested_count')}  "
                  f"llm_recovered={e.get('llm_recovered_count')}  "
                  f"unresolved={e.get('unresolved_count')}")


async def main() -> None:
    cp = json.loads(open(f"{BASE}/checkpoint.json", encoding="utf-8").read())
    with open(f"{BASE}/matching_results.csv", encoding="utf-8-sig", newline="") as fh:
        row = next(r for r in csv.DictReader(fh) if r["pair_id"] == PAIR)
    cv_id = cp["cv_ids"][row["cv_id"]]
    jd_id = cp["jd_ids"][f"published:{row['jd_id']}"]
    labels = await requirement_labels(jd_id)
    line = labels.get("req-llm-001") or next(iter(labels.values()))
    print(f"{PAIR}  cv={row['cv_id']}  jd={row['jd_id']}")
    print(f"injected line: {line!r}")

    session = requests.Session()
    session.headers["User-Agent"] = "probe-mr5-trace/1.0"
    login(session)

    def v_control(parsed, raw):
        return parsed, raw

    def v_record(parsed, raw):
        p = copy.deepcopy(parsed)
        p["evidence"] = (p.get("evidence") or []) + [record_for(p, raw, line)]
        return p, raw

    for tag, fn in (("CONTROL", v_control), ("+ evidence record", v_record)):
        offset = os.path.getsize(TRACE)
        clone = await clone_cv(cv_id, fn)
        try:
            code, body = match(session, clone, jd_id)
            if code != 200:
                print(f"{tag}: HTTP {code} {str(body)[:200]}")
                continue
            events, _ = tail_events(offset)
            show(tag, body, events)
        finally:
            await drop_clone("user_cvs", clone)


if __name__ == "__main__":
    asyncio.run(main())
