"""Is MR2's residual a Vietnamese tokenizer problem, or a property of BM25 itself?

Earlier probe: appending Vietnamese to a posting raised the sparse score, while
appending English or nonsense left it untouched. I read that as "the Vietnamese
syllable tokenizer is the cause". That reading is testable, and this tests it.

pdb.score is a SUM over query terms. If that is what drives the rise, then
appending ENGLISH text that the CV happens to contain must raise the score just
as much - the language is irrelevant, only whether the added terms occur in the
document. If instead English-in-the-CV leaves the score flat, the tokenizer
really is the cause.

Also measured: sanitize_query keeps only the first 100 tokens, so a posting
longer than that silently drops its tail. Appending a requirement can then push
existing terms off the end and change which terms are searched at all.

Run inside the backend container:
    docker compose exec -T -e PYTHONPATH=/app -w /app backend \
        python scripts/probe_bm25_monotone.py
"""
from __future__ import annotations

import asyncio
import json
import re

from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.modules.matching.retrieval.bm25 import tokenize_text
from src.modules.matching.retrieval.bm25_provider import ParadeDbBm25Provider

CV_ID = "f44df223-e638-41c2-ad92-6a289ef7a844"  # CV10, the pair that violated MR2
JD_ID = "d9db33cf-9f22-4bae-9cfa-05371704f03a"

SAT_K = 15.0


def norm(raw: float) -> float:
    return raw / (raw + SAT_K)


async def raw_score(session, query: str) -> float | None:
    clean = ParadeDbBm25Provider.sanitize_query(query)
    if not clean:
        return None
    sql = text(
        """
        SELECT (SELECT pdb.score(id) FROM user_cvs
                WHERE id = :cv_id AND raw_text @@@ :q LIMIT 1) AS s
        """
    )
    row = (await session.execute(sql, {"cv_id": CV_ID, "q": clean})).mappings().one_or_none()
    if row is None or row["s"] is None:
        return 0.0
    return float(row["s"])


async def main() -> None:
    async with SessionFactory() as session:
        cv = (
            await session.execute(
                text("SELECT raw_text FROM user_cvs WHERE id = :i"), {"i": CV_ID}
            )
        ).scalar_one()

        jd = (
            await session.execute(
                text("SELECT title, structured_data FROM job_descriptions WHERE id = :i"),
                {"i": JD_ID},
            )
        ).mappings().one_or_none()

        # Base query: the posting text the facade would build, in its sorted form.
        if jd:
            data = jd["structured_data"] or {}
            if isinstance(data, str):
                data = json.loads(data)
            parts = [jd["title"] or ""]
            reqs = data.get("requirements") or []
            parts.extend(sorted(str(r.get("rawLabel") or "") for r in reqs))
            base = "\n".join(p for p in parts if p)
        else:
            base = ""
        if not base.strip():
            # No posting found: any realistic query serves, the question is about
            # what APPENDING does, not about the base itself.
            base = "Software Engineer\nBachelor degree in Computer Science\nExperience with Python"

        cv_tokens = tokenize_text(cv)
        base_tokens = tokenize_text(base)

        print("=" * 72)
        print(f"CV10 raw_text: {len(cv)} chars, {len(cv_tokens)} tokens")
        print(f"base query   : {len(base_tokens)} tokens "
              f"-> sanitize_query keeps the first 100 "
              f"({'TRUNCATED, tail dropped' if len(base_tokens) > 100 else 'all kept'})")
        print("=" * 72)

        # Pick real phrases from the CV so 'present in the document' is not a guess.
        cv_vocab = {t.lower() for t in cv_tokens}
        # Longest tokens present in the CV: rare -> high IDF -> biggest effect.
        rare_in_cv = sorted({t.lower() for t in cv_tokens if len(t) >= 6}, key=len, reverse=True)[:8]

        probes = [
            ("control (unchanged)", ""),
            ("EN words TAKEN FROM the CV", " ".join(rare_in_cv)),
            ("EN words NOT in the CV", "zygote quixotic borborygmus kerfuffle"),
            ("VI with diacritics", "Kinh nghiem van hanh lo phan ung hat nhan".replace(
                "Kinh nghiem van hanh lo phan ung hat nhan",
                "Kinh nghiệm vận hành lò phản ứng hạt nhân")),
            ("VI without diacritics", "Kinh nghiem van hanh lo phan ung hat nhan"),
        ]

        print(f"{'probe appended to the posting':<34} {'raw':>9} {'norm':>8} {'delta':>9}")
        print("-" * 72)
        control_raw = None
        for label, extra in probes:
            q = base if not extra else base + "\n" + extra
            r = await raw_score(session, q)
            if control_raw is None:
                control_raw = r
            n = norm(r)
            delta = n - norm(control_raw)
            print(f"{label:<34} {r:9.4f} {n:8.4f} {delta:+9.4f}")

        print()
        print("Words taken from the CV:", ", ".join(rare_in_cv))
        print()

        # Truncation: does appending push existing terms past the 100-token cut?
        if len(base_tokens) > 90:
            short = " ".join(base_tokens[:100])
            long_q = base + "\n" + " ".join(rare_in_cv)
            kept_before = ParadeDbBm25Provider.sanitize_query(base).split()
            kept_after = ParadeDbBm25Provider.sanitize_query(long_q).split()
            dropped = [t for t in kept_before if t not in kept_after]
            print(f"terms searched before append: {len(kept_before)}")
            print(f"terms searched after  append: {len(kept_after)}")
            print(f"terms DROPPED by the 100-token cut when appending: {len(dropped)}")
            if dropped:
                print("  ", ", ".join(dropped[:15]))
        else:
            print(f"base is only {len(base_tokens)} tokens: the 100-token cut is not reached, "
                  "so truncation plays no part in THIS pair (it would in a longer posting)")


if __name__ == "__main__":
    asyncio.run(main())
