"""Does the ParadeDB index strip diacritics the way the query side does?

tokenize_text (bm25.py:52-53) NFKD-normalizes and drops combining marks, so
every BM25 query leaves this process without diacritics. The document side is
tokenized by ParadeDB itself. If the two disagree, a Vietnamese CV written with
diacritics cannot be retrieved by the query built from a Vietnamese posting -
the halves would never meet.
"""
from __future__ import annotations

import asyncio
import unicodedata

from sqlalchemy import text

from src.infrastructure.database import SessionFactory


def strip(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


async def main() -> None:
    async with SessionFactory() as session:
        # A CV that actually contains diacritics, so the question is well posed.
        row = (await session.execute(text(
            """
            SELECT id, raw_text FROM user_cvs
            WHERE raw_text ~ '[à-ỹ]' AND raw_text <> ''
            ORDER BY length(raw_text) DESC LIMIT 1
            """
        ))).mappings().one_or_none()

        if row is None:
            print("no CV in the corpus contains diacritics; nothing to compare")
            return

        cv_id, raw = row["id"], row["raw_text"]
        words = [w.strip(".,;:()[]") for w in raw.split()]
        marked = [w for w in words if any(unicodedata.combining(c)
                                          for c in unicodedata.normalize("NFKD", w))
                  and len(w) >= 3]
        if not marked:
            print("CV matched the pattern but yielded no usable word")
            return

        print(f"CV {cv_id}, {len(raw)} chars")
        print(f"{'word':<16} {'form':<12} {'@@@ matches?':<14} {'pdb.score':>10}")
        print("-" * 56)

        tested = 0
        for word in marked:
            if tested >= 6:
                break
            bare = strip(word)
            if bare == word:
                continue
            tested += 1
            for label, form in (("with marks", word), ("stripped", bare)):
                r = (await session.execute(text(
                    "SELECT pdb.score(id) AS s FROM user_cvs "
                    "WHERE id = :i AND raw_text @@@ :q LIMIT 1"
                ), {"i": cv_id, "q": form})).mappings().one_or_none()
                hit = r is not None and r["s"] is not None
                score = f"{float(r['s']):.4f}" if hit else "-"
                print(f"{word if label == 'with marks' else '':<16} {label:<12} "
                      f"{'YES' if hit else 'no':<14} {score:>10}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
