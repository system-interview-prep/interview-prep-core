"""Metamorphic relations measured on the real corpus, through the real API.

Runs INSIDE the backend container. The matching path is the production one:
POST /api/v1/matching/match-ids, ParadeDB BM25, the same path that produced
BASELINE-B. Nothing here calls the facade in process, and nothing pins
``in_memory`` - a suite that scores with a different formula than production
tests a different system, which is the mistake that cost us BASELINE-A.

``/match-ids`` takes identifiers, not payloads, so a transformed input has to
exist as a row. Each relation clones the JD or the CV, mutates the clone's
``structured_data`` / ``parsed_data`` / ``raw_text``, matches against it, then
deletes the clone. Writing structured data directly keeps the non-deterministic
JD parser out of the measurement.

MR0 is the control: clone with NO mutation. A clone carries a new id, so its
cache key is new and the whole result is recomputed - unlike re-running the
same ids, which simply hits ``match_cache``. Any pair that moves under MR0 is
unstable on its own and is excluded from the other relations rather than being
reported as a violation.

Usage (inside the container):
    python scripts/metamorphic_corpus_v1.py --relation MR0 --limit 200 \
        --baseline /tmp/phase1/matching_results.csv \
        --checkpoint /tmp/phase1/checkpoint.json \
        --out /tmp/phase1/mr0.json
    python scripts/metamorphic_corpus_v1.py --cleanup
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path

import requests
from sqlalchemy import text

from src.infrastructure.database import SessionFactory

BASE_URL = os.environ.get("MR_BASE_URL", "http://localhost:5000").rstrip("/")

# Every clone id starts with this, so an interrupted run can always be swept up.
CLONE_PREFIX = "mrx-"

RELATIONS = ("MR1", "MR2", "MR3", "MR4", "MR5", "MR6")
SELECTABLE = ("MR0",) + RELATIONS
JD_SIDE = {"MR0", "MR1", "MR2", "MR4"}   # MR0_CV is the CV-side control


# ----------------------------------------------------------------- auth / API
def login(session: requests.Session) -> None:
    token = os.environ.get("MR_ACCESS_TOKEN", "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        return
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "").strip()
    if not email or not password:
        raise SystemExit("BOOTSTRAP_ADMIN_EMAIL/PASSWORD missing from the container environment")
    res = session.post(f"{BASE_URL}/auth/login", json={"email": email, "password": password}, timeout=30)
    res.raise_for_status()
    session.headers["Authorization"] = f"Bearer {res.json()['access_token']}"


def match(session: requests.Session, cv_id: str, jd_id: str) -> tuple[int, dict]:
    res = session.post(
        f"{BASE_URL}/api/v1/matching/match-ids",
        json={"candidateId": cv_id, "jobId": jd_id,
              "asyncProcessing": False, "recordFitHistory": False},
        timeout=300,
    )
    try:
        body = res.json()
    except ValueError:
        body = {}
    return res.status_code, body


# ------------------------------------------------------------------- cloning
async def clone_jd(src_id: str, mutate) -> str:
    """Copy a job posting, mutate the copy's requirements, return the new id."""
    new_id = CLONE_PREFIX + uuid.uuid4().hex
    async with SessionFactory() as db:
        row = (await db.execute(
            text("SELECT structured_data FROM job_descriptions WHERE id = :id"), {"id": src_id}
        )).mappings().one()
        data = copy.deepcopy(row["structured_data"])
        if isinstance(data, str):
            data = json.loads(data)
        data = mutate(data)
        # Column-by-column copy: SELECT * would collide on the primary key.
        cols = [r[0] for r in (await db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'job_descriptions' ORDER BY ordinal_position"
        ))).all()]
        assign = ", ".join(
            ":new_id" if c == "id" else
            "CAST(:sd AS jsonb)" if c == "structured_data" else c
            for c in cols
        )
        await db.execute(
            text(f"INSERT INTO job_descriptions ({', '.join(cols)}) "
                 f"SELECT {assign} FROM job_descriptions WHERE id = :src"),
            {"new_id": new_id, "sd": json.dumps(data, ensure_ascii=False), "src": src_id},
        )
        await db.commit()
    return new_id


