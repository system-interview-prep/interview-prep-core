"""How much of a Vietnamese CV the query half can actually reach.

An earlier one-off probe (``probe_index_diacritics``, deleted once the finding
was recorded in DOC_AND_PLAN/data/eval/matching/runs/phase3_fixes_20261114)
answered the question for a handful of words. This answers it for every marked
word in every Vietnamese CV of the corpus, which
is the number worth quoting: the share of a CV that the sparse half cannot
retrieve because the two halves disagree on what a token looks like.

A word counts as reachable when the token the query half would send for it -
the one ``tokenize_text`` produces - matches the indexed document. Run it
before and after the index change to get both columns.
"""

from __future__ import annotations

import asyncio
import unicodedata

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.modules.matching.retrieval.bm25 import tokenize_text


def is_marked(word: str) -> bool:
    decomposed = unicodedata.normalize("NFKD", word)
    return any(unicodedata.combining(c) for c in decomposed) or "đ" in word.lower()


async def main() -> None:
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, raw_text FROM user_cvs
                    WHERE raw_text ~ '[à-ỹĐđ]' AND raw_text <> ''
                    ORDER BY length(raw_text) DESC
                    """
                )
            )
        ).mappings().all()

        if not rows:
            print("no CV in the corpus carries Vietnamese marks")
            return

        print(f"{'CV':<40} {'marked words':>13} {'unreachable':>12} {'share':>8}")
        print("-" * 76)
        for row in rows:
            cv_id, raw = row["id"], row["raw_text"]
            words = {
                stripped
                for word in raw.split()
                if (stripped := word.strip(".,;:()[]/|")) and len(stripped) >= 3
                and is_marked(stripped)
            }
            if not words:
                continue
            unreachable = 0
            for word in sorted(words):
                tokens = tokenize_text(word, remove_stopwords=False)
                if not tokens:
                    unreachable += 1
                    continue
                hit = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM user_cvs "
                            "WHERE id = :i AND raw_text @@@ :q LIMIT 1"
                        ),
                        {"i": cv_id, "q": " ".join(tokens)},
                    )
                ).first()
                if hit is None:
                    unreachable += 1
            share = 100 * unreachable / len(words)
            print(f"{str(cv_id):<40} {len(words):>13} {unreachable:>12} {share:>7.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
