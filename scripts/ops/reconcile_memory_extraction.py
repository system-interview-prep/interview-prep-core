"""Bounded manual recovery for post-session memory extraction (V2 spec §9).

The API reconciles automatically every five minutes, but only evaluations of
the last six hours: after a longer outage (broker down for a day, the worker
image without its memory dependency for a week) the sessions graded in that
time are outside the automatic window on purpose, so that a wide sweep never
turns into an unplanned paid backfill. This script is that wider sweep, with
every bound explicit and a dry run first.

Run in interview-prep-core, with the API's environment (it needs the broker
and ``INTERVIEW_CANDIDATE_MEMORY=readwrite``):

    docker compose exec backend python scripts/ops/reconcile_memory_extraction.py --window-hours 48 --dry-run
    docker compose exec backend python scripts/ops/reconcile_memory_extraction.py --window-hours 48 --limit 50

Bounds: ``--window-hours`` at most 720 (30 days), ``--limit`` at most 200
sessions per run, ``--max-dispatches`` per session (default 5). Each
dispatched session costs the worker one extraction (provider calls). The
dry run lists exactly which sessions would be dispatched and which would be
marked exhausted, and changes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

MAX_WINDOW_HOURS = 720
MAX_LIMIT = 200


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--window-hours", type=int, default=6, help="evaluations graded within this many hours (max 720)")
    parser.add_argument("--limit", type=int, default=20, help="sessions per run (max 200)")
    parser.add_argument("--max-dispatches", type=int, default=5, help="dispatches per session before giving up")
    parser.add_argument("--dry-run", action="store_true", help="report only; no lock, no writes, no dispatch")
    args = parser.parse_args(argv)
    if not 1 <= args.window_hours <= MAX_WINDOW_HOURS:
        parser.error(f"--window-hours must be between 1 and {MAX_WINDOW_HOURS}")
    if not 1 <= args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be between 1 and {MAX_LIMIT}")
    if args.max_dispatches < 1:
        parser.error("--max-dispatches must be at least 1")
    return args


async def run(args: argparse.Namespace) -> dict:
    from src.infrastructure.database import StandaloneSessionFactory
    from src.modules.interviews.memory.extraction_job import reconcile_memory_extraction

    async with StandaloneSessionFactory() as db:
        return await reconcile_memory_extraction(
            db,
            limit=args.limit,
            window_hours=args.window_hours,
            max_dispatches=args.max_dispatches,
            dry_run=args.dry_run,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = asyncio.run(run(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("status") in ("COMPLETE", "DRY_RUN") else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