async def clone_cv(src_id: str, mutate) -> str:
    """Copy a CV, mutate the copy's parsed data and text, return the new id."""
    new_id = CLONE_PREFIX + uuid.uuid4().hex
    async with SessionFactory() as db:
        row = (await db.execute(
            text("SELECT parsed_data, raw_text FROM user_cvs WHERE id = :id"), {"id": src_id}
        )).mappings().one()
        parsed = copy.deepcopy(row["parsed_data"])
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        parsed, raw_text = mutate(parsed, row["raw_text"] or "")
        cols = [r[0] for r in (await db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'user_cvs' ORDER BY ordinal_position"
        ))).all()]
        assign = ", ".join(
            ":new_id" if c == "id" else
            "CAST(:pd AS jsonb)" if c == "parsed_data" else
            ":rt" if c == "raw_text" else
            ":ck" if c == "checksum" else c  # (user_id, checksum) is UNIQUE
            for c in cols
        )
        await db.execute(
            text(f"INSERT INTO user_cvs ({', '.join(cols)}) "
                 f"SELECT {assign} FROM user_cvs WHERE id = :src"),
            {"new_id": new_id, "pd": json.dumps(parsed, ensure_ascii=False),
             "rt": raw_text, "ck": hashlib.sha256(new_id.encode()).hexdigest(), "src": src_id},
        )
        await db.commit()
    return new_id


async def drop_clone(table: str, clone_id: str) -> None:
    async with SessionFactory() as db:
        await db.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": clone_id})
        await db.commit()


async def sweep() -> dict:
    out = {}
    async with SessionFactory() as db:
        for table in ("job_descriptions", "user_cvs"):
            res = await db.execute(
                text(f"DELETE FROM {table} WHERE id LIKE :p"), {"p": CLONE_PREFIX + "%"}
            )
            out[table] = res.rowcount
        await db.commit()
    return out


# --------------------------------------------------------------- the mutations
def _requirements(data: dict) -> list:
    return data.get("requirements") or []


def mr0_jd(data):
    return data                                     # control: no change at all


def mr1_jd(data):
    reqs = _requirements(data)
    if len(reqs) < 2:
        raise Skip("fewer than two requirements, permutation is a no-op")
    rotated = reqs[1:] + reqs[:1]                   # deterministic permutation
    data["requirements"] = rotated
    return data


def mr2_jd(data):
    reqs = _requirements(data)
    if not reqs:
        raise Skip("no requirement to copy a shape from")
    probe = copy.deepcopy(reqs[0])
    probe["requirementId"] = "mr2-unsatisfiable"
    probe["priority"] = "preferred"
    probe["rawLabel"] = "Kinh nghiem van hanh lo phan ung hat nhan"
    for key in ("concept", "atomicConcepts", "skill", "minimumExperienceMonths",
                "operator", "threshold", "groupOperator"):
        probe.pop(key, None)
    probe["kind"] = "other"
    data["requirements"] = reqs + [probe]
    return data


def mr4_jd(data):
    reqs = _requirements(data)
    idx = next((i for i, r in enumerate(reqs) if r.get("priority") == "must_have"), None)
    if idx is None:
        raise Skip("no must_have requirement to demote")
    reqs[idx] = {**reqs[idx], "priority": "preferred"}
    data["requirements"] = reqs
    return data


def mr3_cv(parsed, raw_text):
    skills = parsed.get("skills") or []
    if not skills:
        raise Skip("CV has no skill entry to duplicate")
    dup = copy.deepcopy(skills[0])
    dup["claimId"] = str(dup.get("claimId", "claim")) + "-mr3-dup"
    parsed["skills"] = skills + [dup]
    return parsed, raw_text


def mr5_cv(parsed, raw_text):
    # Append a line that evidences something; the relation only asks that
    # coverage must not go DOWN when a CV gains content.
    return parsed, raw_text + "\nAdditional project: maintained production services."


