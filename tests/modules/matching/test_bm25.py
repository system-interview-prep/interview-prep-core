from src.modules.matching.bm25 import (
    bm25_section_maxsim,
    bm25_similarity,
    tokenize_text,
)


def test_tokenize_preserves_tech_tokens_and_filters_stopwords() -> None:
    text = "We are seeking a Senior C++ and .NET Developer with Node.js & Spring Boot experience."
    tokens = tokenize_text(text)

    assert "c++" in tokens
    assert ".net" in tokens
    assert "node.js" in tokens
    assert "spring boot" in tokens
    assert "developer" in tokens
    # Stopwords like "we", "are", "a", "and", "with" must be filtered
    assert "we" not in tokens
    assert "and" not in tokens
    assert "with" not in tokens


def test_tokenize_canonicalizes_common_aliases() -> None:
    text = "Proficient in JS, TS, K8s, and Postgres database."
    tokens = tokenize_text(text)

    assert "javascript" in tokens
    assert "typescript" in tokens
    assert "kubernetes" in tokens
    assert "postgresql" in tokens


def test_bm25_similarity_identical_text() -> None:
    text = "Python FastAPI Docker PostgreSQL microservices architecture"
    score = bm25_similarity(text, text)
    assert score == 1.0


def test_bm25_similarity_orthogonal_text() -> None:
    query = "Senior Python Developer with FastAPI and Docker"
    doc = "Graphic designer creating banners with Photoshop, Illustrator, and Canva"
    score = bm25_similarity(query, doc)
    assert score == 0.0


def test_bm25_similarity_partial_match() -> None:
    query = "Python, FastAPI, Docker, Kubernetes, AWS, PostgreSQL"
    doc = "Python developer with experience in FastAPI and Docker deployments"
    score = bm25_similarity(query, doc)

    assert 0.4 <= score <= 0.8


def test_bm25_section_maxsim() -> None:
    jd_bullets = [
        "Develop RESTful APIs using Python and FastAPI",
        "Deploy containerized microservices via Docker and Kubernetes",
        "Manage relational databases using PostgreSQL",
    ]
    cv_bullets = [
        "Architected scalable backend APIs with Python and FastAPI framework",
        "Created CI/CD pipeline and containerized services using Docker",
        "Designed schema and optimized queries on PostgreSQL and MySQL",
    ]
    score = bm25_section_maxsim(jd_bullets, cv_bullets)
    assert score > 0.3


def test_bm25_handles_empty_inputs_safely() -> None:
    assert bm25_similarity(None, "Valid text") == 0.0
    assert bm25_similarity("Valid text", "") == 0.0
    assert bm25_similarity("", "") == 0.0
    assert bm25_section_maxsim([], ["Some text"]) == 0.0
    assert bm25_section_maxsim(["Some text"], []) == 0.0
