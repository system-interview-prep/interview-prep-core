"""Measure, on identical inputs, how far ParadeDB BM25 and in-memory BM25 disagree.

The matching facade feeds the two providers differently, so a raw end-to-end
score gap mixes three causes. This isolates them:

  paradedb        ParadeDbBm25Provider on user_cvs.raw_text, corpus IDF,
                  saturation normalisation - what POST /match-ids uses.
  memory_raw      bm25_similarity(job_text, user_cvs.raw_text) - same document
                  as ParadeDB, different formula. The gap to `paradedb` is the
                  pure formula effect.
  memory_struct   bm25_similarity(job_text, contextual_resume_text) - what the
                  in-process benchmark harness and POST /match actually score.
                  The gap to `memory_raw` is the choice-of-document effect.

Run inside the backend container, which already has DB access:
  docker compose exec -T -e PYTHONPATH=/app -w /app backend \
      python scripts/bm25_provider_ab.py --checkpoint-json - < checkpoint.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from typing import Any

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.matching.domain.adapters import job_description_to_matching_job
from src.modules.matching.domain.schemas import SkillRequirement, UnresolvedRequirement
from src.modules.matching.retrieval.bm25 import bm25_similarity
from src.modules.matching.retrieval.bm25_provider import (
    ParadeDbBm25Provider,
    track_bm25_provider,
)
from src.modules.user_cvs.domain.schemas import CanonicalResume


def job_query_text(job: Any) -> str:
    """Replicate the facade's contextual_job_text exactly (facade.py:1006-1013)."""
    parts = [value for value in (job.job_title,) if value]
    parts.extend(
        item.raw_label for item in job.requirements if isinstance(item, UnresolvedRequirement)
    )
    parts.extend(item.skill.label for item in job.requirements if isinstance(item, SkillRequirement))
    parts.extend(item.text for item in job.responsibilities)
    parts.extend(item.label for item in job.career_classifications)
    joined = "\n".join(parts)
    return joined or "\n".join(item.text for item in job.evidence)


def resume_doc_text(resume: CanonicalResume) -> str:
    """Replicate the facade's contextual_resume_text exactly (facade.py:989-1005)."""
    parts: list[str] = [
        value for value in (resume.profile.headline, resume.profile.summary) if value
    ]
    parts.extend(claim.concept.label for claim in resume.skills)
    parts.extend(item.label for item in resume.career_classifications)
    parts.extend(project.name for project in resume.projects)
    parts.extend(line for employment in resume.employment for line in employment.responsibilities)
    parts.extend(
        achievement.text
        for employment in resume.employment
        for achievement in employment.achievements
    )
    parts.extend(project.description for project in resume.projects if project.description)
    joined = "\n".join(parts)
    return joined or "\n".join(item.text for item in resume.evidence)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-json", default="-", help="checkpoint.json, or - for stdin")
    parser.add_argument("--out", default="", help="write per-pair rows as JSON here")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.checkpoint_json == "-" else open(args.checkpoint_json).read()
    checkpoint = json.loads(raw)
    cv_ids: dict[str, str] = checkpoint["cv_ids"]
    jd_ids: dict[str, str] = {
        key.split("published:", 1)[1]: value
        for key, value in checkpoint["jd_ids"].items()
        if key.startswith("published:")
    }
    print(f"CVs={len(cv_ids)} JDs={len(jd_ids)}", flush=True)

    resumes: dict[str, tuple[str, str, str]] = {}  # label -> (db_id, raw_text, struct_text)
    jobs: dict[str, str] = {}  # label -> job_text

    async with SessionFactory() as db:
        for label, db_id in cv_ids.items():
            row = (
                await db.execute(
                    text("SELECT raw_text, parsed_data FROM user_cvs WHERE id = :id"),
                    {"id": db_id},
                )
            ).mappings().one_or_none()
            if row is None:
                print(f"  !! CV {label} ({db_id}) not in user_cvs", flush=True)
                continue
            parsed = row["parsed_data"]
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            # Same construction as matching's input_resolver.resolve_resume.
            resume = CanonicalResume.model_validate(parsed)
            resumes[label] = (db_id, row["raw_text"] or "", resume_doc_text(resume))

        for label, db_id in jd_ids.items():
            row = (
                await db.execute(
                    text("SELECT structured_data FROM job_descriptions WHERE id = :id"),
                    {"id": db_id},
                )
            ).mappings().one_or_none()
            if row is None:
                print(f"  !! JD {label} ({db_id}) not in job_descriptions", flush=True)
                continue
            structured = row["structured_data"]
            if isinstance(structured, str):
                structured = json.loads(structured)
            canonical = CanonicalJobDescription.model_validate(structured)
            jobs[label] = job_query_text(job_description_to_matching_job(canonical, job_id=db_id))

    provider = ParadeDbBm25Provider(session_factory=SessionFactory)
    rows: list[dict[str, Any]] = []
    providers_seen: dict[str, int] = {}

    for jd_label, job_text in sorted(jobs.items()):
        for cv_label, (db_id, raw_text, struct_text) in sorted(resumes.items()):
            with track_bm25_provider() as used:
                paradedb = await provider.score_async(job_text, struct_text, doc_id=db_id)
            name = used.get("provider", "?")
            providers_seen[name] = providers_seen.get(name, 0) + 1
            rows.append(
                {
                    "jd": jd_label,
                    "cv": cv_label,
                    "paradedb": paradedb,
                    "memory_raw": bm25_similarity(job_text, raw_text),
                    "memory_struct": bm25_similarity(job_text, struct_text),
                }
            )

    print(f"provider actually used: {providers_seen}", flush=True)
    if set(providers_seen) - {"paradedb"}:
        print("  !! a fallback fired - the paradedb column is NOT ParadeDB", flush=True)

    def show(name: str) -> None:
        values = [row[name] for row in rows]
        print(
            f"  {name:<14} n={len(values)} mean={statistics.mean(values):.4f} "
            f"median={statistics.median(values):.4f} min={min(values):.4f} max={max(values):.4f} "
            f"zeros={sum(1 for v in values if v == 0.0)}",
            flush=True,
        )

    print(f"\n{len(rows)} pairs")
    for name in ("paradedb", "memory_raw", "memory_struct"):
        show(name)

    def gap(a: str, b: str) -> None:
        diffs = [abs(row[a] - row[b]) for row in rows]
        print(
            f"  |{a} - {b}|: mean={statistics.mean(diffs):.4f} "
            f"median={statistics.median(diffs):.4f} max={max(diffs):.4f}",
            flush=True,
        )

    print("\ngaps")
    gap("paradedb", "memory_raw")
    gap("memory_raw", "memory_struct")
    gap("paradedb", "memory_struct")

    # Rank agreement per JD: does the provider change WHICH CV ranks best?
    flips = 0
    for jd_label in sorted(jobs):
        block = [row for row in rows if row["jd"] == jd_label]
        best_p = max(block, key=lambda r: r["paradedb"])["cv"]
        best_m = max(block, key=lambda r: r["memory_struct"])["cv"]
        if best_p != best_m:
            flips += 1
    print(f"\ntop-ranked CV differs on {flips}/{len(jobs)} JDs", flush=True)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as stream:
            json.dump(rows, stream, ensure_ascii=False, indent=2)
        print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