def mr6_cv(parsed, raw_text):
    # Whitespace and case only - the token sequence is preserved.
    collapsed = re.sub(r"[ \t]+", " ", raw_text)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return parsed, collapsed.upper()


class Skip(Exception):
    """This pair cannot carry this relation; not a violation."""


def mr0_cv(parsed, raw_text):
    return parsed, raw_text                     # control for the CV side


JD_MUTATORS = {"MR0": mr0_jd, "MR1": mr1_jd, "MR2": mr2_jd, "MR4": mr4_jd}
CV_MUTATORS = {"MR0_CV": mr0_cv, "MR3": mr3_cv, "MR5": mr5_cv, "MR6": mr6_cv}


# -------------------------------------------------------------- the relations
def centesimal(v):
    return None if v is None else round(float(v) * 100, 4)


def observed(body: dict) -> dict:
    reqs = body.get("requirementResults") or []
    factors = {f.get("factor"): f for f in (body.get("factorResults") or [])}
    cov = factors.get("requirement_coverage") or {}
    return {
        "eligibility": body.get("eligibility"),
        "decision": body.get("decision"),
        "fit_band": body.get("fitBand"),
        "diagnostic": centesimal(body.get("diagnosticScore")),
        "suitability": centesimal(body.get("suitabilityScore")),
        "profile_match": centesimal(body.get("profileMatchScore")),
        "requirement_count": len(reqs),
        "not_met": sum(1 for r in reqs if r.get("status") == "not_met"),
        "unknown": sum(1 for r in reqs if r.get("status") == "unknown"),
        "failed_must_have": sorted(body.get("failedMustHaveRequirements") or []),
        "coverage": centesimal(cov.get("rawScore")),
    }


def from_baseline(row: dict) -> dict:
    def num(key):
        v = row.get(key)
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None
    factors = {}
    try:
        factors = json.loads(row.get("factor_scores") or "{}")
    except ValueError:
        pass
    cov = factors.get("requirement_coverage")
    return {
        "eligibility": row.get("eligibility") or None,
        "decision": row.get("decision") or None,
        "fit_band": row.get("fit_band") or None,
        "diagnostic": num("diagnostic_score"),
        "suitability": num("suitability_score"),
        "profile_match": num("profile_match_score"),
        "requirement_count": int(float(row["requirement_count"])) if row.get("requirement_count") else None,
        "not_met": None,                       # not published per-pair in the CSV
        "unknown": int(float(row["unknown_requirement_count"])) if row.get("unknown_requirement_count") else None,
        "failed_must_have": None,              # only the count is in the CSV
        "coverage": None if cov is None else round(float(cov), 4),
    }


ORDER = {"ineligible": 0, "review_required": 1, "eligible": 2}
EPS = 1e-6


# Relations that assert "nothing changed". For these, an inert mutation is the
# property holding, not the test failing to bite.
INVARIANCE = {"MR0", "MR0_CV", "MR1", "MR6"}

MONITORED = ("eligibility", "decision", "fit_band", "diagnostic", "suitability",
             "profile_match", "requirement_count", "not_met", "unknown", "coverage")


def inert(before: dict, after: dict) -> bool:
    """True when the mutation moved nothing this harness can observe."""
    for field in MONITORED:
        a, b = before.get(field), after.get(field)
        if a is None or b is None:
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(a - b) > EPS:
                return False
        elif a != b:
            return False
    return before.get("failed_must_have") == after.get("failed_must_have")


def vacuity(relation: str, before: dict, after: dict) -> str | None:
    """Why this pair could not have violated the relation, or None.

    A relation that cannot be violated carries no evidence either way, so it is
    reported apart from the passes rather than inflating them.
    """
    if relation in INVARIANCE:
        return None
    if relation == "MR2":
        if (before.get("requirement_count") is not None
                and after.get("requirement_count") == before.get("requirement_count")):
            return "injected requirement never reached evaluation"
    if relation == "MR4":
        if not before.get("failed_must_have"):
            return "no failing must_have, so demoting one relieves no gate"
    if relation == "MR5":
        cov = before.get("coverage")
        if cov is not None and cov >= 100.0 - EPS:
            return "coverage already complete, it cannot rise"
    if inert(before, after):
        return "mutation moved nothing observable"
    return None


