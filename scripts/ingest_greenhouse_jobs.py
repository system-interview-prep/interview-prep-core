"""CLI script for ingesting external jobs from Greenhouse Job Boards.

Usage::

    # Dry-run:
    python -m scripts.ingest_greenhouse_jobs --board-token stripe --company-name Stripe --dry-run

    # Live ingestion:
    python -m scripts.ingest_greenhouse_jobs --board-token stripe --company-name Stripe

    # Limit jobs:
    python -m scripts.ingest_greenhouse_jobs --board-token figma --company-name Figma --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from src.infrastructure.database import SessionFactory
except Exception:  # pragma: no cover
    SessionFactory = None

from src.modules.job_descriptions.ingestion.adapters.greenhouse import GreenhouseJobBoardAdapter
from src.modules.job_descriptions.ingestion.models import IngestionConfig
from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository
from src.modules.job_descriptions.ingestion.sanitizer import HtmlSanitizer
from src.modules.job_descriptions.ingestion.service import JobIngestionService
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest jobs from Greenhouse public Job Board API into job_descriptions."
    )
    parser.add_argument(
        "--board-token",
        required=True,
        help="Greenhouse board token (e.g. 'stripe', 'figma').",
    )
    parser.add_argument(
        "--company-name",
        required=True,
        help="Official company name to associate with this board.",
    )
    parser.add_argument(
        "--company-logo-url",
        default=None,
        help="Optional company logo URL.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of jobs to process.",
    )
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=24.0,
        help="Grace period in hours before marking absent jobs as CLOSED (default: 24.0).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse jobs without writing to database.",
    )
    return parser.parse_args(argv)


async def run_ingestion(args: argparse.Namespace) -> int:
    config = IngestionConfig(
        board_token=args.board_token.strip(),
        company_name=args.company_name.strip(),
        company_logo_url=args.company_logo_url.strip() if args.company_logo_url else None,
        grace_period_hours=args.grace_hours,
    )

    print("=" * 70)
    print(f"GREENHOUSE JOB INGESTION: board='{config.board_token}', company='{config.company_name}'")
    print(f"Mode: {'DRY-RUN (no DB writes)' if args.dry_run else 'LIVE'}")
    print("=" * 70)

    adapter = GreenhouseJobBoardAdapter()
    print(f"Fetching jobs from Greenhouse API for board '{config.board_token}'...")
    candidates = await adapter.fetch_board_jobs(config)
    print(f"Fetched {len(candidates)} total job postings from Greenhouse.")

    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]
        print(f"Limiting to first {len(candidates)} jobs as requested by --limit.")

    if args.dry_run:
        print("\n--- DRY RUN PREVIEW ---")
        parser = DeterministicJobDescriptionParser()
        for idx, cand in enumerate(candidates, start=1):
            plain = HtmlSanitizer.to_plain_text(cand.raw_html)
            exp_min = None
            try:
                from src.modules.user_cvs.facade import SourceDocument

                doc = SourceDocument(text=plain, blocks=[], sha256="dryrun")
                parsed = parser.parse(doc, extraction_version="1.0")
                exp_min = parsed.experience_min_years
            except Exception:
                pass

            print(f"[{idx}/{len(candidates)}] ID: {cand.external_job_id} | Title: {cand.title}")
            print(f"    Company: {cand.company_name} | Location: {cand.location}")
            print(f"    Apply URL: {cand.apply_url}")
            print(f"    Experience Min (parsed): {exp_min}")
            print(f"    Plain text preview ({len(plain)} chars): {plain[:120]}...\n")

        print("Dry run completed. Zero database changes made.")
        return 0

    if SessionFactory is None:
        print("ERROR: Database SessionFactory is not available.", file=sys.stderr)
        return 1

    async with SessionFactory() as session:
        async with session.begin():
            repo = JobIngestionRepository(session)
            service = JobIngestionService(repo, adapter)
            summary = await service.ingest_candidates(config, candidates)

    print("\n" + "=" * 70)
    print("INGESTION SUMMARY")
    print("=" * 70)
    print(json.dumps(summary.model_dump(), indent=2, default=str))

    if summary.status == "FAILED":
        return 1
    return 0


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(run_ingestion(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
