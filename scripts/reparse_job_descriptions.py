"""Re-parse stored external jobs after parser rules change.

Ingestion skips a posting whose content hash is unchanged, so a parser fix
never reaches jobs already in the database. This re-runs the ingestion parser
on the stored plain text, without fetching the board again, and writes through
the same repository update. Human-reviewed jobs are left untouched.

Usage::

    # Preview which skills each job gains or loses:
    python -m scripts.reparse_job_descriptions

    # Write the new parse results:
    python -m scripts.reparse_job_descriptions --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from src.infrastructure.database import SessionFactory  # noqa: E402
from src.modules.job_descriptions.ingestion.models import ExternalJobCandidate  # noqa: E402
from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository  # noqa: E402
from src.modules.job_descriptions.ingestion.service import JobIngestionService  # noqa: E402
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser  # noqa: E402

# Keys the repository rebuilds itself; the rest of `ingestion` is carried over.
_REBUILT_INGESTION_KEYS = {"source_type", "source_key", "content_hash", "fetched_at"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Re-parse stored external jobs with the current parser.")
    parser.add_argument("--apply", action="store_true", help="Write results (default: preview only).")
    parser.add_argument("--job-id", action="append", default=[], help="Limit to these job ids.")
    return parser.parse_args(argv)


def _skills(structured: dict | None) -> list[str]:
    labels: list[str] = []
    for req in (structured or {}).get("requirements") or []:
        concept = req.get("concept") or {}
        label = concept.get("label")
        if req.get("kind") == "skill" and label and label not in labels:
            labels.append(label)
    return labels


async def run(args: argparse.Namespace) -> int:
    from src.core.config import get_settings
    from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser
    from src.modules.taxonomy.facade import load_active_skill_taxonomy

    async with SessionFactory() as session:
        taxonomy = await load_active_skill_taxonomy(session)
        filters = "AND id = ANY(CAST(:ids AS uuid[]))" if args.job_id else ""
        rows = (
            await session.execute(
                text(
                    "SELECT id, title, company_name, company_logo_url, location, source_type, source_key, "
                    "source_name, source_url, apply_url, external_job_id, posted_at, raw_text, "
                    "structured_data, extracted_metadata, primary_taxonomy_concept_id, "
                    "primary_taxonomy_version "
                    "FROM job_descriptions WHERE item_type = 'JOB_DESCRIPTION' "
                    f"{filters} ORDER BY company_name, title"
                ),
                {"ids": args.job_id} if args.job_id else {},
            )
        ).mappings().all()

    parser_cls = (
        HybridJobDescriptionParser
        if get_settings().jd_parser_mode.casefold().strip() == "hybrid"
        else DeterministicJobDescriptionParser
    )
    print(f"Parser: {parser_cls.__name__} | jobs: {len(rows)} | mode: {'APPLY' if args.apply else 'PREVIEW'}")

    changed = failed = skipped = 0
    async with SessionFactory() as session:
        async with session.begin():
            repo = JobIngestionRepository(session)
            service = JobIngestionService(repo, parser=parser_cls(taxonomy.skills, taxonomy.version))
            for row in rows:
                meta = row["extracted_metadata"] or {}
                label = f"{row['company_name']} | {row['title']}"
                if meta.get("is_human_reviewed") is True:
                    skipped += 1
                    print(f"- SKIP (human reviewed) {label}")
                    continue
                ingestion = meta.get("ingestion") or {}
                content_hash = ingestion.get("content_hash") or ""
                candidate = ExternalJobCandidate(
                    external_job_id=row["external_job_id"],
                    title=row["title"],
                    company_name=row["company_name"],
                    company_logo_url=row["company_logo_url"],
                    location=row["location"],
                    source_type=row["source_type"],
                    source_key=row["source_key"],
                    source_name=row["source_name"],
                    source_url=row["source_url"],
                    apply_url=row["apply_url"],
                    posted_at=row["posted_at"],
                    raw_payload=meta.get("raw_external_job") or {},
                    metadata={k: v for k, v in ingestion.items() if k not in _REBUILT_INGESTION_KEYS},
                )
                plain_text = row["raw_text"] or ""
                try:
                    parsed = await service._parse(candidate, content_hash, plain_text)
                except Exception as exc:  # keep the stored result when the parser fails
                    failed += 1
                    print(f"- FAIL {label}: {exc}")
                    continue
                before = _skills(row["structured_data"])
                after = _skills(parsed.model_dump(mode="json", by_alias=True))
                removed = [s for s in before if s not in after]
                added = [s for s in after if s not in before]
                if removed or added:
                    changed += 1
                print(f"- {label}\n    removed: {removed or '-'}\n    added:   {added or '-'}")
                if not args.apply:
                    continue
                classification = parsed.career_classifications[0] if parsed.career_classifications else None
                await repo.update_candidate(
                    str(row["id"]),
                    candidate,
                    sanitized_html="",
                    plain_text=plain_text,
                    content_hash=content_hash,
                    parsed=parsed,
                    is_human_reviewed=False,
                    taxonomy_concept_id=classification.code if classification else None,
                    taxonomy_version=getattr(classification, "taxonomy_version", None) if classification else None,
                )

    print(f"Skill changes: {changed} | failed: {failed} | skipped: {skipped} | written: {args.apply}")
    return 1 if failed else 0


def main() -> None:
    sys.exit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
