"""
PR4 — Backfill Job Listing Fields  (v2 — dual source strategy)
===============================================================

Source-of-proposal strategy (per field, per job):

  1. EXISTING_CANONICAL
     structured_data already contains the canonical field with a non-None value.
     Use it directly — no re-parsing needed.

  2. REEXTRACTED_FROM_RAW
     structured_data is missing / null for this field AND raw_text is available.
     Run the PR2 parser (DeterministicJobDescriptionParser) once per job,
     in-memory, and read the proposed value from the fresh CanonicalJobDescription.
     The parser is called at most once per job.

  3. SKIP_NO_SOURCE
     Neither structured_data has the field nor raw_text is available.
     No update is proposed.

Design invariants:
  - Does NOT write regex in this script — uses the parser directly.
  - Does NOT mutate structured_data or write fresh extraction back to DB.
  - Does NOT touch lifecycle columns (processing_status, listing_status, status).
  - Does NOT touch title, taxonomy, source identity, posted_at.
  - Default (safe) mode: only fills NULL columns.
  - --force: allows overwriting an existing published value (logged explicitly).
  - --dry-run: zero DB writes.
  - Each job update is atomic (per-job rollback on failure).
  - Batch failure of one job never aborts the rest.

Usage::

    python -m scripts.backfill_job_listing_fields --dry-run --job-id <uuid>
    python -m scripts.backfill_job_listing_fields --dry-run --limit 50
    python -m scripts.backfill_job_listing_fields --limit 50
    python -m scripts.backfill_job_listing_fields --job-id <uuid> --force
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path for direct CLI execution
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Expose SessionFactory at module level so tests can patch it.
# Deferred import avoids DB connection at import time.
# ---------------------------------------------------------------------------
try:
    from src.infrastructure.database import SessionFactory  # type: ignore[assignment]
except Exception:  # pragma: no cover
    SessionFactory = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Backfillable fields
# ---------------------------------------------------------------------------
BACKFILL_FIELDS: list[str] = [
    "company_name",
    "location",
    "work_mode",
    "employment_type",
    "seniority",
    "experience_min_years",
    "experience_max_years",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_negotiable",
]

# CanonicalJobDescription camelCase alias -> DB column
_CANON_TO_DB: dict[str, str] = {
    "companyName": "company_name",
    "location": "location",
    "workMode": "work_mode",
    "employmentType": "employment_type",
    "seniority": "seniority",
    "experienceMinYears": "experience_min_years",
    "experienceMaxYears": "experience_max_years",
    "salaryMin": "salary_min",
    "salaryMax": "salary_max",
    "salaryCurrency": "salary_currency",
    "salaryPeriod": "salary_period",
    "salaryNegotiable": "salary_negotiable",
}

# Evidence span prefix per canonical key (best-effort look-up)
_EVIDENCE_PREFIX: dict[str, str] = {
    "companyName": "ev-jd-company",
    "location": "ev-jd-location",
    "experienceMinYears": "ev-jd-experience",
    "experienceMaxYears": "ev-jd-experience",
    "salaryMin": "ev-jd-salary",
    "salaryMax": "ev-jd-salary",
    "salaryNegotiable": "ev-jd-salary",
    "salaryCurrency": "ev-jd-salary",
    "salaryPeriod": "ev-jd-salary",
}

_RAW_FALLBACK: dict[str, list[str]] = {
    "experienceMinYears": ["experienceRaw"],
    "experienceMaxYears": ["experienceRaw"],
    "salaryMin": ["salaryRaw"],
    "salaryMax": ["salaryRaw"],
    "salaryNegotiable": ["salaryRaw"],
    "salaryCurrency": ["salaryRaw"],
    "salaryPeriod": ["salaryRaw"],
}

# Proposal source labels
EXISTING_CANONICAL = "EXISTING_CANONICAL"
REEXTRACTED_FROM_RAW = "REEXTRACTED_FROM_RAW"
SKIP_NO_SOURCE = "SKIP_NO_SOURCE"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FieldProposal:
    db_col: str
    canon_key: str
    current_value: Any
    proposed_value: Any
    evidence_excerpt: str
    action: str          # UPDATE | OVERWRITE | SKIP_ALREADY_SET | SKIP_NO_EVIDENCE | SKIP_UNCHANGED
    proposal_source: str  # EXISTING_CANONICAL | REEXTRACTED_FROM_RAW | SKIP_NO_SOURCE


@dataclass
class JobProposal:
    job_id: str
    title: str
    fields: list[FieldProposal] = field(default_factory=list)


@dataclass
class RunResult:
    inspected: int = 0
    jobs_updated: int = 0
    fields_updated: int = 0
    jobs_skipped: int = 0
    jobs_failed: int = 0
    jobs_overwritten: int = 0

    @property
    def updated(self) -> int:
        return self.jobs_updated

    @updated.setter
    def updated(self, val: int) -> None:
        self.jobs_updated = val

    @property
    def skipped(self) -> int:
        return self.jobs_skipped

    @skipped.setter
    def skipped(self, val: int) -> None:
        self.jobs_skipped = val

    @property
    def failed(self) -> int:
        return self.jobs_failed

    @failed.setter
    def failed(self, val: int) -> None:
        self.jobs_failed = val

    @property
    def overwritten(self) -> int:
        return self.jobs_overwritten

    @overwritten.setter
    def overwritten(self, val: int) -> None:
        self.jobs_overwritten = val


# ---------------------------------------------------------------------------
# Parser helper — run PR2 parser once per job from raw_text
# ---------------------------------------------------------------------------

def _make_source_document(raw_text: str, job_id: str) -> "SourceDocument":  # noqa: F821
    """Wrap raw_text in a minimal SourceDocument for the parser."""
    from src.modules.user_cvs.parsing.domain.source import SourceBlock, SourceDocument
    block = SourceBlock(
        block_id="block-0000",
        text=raw_text,
        page=None,
        reading_order=0,
        bounding_box=None,
        block_type="text",
        char_start=0,
        char_end=len(raw_text),
        section="other",
    )
    import hashlib

    doc_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return SourceDocument(
        document_id=job_id,
        document_sha256=doc_sha,
        text=raw_text,
        blocks=(block,),
    )


def _run_parser(raw_text: str, job_id: str) -> dict[str, Any]:
    """
    Run the PR2 DeterministicJobDescriptionParser on raw_text.
    Returns a dict of {canonKey: value} from the fresh CanonicalJobDescription.
    Raises on parse error (caller should handle).
    Does NOT modify structured_data or write to DB.
    """
    from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser

    source = _make_source_document(raw_text, job_id)
    parser = DeterministicJobDescriptionParser()
    canonical = parser.parse(source, extraction_version="backfill-reextract")
    # Dump using by_alias=True to get camelCase keys that match _CANON_TO_DB
    return canonical.model_dump(by_alias=True, exclude_none=False)


# ---------------------------------------------------------------------------
# Evidence extraction (read-only — no parser logic)
# ---------------------------------------------------------------------------

def _evidence_text(data: dict, canon_key: str) -> str:
    """Pull a short evidence excerpt from a canonical dict (structured_data or fresh parse)."""
    prefix = _EVIDENCE_PREFIX.get(canon_key)
    if prefix:
        for span in data.get("evidence", []):
            if isinstance(span, dict) and str(span.get("evidenceId", "")).startswith(prefix):
                txt = span.get("text", "")
                if txt:
                    return txt[:120].replace("\n", " ")
    for fk in _RAW_FALLBACK.get(canon_key, []):
        val = data.get(fk)
        if val is not None and str(val).strip():
            return str(val)[:120]
    val = data.get(canon_key)
    if val is not None:
        return str(val)[:120]
    return "(no evidence)"


# ---------------------------------------------------------------------------
# Core proposal builder
# ---------------------------------------------------------------------------

def _decode_structured_data(raw_sd: Any) -> dict:
    if not raw_sd:
        return {}
    if isinstance(raw_sd, dict):
        return raw_sd
    try:
        return json.loads(raw_sd)
    except (json.JSONDecodeError, TypeError):
        return {}


def _build_proposal(row: dict, *, force: bool) -> JobProposal:
    """
    Compute proposed field updates for one DB row.

    Source-of-proposal strategy:
      1. Prefer structured_data values that are already present (EXISTING_CANONICAL).
      2. For fields still missing/null, if raw_text is available, run the PR2 parser
         once and read from the fresh extraction (REEXTRACTED_FROM_RAW).
      3. If neither source has a value, propose nothing (SKIP_NO_SOURCE / no action).
    """
    proposal = JobProposal(job_id=row["id"], title=row.get("title") or "(untitled)")
    structured_data = _decode_structured_data(row.get("structured_data"))
    raw_text: str | None = row.get("raw_text")

    # Determine which fields are still missing from structured_data
    needs_reextract: list[str] = []
    for canon_key in _CANON_TO_DB:
        val = structured_data.get(canon_key)
        if val in (None, "", [], {}):
            needs_reextract.append(canon_key)

    # Run parser exactly once if any field needs it and raw_text is available
    fresh_data: dict[str, Any] = {}
    reextract_attempted = False
    if needs_reextract and raw_text and raw_text.strip():
        try:
            fresh_data = _run_parser(raw_text, row["id"])
            reextract_attempted = True
        except Exception as exc:
            fresh_data = {}
            # Soft failure: log, but don't crash the proposal builder.
            # Fields that needed re-extraction will become SKIP_NO_SOURCE.
            print(f"  [WARN] Parser re-extraction failed for {row['id']!r}: {exc}", file=sys.stderr)

    for canon_key, db_col in _CANON_TO_DB.items():
        current_value = row.get(db_col)

        # Determine proposed value and source label
        sd_val = structured_data.get(canon_key)
        if sd_val not in (None, "", [], {}):
            proposed_raw = sd_val
            proposal_source = EXISTING_CANONICAL
            evidence = _evidence_text(structured_data, canon_key)
        elif reextract_attempted or (needs_reextract and canon_key in needs_reextract):
            fresh_val = fresh_data.get(canon_key)
            if fresh_val not in (None, "", [], {}):
                proposed_raw = fresh_val
                proposal_source = REEXTRACTED_FROM_RAW
                evidence = _evidence_text(fresh_data, canon_key)
            else:
                proposed_raw = None
                proposal_source = SKIP_NO_SOURCE
                evidence = "(no evidence in raw_text)"
        else:
            proposed_raw = None
            proposal_source = SKIP_NO_SOURCE
            evidence = "(no raw_text available)"

        # Action resolution
        if proposed_raw is None:
            action = "SKIP_NO_EVIDENCE"
            display_proposed = current_value
        elif current_value == proposed_raw:
            action = "SKIP_UNCHANGED"
            display_proposed = proposed_raw
        elif current_value is not None and not force:
            action = "SKIP_ALREADY_SET"
            display_proposed = proposed_raw
        else:
            action = "OVERWRITE" if (current_value is not None and force) else "UPDATE"
            display_proposed = proposed_raw

        proposal.fields.append(FieldProposal(
            db_col=db_col,
            canon_key=canon_key,
            current_value=current_value,
            proposed_value=display_proposed,
            evidence_excerpt=evidence,
            action=action,
            proposal_source=proposal_source,
        ))

    return proposal


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_CANDIDATE_BASE = """
    SELECT id, title, raw_text, company_name, location, work_mode, employment_type, seniority,
           experience_min_years, experience_max_years,
           salary_min, salary_max, salary_currency, salary_period, salary_negotiable,
           structured_data
    FROM job_descriptions
    WHERE item_type = 'JOB_DESCRIPTION'
      AND processing_status = 'DONE'
      AND (
        structured_data IS NOT NULL
        OR raw_text IS NOT NULL
      )
