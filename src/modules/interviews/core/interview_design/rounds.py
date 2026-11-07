"""The rounds a real interview covers, and the job keywords that are not one.

A real backend interview walks through a few rounds: a project from the CV,
the fundamentals of the stack (language and framework, databases, APIs),
running the system, and a story about working with people (research 20/10,
PROGRESS_EVIDENCE_RESEARCH, "Ứng viên có quan tâm không"). Interview question
lists for backend roles in Vietnam group questions that way and have no topic
of their own for JSON or XML (ITviec, "Top 30+ câu hỏi phỏng vấn Backend").

The planner used each keyword the job post resolved to as one technical
topic, so a post that mentions "JSON, XML" spent questions on them and the
progress page then showed "XML 0.3/10". Here:

* ``area_of``: the round a concept belongs to. The planner spreads technical
  questions over different rounds; the progress page groups skills by it.
* ``MINOR_CONCEPTS``: keywords a job post lists that interviewers do not ask
  about on their own. They are kept out of the question plan when the job has
  other skills to ask about (they are still checked against the CV).
"""

from __future__ import annotations

# Rounds, in display order. Keys are stable; the frontend has their names.
AREAS = ("project", "language", "framework", "data", "api", "infra", "ai", "teamwork", "general", "other")

_AREA_IDS: dict[str, tuple[str, ...]] = {
    "language": (
        "skill-python",
        "skill-java",
        "skill-kotlin",
        "skill-javascript",
        "skill-typescript",
        "skill-csharp",
        "skill-cpp",
        "skill-php",
        "skill-html",
        "skill-css",
    ),
    "framework": (
        "skill-spring-boot",
        "skill-fastapi",
        "skill-django",
        "skill-flask",
        "skill-nodejs",
        "skill-dotnet",
        "skill-react",
        "skill-angular",
        "skill-android",
        "skill-ios",
        "skill-unity",
    ),
    "data": (
        "skill-sql",
        "skill-postgresql",
        "skill-mysql",
        "skill-oracle",
        "skill-mongodb",
        "skill-redis",
        "skill-kafka",
    ),
    "api": ("skill-http", "skill-rest-api", "skill-json", "skill-xml"),
    "infra": (
        "skill-docker",
        "skill-kubernetes",
        "skill-container-orchestrator",
        "skill-aws",
        "skill-azure",
        "skill-gcp",
        "skill-linux",
        "skill-cicd",
        "skill-monitoring",
        "skill-git",
    ),
    "ai": (
        "skill-artificial-intelligence",
        "skill-machine-learning",
        "skill-natural-language-processing",
        "skill-generative-ai",
        "skill-large-language-models",
        "skill-retrieval-augmented-generation",
        "skill-langgraph",
        "skill-gemini",
        "skill-tensorflow",
        "skill-numpy",
        "skill-pandas",
        "skill-semantic-search",
        "skill-bm25",
        "skill-tf-idf",
        "skill-reciprocal-rank-fusion",
        "skill-named-entity-recognition",
    ),
    "project": ("cv-validation",),
    "teamwork": ("soft-skill.star",),
}
_AREA_OF: dict[str, str] = {cid: area for area, ids in _AREA_IDS.items() for cid in ids}

# Data formats and everyday tools: a job post lists them, an interviewer asks
# about them inside another topic (JSON inside API design, Git inside how the
# team works), not as a topic.
MINOR_CONCEPTS = frozenset(
    {"skill-json", "skill-xml", "skill-git", "skill-jira", "skill-excel", "skill-ms-office"}
)

# Rounds made of technical questions, which a session can focus on. Project
# and teamwork questions are in every session already.
FOCUSABLE_AREAS = frozenset({"language", "framework", "data", "api", "infra", "ai"})

_GENERAL_PREFIX = "technology"


def area_of(concept_id: str | None) -> str:
    key = str(concept_id or "")
    if key in _AREA_OF:
        return _AREA_OF[key]
    if key == _GENERAL_PREFIX or key.startswith(f"{_GENERAL_PREFIX}."):
        return "general"
    if key.startswith("soft-skill"):
        return "teamwork"
    return "other"


def is_minor(concept_id: str | None) -> bool:
    return str(concept_id or "") in MINOR_CONCEPTS


def spread_key(concept_id: str) -> str:
    """What two technical questions should differ in: the round, when known.

    A concept outside the catalog is its own key, so unknown skills are never
    merged with each other.
    """
    area = area_of(concept_id)
    return f"concept:{concept_id}" if area in {"other", "general"} else f"area:{area}"