def judge(relation: str, before: dict, after: dict) -> list[str]:
    """Return the list of broken clauses; empty means the relation held."""
    bad = []

    def same(field, tol=EPS):
        a, b = before.get(field), after.get(field)
        if a is None or b is None:
            return
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(a - b) > tol:
                bad.append(f"{field}: {a} -> {b}")
        elif a != b:
            bad.append(f"{field}: {a!r} -> {b!r}")

    def not_above(field, tol=EPS):
        a, b = before.get(field), after.get(field)
        if a is None or b is None:
            return
        if b > a + tol:
            bad.append(f"{field} increased: {a} -> {b}")

    def not_below(field, tol=EPS):
        a, b = before.get(field), after.get(field)
        if a is None or b is None:
            return
        if b < a - tol:
            bad.append(f"{field} decreased: {a} -> {b}")

    if relation in ("MR0", "MR1"):
        for f in ("eligibility", "decision", "fit_band", "diagnostic", "suitability",
                  "profile_match", "requirement_count", "unknown", "coverage"):
            same(f)
        if before.get("failed_must_have") is not None and after.get("failed_must_have") is not None:
            if before["failed_must_have"] != after["failed_must_have"]:
                bad.append("failed_must_have set changed")
    elif relation in ("MR2", "MR3"):
        not_above("suitability")
        not_above("diagnostic")
        if before.get("eligibility") and after.get("eligibility"):
            if ORDER[after["eligibility"]] > ORDER[before["eligibility"]]:
                bad.append(f"eligibility improved: {before['eligibility']} -> {after['eligibility']}")
    elif relation == "MR4":
        if before.get("eligibility") and after.get("eligibility"):
            if ORDER[after["eligibility"]] < ORDER[before["eligibility"]]:
                bad.append(f"eligibility worsened: {before['eligibility']} -> {after['eligibility']}")
    elif relation == "MR5":
        not_below("coverage")
    elif relation == "MR6":
        same("profile_match")
    return bad


# ------------------------------------------------------------------ the driver
def pick(rows: list[dict], limit: int, seed: int) -> list[dict]:
    """Stratify by verdict so the sample is not all one outcome."""
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("status") != "ok":
            continue
        buckets.setdefault(r.get("eligibility") or "?", []).append(r)
    rng = random.Random(seed)
    for v in buckets.values():
        rng.shuffle(v)
    out, i = [], 0
    while len(out) < limit and any(buckets.values()):
        for key in sorted(buckets):
            if buckets[key] and len(out) < limit:
                out.append(buckets[key].pop())
        i += 1
        if i > limit * 4:
            break
    return out


async def measure(session, relation, cv_id, jd_id):
    """Match one pair through a clone. Returns (observed, status_code, detail)."""
    table = "job_descriptions" if relation in JD_SIDE else "user_cvs"
    clone_id = None
    try:
        if relation in JD_SIDE:
            clone_id = await clone_jd(jd_id, JD_MUTATORS[relation])
            code, body = match(session, cv_id, clone_id)
        else:
            clone_id = await clone_cv(cv_id, CV_MUTATORS[relation])
            code, body = match(session, clone_id, jd_id)
        if code != 200:
            return None, code, str(body)[:300]
        return observed(body), code, None
    finally:
        if clone_id:
            try:
                await drop_clone(table, clone_id)
            except Exception as err:               # noqa: BLE001
                print(f"!! clone {clone_id} not deleted: {err}", flush=True)