"""


async def _fetch_jobs(db: AsyncSession, job_id: str | None, limit: int) -> list[dict]:
    if job_id:
        result = await db.execute(text(_CANDIDATE_BASE + " AND id = :job_id"), {"job_id": job_id})
    else:
        result = await db.execute(
            text(_CANDIDATE_BASE + " ORDER BY created_at ASC LIMIT :limit"), {"limit": limit}
        )
    return [dict(r) for r in result.mappings()]


async def _apply_proposal(db: AsyncSession, proposal: JobProposal) -> int:
    """Write actionable fields atomically. Returns number of columns updated."""
    updates: dict[str, Any] = {
        fp.db_col: fp.proposed_value
        for fp in proposal.fields
        if fp.action in ("UPDATE", "OVERWRITE")
    }
    if not updates:
        return 0
    set_clauses = ", ".join(f"{col} = :{col}" for col in updates)
    await db.execute(
        text(f"UPDATE job_descriptions SET {set_clauses}, updated_at = now() WHERE id = :id"),
        {"id": proposal.job_id, **updates},
    )
    await db.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_W = 26
_DIV = "-" * 110


def _fmt(v: Any) -> str:
    return "null" if v is None else str(v)[:24]


def _print_proposal(p: JobProposal, dry_run: bool) -> None:
    tag = "[DRY-RUN]" if dry_run else "[LIVE]"
    print(f"\n{tag} Job {p.job_id!r}")
    print(f"  Title: {p.title!r}")
    print(_DIV)
    print(f"  {'Field':<{_W}} {'Before':<18} {'After':<18} {'Action':<22} {'Source':<22} Evidence")
    print(_DIV)
    for fp in p.fields:
        after = _fmt(fp.proposed_value) if fp.action in ("UPDATE", "OVERWRITE") else "(no change)"
        ev = fp.evidence_excerpt[:30].replace("\n", " ")
        print(
            f"  {fp.db_col:<{_W}} {_fmt(fp.current_value):<18} {after:<18}"
            f" {fp.action:<22} {fp.proposal_source:<22} {ev}"
        )
    print()


def _print_corpus_report(proposals: list[JobProposal], inspected: int) -> None:
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"proposed": 0, "no_evidence": 0, "already_set": 0, "unchanged": 0,
                 "existing_canonical": 0, "reextracted": 0, "no_source": 0}
    )
    for p in proposals:
        for fp in p.fields:
            s = stats[fp.db_col]
            if fp.action in ("UPDATE", "OVERWRITE"):
                s["proposed"] += 1
            elif fp.action == "SKIP_NO_EVIDENCE":
                s["no_evidence"] += 1
            elif fp.action == "SKIP_ALREADY_SET":
                s["already_set"] += 1
            elif fp.action == "SKIP_UNCHANGED":
                s["unchanged"] += 1

            if fp.proposal_source == EXISTING_CANONICAL:
                s["existing_canonical"] += 1
            elif fp.proposal_source == REEXTRACTED_FROM_RAW:
                s["reextracted"] += 1
            elif fp.proposal_source == SKIP_NO_SOURCE:
                s["no_source"] += 1

    print("\n" + "=" * 110)
    print(f"CORPUS VERIFICATION REPORT  (inspected: {inspected})")
    print("=" * 110)
    print(
        f"  {'Field':<28} {'Would Update':>12} {'No Evidence':>12}"
        f" {'Already Set':>12} {'Unchanged':>10} {'Existing SD':>12} {'Reextracted':>12} {'No Source':>10}"
    )
    print("-" * 110)
    for col in BACKFILL_FIELDS:
        s = stats.get(col, {})
        print(
            f"  {col:<28}"
            f" {s.get('proposed', 0):>12}"
            f" {s.get('no_evidence', 0):>12}"
            f" {s.get('already_set', 0):>12}"
            f" {s.get('unchanged', 0):>10}"
            f" {s.get('existing_canonical', 0):>12}"
            f" {s.get('reextracted', 0):>12}"
            f" {s.get('no_source', 0):>10}"
        )
    print("=" * 110)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _run(*, job_id: str | None, limit: int, dry_run: bool, force: bool) -> RunResult:
    global SessionFactory  # noqa: PLW0603
    if SessionFactory is None:
        from src.infrastructure.database import SessionFactory as _SF  # noqa: PLC0415
        SessionFactory = _SF

    result = RunResult()
    proposals: list[JobProposal] = []

    async with SessionFactory() as db:
        rows = await _fetch_jobs(db, job_id, limit)

    if not rows:
        print(
            "No eligible rows found "
            "(item_type=JOB_DESCRIPTION, processing_status=DONE)."
        )
        return result

    result.inspected = len(rows)
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Inspecting {len(rows)} job(s)...\n")

    for row in rows:
        has_sd = bool(row.get("structured_data"))
        has_raw = bool(row.get("raw_text") and str(row["raw_text"]).strip())
        if not has_sd and not has_raw:
            print(f"  [SKIP] {row['id']!r} — no structured_data AND no raw_text.")
            result.jobs_skipped += 1
            continue

        proposal = _build_proposal(row, force=force)
        proposals.append(proposal)
        _print_proposal(proposal, dry_run=dry_run)

        actionable_fields = [fp for fp in proposal.fields if fp.action in ("UPDATE", "OVERWRITE")]
        num_fields = len(actionable_fields)
        has_overwrites = any(fp.action == "OVERWRITE" for fp in actionable_fields)

        if dry_run:
            if num_fields > 0:
                result.jobs_updated += 1
                result.fields_updated += num_fields
                if has_overwrites:
                    result.jobs_overwritten += 1
            else:
                result.jobs_skipped += 1
            continue

        if num_fields == 0:
            result.jobs_skipped += 1
            continue

        try:
            async with SessionFactory() as wdb:
                n = await _apply_proposal(wdb, proposal)
            if n:
                result.jobs_updated += 1
                result.fields_updated += n
                if has_overwrites:
                    result.jobs_overwritten += 1
                print(f"  \u2714  {n} column(s) updated for {proposal.job_id!r}")
            else:
                result.jobs_skipped += 1
        except Exception as exc:
            result.jobs_failed += 1
            print(f"  \u2718  FAILED {proposal.job_id!r}: {exc}", file=sys.stderr)

    if proposals:
        _print_corpus_report(proposals, result.inspected)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PR4 — Backfill job listing published fields from structured_data / raw_text.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Print proposed changes without writing to DB.")
    p.add_argument("--job-id", metavar="UUID", default=None,
                   help="Restrict to a single job UUID.")
    p.add_argument("--limit", type=int, default=50, metavar="N",
                   help="Max jobs when --job-id is not set (default: 50).")
    p.add_argument("--force", action="store_true", default=False,
                   help="Allow overwriting non-NULL published values.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.dry_run and args.force:
        import time
        print(
            "\n\u26a0\ufe0f  WARNING: --force active — existing published values WILL be overwritten.\n"
            "   Ctrl+C within 5 s to abort...\n",
            file=sys.stderr,
        )
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("Aborted.", file=sys.stderr)
            sys.exit(0)

    result = asyncio.run(_run(
        job_id=args.job_id, limit=args.limit, dry_run=args.dry_run, force=args.force,
    ))

    tag = "[DRY-RUN] " if args.dry_run else ""
    print(f"\n{tag}Summary")
    print(f"  Jobs inspected       : {result.inspected}")
    if args.dry_run:
        print(f"  Jobs would update    : {result.jobs_updated}")
        print(f"  Fields would update  : {result.fields_updated}")
        print(f"  Jobs would skip      : {result.jobs_skipped}")
        if result.jobs_overwritten:
            print(f"  Jobs would overwrite : {result.jobs_overwritten}")
        print(f"  Jobs failed          : {result.jobs_failed}")
    else:
        print(f"  Jobs updated         : {result.jobs_updated}")
        print(f"  Fields updated       : {result.fields_updated}")
        print(f"  Jobs skipped         : {result.jobs_skipped}")
        if result.jobs_overwritten:
            print(f"  Jobs overwritten     : {result.jobs_overwritten}")
        print(f"  Jobs failed          : {result.jobs_failed}")

    sys.exit(1 if result.failed else 0)


if __name__ == "__main__":
    main()

