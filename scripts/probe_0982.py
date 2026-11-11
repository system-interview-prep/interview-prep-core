"""Why does injecting an UNMEETABLE requirement rescue a rejected candidate?

jobscan_0982 under MR2: diagnostic 6.81 -> 25.302, eligibility ineligible ->
review_required, and failedMustHaveRequirements went from ["req-llm-002"] to
empty while not_met stayed at 3. The must-have did not stop failing, so its
REASON CODE must have changed - _eligibility only disqualifies on a not_met
must-have whose reason is outside _INCONCLUSIVE_ABSENCE_REASON_CODES
(currently just "skill_evidence_missing").

This dumps every requirement result either side of the mutation so the change
is read off the system rather than inferred.
"""
from __future__ import annotations

import asyncio
import json
import sys

import requests

sys.path.insert(0, "/app/scripts")

from metamorphic_corpus_v1 import (  # noqa: E402
    JD_MUTATORS,
    clone_jd,
    drop_clone,
    login,
    match,
)

CV_ID = None   # filled from the checkpoint
JD_ID = None
PAIR = "jobscan_0982"


def dump(tag: str, body: dict) -> None:
    print(f"\n===== {tag} =====")
    print(f"eligibility={body.get('eligibility')}  "
          f"diagnostic={body.get('diagnosticScore')}  "
          f"failedMustHave={body.get('failedMustHaveRequirements')}")
    warn = body.get("matchWarningCodes") or body.get("warningCodes")
    if warn:
        print(f"warnings: {warn}")
    print(f"{'requirementId':<22} {'priority':<13} {'status':<10} {'reasonCode':<34} ev")
    print("-" * 94)
    for r in body.get("requirementResults") or []:
        print(f"{str(r.get('requirementId'))[:22]:<22} "
              f"{str(r.get('priority')):<13} "
              f"{str(r.get('status')):<10} "
              f"{str(r.get('reasonCode'))[:34]:<34} "
              f"{len(r.get('evidenceRefs') or [])}")


async def main() -> None:
    import csv

    cp = json.loads(open("/tmp/phase1/checkpoint.json", encoding="utf-8").read())
    with open("/tmp/phase1/matching_results.csv", encoding="utf-8-sig", newline="") as fh:
        row = next(r for r in csv.DictReader(fh) if r["pair_id"] == PAIR)
    cv_id = cp["cv_ids"][row["cv_id"]]
    jd_id = cp["jd_ids"][f"published:{row['jd_id']}"]
    print(f"{PAIR}: cv={row['cv_id']} jd={row['jd_id']}")

    session = requests.Session()
    session.headers["User-Agent"] = "probe-0982/1.0"
    login(session)

    for tag, relation in (("CONTROL (MR0, no change)", "MR0"),
                          ("MR2 (unmeetable requirement added)", "MR2")):
        clone = await clone_jd(jd_id, JD_MUTATORS[relation])
        try:
            code, body = match(session, cv_id, clone)
            if code != 200:
                print(f"{tag}: HTTP {code} {str(body)[:200]}")
                continue
            dump(tag, body)
        finally:
            await drop_clone("job_descriptions", clone)


if __name__ == "__main__":
    asyncio.run(main())
