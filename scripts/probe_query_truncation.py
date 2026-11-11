"""How many postings are long enough for sanitize_query's 100-token cut to bite?

ParadeDbBm25Provider.sanitize_query keeps tokens[:100]. Everything after that is
silently dropped from the BM25 query, so a posting's tail is never searched -
and appending a requirement can push existing terms off the end, changing which
terms are searched at all rather than merely adding one.
"""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.modules.matching.retrieval.bm25 import tokenize_text


async def main() -> None:
    async with SessionFactory() as session:
        rows = (await session.execute(
            text("SELECT id, title, structured_data FROM job_descriptions")
        )).mappings().all()

    counts = []
    for r in rows:
        data = r["structured_data"] or {}
        if isinstance(data, str):
            data = json.loads(data)
        parts = [r["title"] or ""]
        parts.extend(str(x.get("rawLabel") or "") for x in (data.get("requirements") or []))
        parts.extend(str(x.get("text") or "") for x in (data.get("responsibilities") or []))
        counts.append(len(tokenize_text("\n".join(p for p in parts if p))))

    counts.sort()
    n = len(counts)
    over = sum(1 for c in counts if c > 100)
    print(f"postings measured     : {n}")
    print(f"median query tokens   : {counts[n // 2] if n else 0}")
    print(f"max query tokens      : {counts[-1] if n else 0}")
    print(f"OVER the 100 cut      : {over}/{n} ({100.0 * over / n:.1f}%)" if n else "")
    if over:
        tails = [c - 100 for c in counts if c > 100]
        print(f"  tokens dropped, median: {sorted(tails)[len(tails) // 2]}, max: {max(tails)}")


if __name__ == "__main__":
    asyncio.run(main())
