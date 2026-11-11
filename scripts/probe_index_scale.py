"""How much of the corpus does the query/index normalization mismatch cost?

Established: the index keeps diacritics, the query side strips them, so a
diacritic-bearing word in a CV can never be reached by a query this system
builds. This measures (a) whether case is handled (it is the assumption MR6
rests on), and (b) how many CV tokens are unreachable because of the mismatch.
"""
from __future__ import annotations

import asyncio
import unicodedata

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.modules.matching.retrieval.bm25 import tokenize_text


def has_marks(w: str) -> bool:
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFKD", w))


async def main() -> None:
    async with SessionFactory() as session:
        print("=== case: does the index fold it? ===")
        for q in ("Hùng", "hùng", "HÙNG"):
            r = (await session.execute(text(
                "SELECT pdb.score(id) AS s FROM user_cvs "
                "WHERE id = :i AND raw_text @@@ :q LIMIT 1"
            ), {"i": "e8fddc37-2389-4a1d-b9b7-e96bf4472ed7", "q": q})).mappings().one_or_none()
            hit = r is not None and r["s"] is not None
            print(f"  @@@ {q!r:<10} -> {'match' if hit else 'NO MATCH'}")

        print()
        print("=== scale: CV tokens the query side can never reach ===")
        rows = (await session.execute(text(
            "SELECT id, raw_text FROM user_cvs WHERE raw_text IS NOT NULL AND raw_text <> ''"
        ))).mappings().all()

        total_cv = len(rows)
        affected = 0
        tok_all = tok_lost = 0
        for r in rows:
            words = [w.strip(".,;:()[]/|") for w in (r["raw_text"] or "").split()]
            words = [w for w in words if w]
            lost = [w for w in words if has_marks(w)]
            tok_all += len(words)
            tok_lost += len(lost)
            if lost:
                affected += 1

        print(f"  CVs indexed                 : {total_cv}")
        print(f"  CVs holding diacritics      : {affected} ({100.0 * affected / max(1, total_cv):.0f}%)")
        print(f"  word occurrences            : {tok_all}")
        print(f"  unreachable from any query  : {tok_lost} ({100.0 * tok_lost / max(1, tok_all):.1f}%)")

        print()
        print("=== per CV ===")
        for r in rows:
            words = [w.strip(".,;:()[]/|") for w in (r["raw_text"] or "").split()]
            words = [w for w in words if w]
            lost = sum(1 for w in words if has_marks(w))
            if not words:
                continue
            pct = 100.0 * lost / len(words)
            if pct > 0:
                print(f"  {str(r['id'])[:8]}  {len(words):>5} words  {lost:>5} unreachable  {pct:5.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