async def run(limit: int, baseline: Path, checkpoint: Path, out: Path, seed: int,
              relations: tuple[str, ...]):
    cp = json.loads(checkpoint.read_text(encoding="utf-8"))
    with open(baseline, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    sample = pick(rows, limit, seed)          # the baseline is used ONLY to sample

    session = requests.Session()
    session.headers["User-Agent"] = "metamorphic-corpus/2.0"
    login(session)

    tally = {r: {"held": 0, "vacuous": 0, "violations": 0, "skipped": 0, "errors": 0}
             for r in relations}
    records = []
    started = time.time()
    for n, row in enumerate(sample, 1):
        pair_id = row["pair_id"]
        cv_id = cp["cv_ids"][row["cv_id"]]
        jd_id = cp["jd_ids"][f"published:{row['jd_id']}"]

        # One control per side, measured in this same run.
        # Grouped by side, each under its own control. Cloning a CV perturbs
        # the BM25 corpus (ParadeDB keeps deleted rows until vacuum), so a CV
        # clone must never sit between a JD control and the JD relations it is
        # the control for.
        for side, ctrl in (("jd", "MR0"), ("cv", "MR0_CV")):
            group = [r for r in relations
                     if (r in JD_SIDE) == (side == "jd")]
            if not group:
                continue
            try:
                before, _, detail = await measure(session, ctrl, cv_id, jd_id)
            except Exception as err:                    # noqa: BLE001
                before, detail = None, f"{type(err).__name__}: {err}"[:300]
            if before is None:
                records.append({"pair_id": pair_id, "relation": f"control:{side}",
                                "outcome": "error", "detail": detail})
                for relation in group:
                    tally[relation]["errors"] += 1
                continue

            for relation in group:
                try:
                    after, code, detail = await measure(session, relation, cv_id, jd_id)
                except Skip as why:
                    tally[relation]["skipped"] += 1
                    records.append({"pair_id": pair_id, "relation": relation,
                                    "outcome": "skipped", "why": str(why)})
                    continue
                except Exception as err:                # noqa: BLE001
                    tally[relation]["errors"] += 1
                    records.append({"pair_id": pair_id, "relation": relation,
                                    "outcome": "error",
                                    "detail": f"{type(err).__name__}: {err}"[:300]})
                    continue
                if after is None:
                    tally[relation]["errors"] += 1
                    records.append({"pair_id": pair_id, "relation": relation,
                                    "outcome": "error", "status_code": code,
                                    "detail": detail})
                    continue
                bad = judge(relation, before, after)
                if bad:
                    tally[relation]["violations"] += 1
                    records.append({"pair_id": pair_id, "relation": relation,
                                    "outcome": "violation", "broken": bad,
                                    "before": before, "after": after})
                else:
                    why = vacuity(relation, before, after)
                    if why:
                        # Held, but it could not have done otherwise: report it
                        # apart from the passes so the suite is not read as
                        # stronger evidence than it is.
                        tally[relation]["vacuous"] += 1
                        records.append({"pair_id": pair_id, "relation": relation,
                                        "outcome": "held_vacuously", "why": why})
                    else:
                        tally[relation]["held"] += 1

        if n % 5 == 0 or n == len(sample):
            rate = (time.time() - started) / n
            line = "  ".join(
                f"{r}:{t['held']}/{t['vacuous']}/{t['violations']}" for r, t in tally.items()
            )
            print(f"  {n}/{len(sample)}  held/vacuous/viol  {line}  {rate:.1f}s/pair", flush=True)

    summary = {"n": len(sample), "relations": tally, "seed": seed,
               "seconds": round(time.time() - started, 1),
               "control": "identity clone measured in the same run, per side, no cross-side interleaving"}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "records": records},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relation", choices=SELECTABLE, action="append",
                    help="repeatable; default is every relation")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261111)
    ap.add_argument("--baseline", default="/tmp/phase1/matching_results.csv")
    ap.add_argument("--checkpoint", default="/tmp/phase1/checkpoint.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    if args.cleanup:
        print(json.dumps(asyncio.run(sweep()), ensure_ascii=False))
        return 0
    relations = tuple(args.relation or RELATIONS)
    out = Path(args.out or "/tmp/phase1/metamorphic.json")
    asyncio.run(run(args.limit, Path(args.baseline), Path(args.checkpoint),
                    out, args.seed, relations))
    return 0


if __name__ == "__main__":
    sys.exit(main())
