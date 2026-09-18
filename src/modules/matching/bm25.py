"""Okapi BM25 implementation tailored for bilingual (EN/VI) tech resume-job matching.

Features:
- Preserves technical tokens (C++, C#, .NET, Node.js, REST API, etc.) without corruption.
- Bilingual Unicode normalization (NFKD) and technical stopword filtering.
- Calibrated Okapi BM25 term frequency saturation (k1=1.5, b=0.75) bounded to [0.0, 1.0].
- Length-normalized penalization for verbose keyword stuffing while rewarding precise coverage.
- Bullet-level / Section-level MaxSim alignment for fine-grained evidence attribution.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Sequence

# Common English and Vietnamese stopwords that carry minimal discriminative value in matching
_DEFAULT_STOPWORDS = {
    # English
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "with",
    "by", "about", "against", "between", "into", "through", "during", "before",
    "after", "above", "below", "from", "up", "down", "of", "off", "over", "under",
    "again", "further", "then", "once", "here", "there", "when", "where", "why",
    "how", "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "s", "t", "can", "will", "just", "don", "should", "now", "be", "is", "are",
    "was", "were", "been", "being", "have", "has", "had", "having", "do", "does",
    "did", "doing", "would", "could", "must", "we", "you", "they", "our", "their",
    # Vietnamese
    "va", "hoac", "nhung", "trong", "tren", "tai", "den", "cho", "voi", "boi",
    "ve", "giua", "vao", "qua", "truoc", "sau", "tu", "cua", "cac", "nhung",
    "mot", "hai", "ba", "nay", "do", "khi", "noi", "sao", "tat_ca", "moi",
    "khong", "chi", "cung", "nhu", "rat", "se", "da", "dang", "duoc", "bi",
    "co", "la", "thi", "ma", "de", "chung_toi", "ban", "ho",
}

# Regex to preserve technical compound terms before token splitting
_TECH_TOKEN_RE = re.compile(
    r"(?:c\+\+|c\#|\.net|node\.js|vue\.js|react\.js|angular\.js|next\.js|nuxt\.js|"
    r"restful\s+api|rest\s+api|spring\s+boot|ci/cd|k8s|tcp/ip)"
    r"|[a-zA-Z0-9]+(?:[+#.-][a-zA-Z0-9]+)*",
    re.IGNORECASE,
)


def tokenize_text(text: str | None, remove_stopwords: bool = True) -> list[str]:
    """Tokenize technical text into normalized tokens, preserving technology keywords."""
    if not text:
        return []

    normalized = unicodedata.normalize("NFKD", text)
    clean_text = "".join(ch for ch in normalized if not unicodedata.combining(ch)).casefold()

    tokens: list[str] = []
    for match in _TECH_TOKEN_RE.finditer(clean_text):
        token = match.group(0).strip()
        # Canonicalize common technology aliases
        if token in {"js", "javascript"}:
            token = "javascript"
        elif token in {"ts", "typescript"}:
            token = "typescript"
        elif token in {"k8s", "kubernetes"}:
            token = "kubernetes"
        elif token in {"postgres", "postgresql"}:
            token = "postgresql"

        if remove_stopwords and (token in _DEFAULT_STOPWORDS or len(token) <= 1):
            continue
        tokens.append(token)

    return tokens


def bm25_similarity(
    query_text: str | None,
    document_text: str | None,
    k1: float = 1.5,
    b: float = 0.75,
    reference_length: float = 100.0,
) -> float:
    """Calculate calibrated BM25 similarity in range [0.0, 1.0] for a query and document pair.

    Balances query term coverage with term-frequency saturation and document length normalization.
    """
    if not query_text or not document_text:
        return 0.0

    q_tokens = tokenize_text(query_text)
    d_tokens = tokenize_text(document_text)

    if not q_tokens or not d_tokens:
        return 0.0

    doc_counter = Counter(d_tokens)
    q_unique = list(set(q_tokens))
    doc_len = len(d_tokens)

    # BM25 document length normalization factor
    len_norm = 1.0 - b + b * (doc_len / max(1.0, reference_length))

    score = 0.0
    max_score = 0.0

    for term in q_unique:
        # Technical keywords and longer terms get higher specificity weight
        term_weight = 1.0 + min(1.0, len(term) / 8.0)
        tf = doc_counter.get(term, 0)

        # Okapi BM25 TF saturation
        tf_saturation = (tf * (k1 + 1.0)) / (tf + k1 * len_norm)
        # Bounded contribution per term
        score += term_weight * min(1.0, tf_saturation)
        max_score += term_weight

    if max_score <= 0.0:
        return 0.0

    # Coverage ratio scaled to [0.0, 1.0]
    normalized = score / max_score
    return round(min(1.0, max(0.0, normalized)), 4)


def bm25_section_maxsim(
    query_bullets: Sequence[str],
    doc_bullets: Sequence[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Calculate Section-wise / Bullet-wise MaxSim BM25 alignment.

    For each requirement bullet in JD, finds the best matching bullet in CV,
    then averages the maximum similarities across all requirements.
    """
    valid_queries = [b for b in query_bullets if b and b.strip()]
    valid_docs = [b for b in doc_bullets if b and b.strip()]

    if not valid_queries or not valid_docs:
        return 0.0

    bullet_scores = []
    for q in valid_queries:
        best_sim = max(
            (bm25_similarity(q, d, k1=k1, b=b, reference_length=30.0) for d in valid_docs),
            default=0.0,
        )
        bullet_scores.append(best_sim)

    if not bullet_scores:
        return 0.0

    return round(sum(bullet_scores) / len(bullet_scores), 4)
