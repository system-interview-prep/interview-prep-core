"""The one skill catalogue shared by the JD parser, CV parser and seeded taxonomy.

Before this module the JD parser, CV parser, career rules, a migration and the
Question Bank each kept their own skill list, so a CV and a JD could resolve the
same skill to different concept ids (``skill-cpp`` vs ``skill-cplusplus``) or
one side could not see it at all (Kotlin in CVs). Every consumer now derives
from :data:`SKILL_CATALOG`; the database copy is synced from it additively, so
admin edits in the database are never overwritten.

``broader`` lists the more general skills a question can fall back to, and
``roles`` the career specialisations (``career.py`` codes) the skill signals.
Cross-cutting skills (Git, HTTP, JSON) signal no role, so they cannot tip a
classification on their own.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

BACKEND = "technology.software-engineering.backend"
FRONTEND = "technology.software-engineering.frontend"
MOBILE = "technology.software-engineering.mobile"
DEVOPS = "technology.cloud-devops"
AI = "technology.artificial-intelligence"
GAME = "technology.game-development"


@dataclass(frozen=True)
class SkillEntry:
    label: str
    aliases: tuple[str, ...]
    broader: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


# Aliases are matched case-insensitively on word boundaries. Aliases in
# LIST_ONLY_ALIASES are also ordinary English ("you excel at", "the rest of the
# team"), so they only count as an item of a skill list: at a line start or
# after a separator, and followed by a separator or the line end.
SKILL_CATALOG: dict[str, SkillEntry] = {
    # Languages
    "skill-python": SkillEntry("Python", ("python",), roles=(BACKEND, AI)),
    "skill-java": SkillEntry("Java", ("java",), roles=(BACKEND, MOBILE)),
    "skill-kotlin": SkillEntry("Kotlin", ("kotlin",), roles=(MOBILE,)),
    "skill-javascript": SkillEntry("JavaScript", ("javascript", "js"), roles=(FRONTEND,)),
    "skill-typescript": SkillEntry("TypeScript", ("typescript",), ("skill-javascript",), (FRONTEND,)),
    "skill-csharp": SkillEntry("C#", ("c#", "csharp"), roles=(BACKEND, GAME)),
    "skill-cpp": SkillEntry("C++", ("c++", "cpp"), roles=(GAME,)),
    "skill-php": SkillEntry("PHP", ("php",), roles=(BACKEND,)),
    "skill-sql": SkillEntry("SQL", ("sql",), roles=(BACKEND,)),
    "skill-html": SkillEntry("HTML", ("html",), roles=(FRONTEND,)),
    "skill-css": SkillEntry("CSS", ("css",), roles=(FRONTEND,)),
    # Frameworks and runtimes
    "skill-spring-boot": SkillEntry(
        "Spring Boot", ("spring boot", "springboot"), ("skill-java",), (BACKEND,)
    ),
    "skill-fastapi": SkillEntry("FastAPI", ("fastapi",), ("skill-python",), (BACKEND,)),
    "skill-django": SkillEntry("Django", ("django",), ("skill-python",), (BACKEND,)),
    "skill-flask": SkillEntry("Flask", ("flask",), ("skill-python",), (BACKEND,)),
    "skill-nodejs": SkillEntry("Node.js", ("node.js", "nodejs"), ("skill-javascript",), (BACKEND,)),
    "skill-dotnet": SkillEntry(
        ".NET", (".net", "dotnet", "asp.net", "aspnet"), ("skill-csharp",), (BACKEND,)
    ),
    "skill-react": SkillEntry("React", ("react", "reactjs", "react.js"), ("skill-javascript",), (FRONTEND,)),
    "skill-angular": SkillEntry(
        "Angular", ("angular", "angularjs", "angular.js"), ("skill-typescript",), (FRONTEND,)
    ),
    "skill-android": SkillEntry("Android", ("android",), roles=(MOBILE,)),
    "skill-ios": SkillEntry("iOS", ("ios",), roles=(MOBILE,)),
    "skill-unity": SkillEntry("Unity", ("unity", "unity engine"), ("skill-csharp",), (GAME,)),
    # Data stores and messaging
    "skill-postgresql": SkillEntry("PostgreSQL", ("postgresql", "postgres"), ("skill-sql",), (BACKEND,)),
    "skill-mysql": SkillEntry("MySQL", ("mysql",), ("skill-sql",), (BACKEND,)),
    "skill-oracle": SkillEntry("Oracle Database", ("oracle", "oracle database"), ("skill-sql",), (BACKEND,)),
    "skill-mongodb": SkillEntry("MongoDB", ("mongodb",), roles=(BACKEND,)),
    "skill-redis": SkillEntry("Redis", ("redis",), roles=(BACKEND,)),
    "skill-kafka": SkillEntry("Kafka", ("kafka",), roles=(BACKEND,)),
    # Web and data formats
    "skill-http": SkillEntry("HTTP", ("http",)),
    "skill-rest-api": SkillEntry(
        "REST API", ("rest", "rest api", "restful", "restful api"), ("skill-http",), (BACKEND,)
    ),
    "skill-json": SkillEntry("JSON", ("json",)),
    "skill-xml": SkillEntry("XML", ("xml",)),
    # Cloud and operations
    "skill-docker": SkillEntry("Docker", ("docker",), roles=(DEVOPS,)),
    "skill-kubernetes": SkillEntry(
        "Kubernetes", ("kubernetes", "k8s"), ("skill-container-orchestrator",), (DEVOPS,)
    ),
    "skill-container-orchestrator": SkillEntry(
        "Kubernetes or Docker Swarm", ("container orchestration",), ("skill-docker",), (DEVOPS,)
    ),
    "skill-aws": SkillEntry("AWS", ("aws", "amazon web services"), roles=(DEVOPS,)),
    "skill-azure": SkillEntry("Microsoft Azure", ("azure", "microsoft azure"), roles=(DEVOPS,)),
    "skill-gcp": SkillEntry("Google Cloud Platform", ("gcp", "google cloud platform"), roles=(DEVOPS,)),
    "skill-linux": SkillEntry("Linux", ("linux",), roles=(DEVOPS,)),
    "skill-cicd": SkillEntry(
        "CI/CD",
        ("ci/cd", "continuous integration", "continuous delivery", "continuous deployment"),
        roles=(DEVOPS,),
    ),
    "skill-monitoring": SkillEntry(
        "Monitoring", ("monitoring", "observability", "telemetry"), roles=(DEVOPS,)
    ),
    "skill-git": SkillEntry("Git", ("git",)),
    # AI and search
    "skill-artificial-intelligence": SkillEntry(
        "Artificial Intelligence", ("artificial intelligence", "ai"), roles=(AI,)
    ),
    "skill-machine-learning": SkillEntry(
        "Machine Learning", ("machine learning", "ml"), ("skill-artificial-intelligence",), (AI,)
    ),
    "skill-natural-language-processing": SkillEntry(
        "Natural Language Processing",
        ("natural language processing", "nlp"),
        ("skill-machine-learning",),
        (AI,),
    ),
    "skill-generative-ai": SkillEntry(
        "Generative AI", ("generative ai", "genai", "gen ai"), ("skill-artificial-intelligence",), (AI,)
    ),
    "skill-large-language-models": SkillEntry(
        "Large Language Models",
        ("large language models", "large language model", "llms", "llm"),
        ("skill-generative-ai",),
        (AI,),
    ),
    "skill-retrieval-augmented-generation": SkillEntry(
        "Retrieval-Augmented Generation",
        ("retrieval-augmented generation", "retrieval augmented generation", "rag"),
        ("skill-large-language-models",),
        (AI,),
    ),
    "skill-langgraph": SkillEntry("LangGraph", ("langgraph",), ("skill-large-language-models",), (AI,)),
    "skill-gemini": SkillEntry(
        "Gemini", ("gemini", "google gemini"), ("skill-large-language-models",), (AI,)
    ),
    "skill-tensorflow": SkillEntry("TensorFlow", ("tensorflow",), ("skill-machine-learning",), (AI,)),
    "skill-numpy": SkillEntry("NumPy", ("numpy",), ("skill-python",), (AI,)),
    "skill-pandas": SkillEntry("pandas", ("pandas",), ("skill-python",), (AI,)),
    "skill-semantic-search": SkillEntry(
        "Semantic Search", ("semantic search",), ("skill-natural-language-processing",), (AI,)
    ),
    "skill-bm25": SkillEntry("BM25", ("bm25",), ("skill-semantic-search",), (AI,)),
    "skill-tf-idf": SkillEntry("TF-IDF", ("tf-idf", "tf idf"), ("skill-semantic-search",), (AI,)),
    "skill-reciprocal-rank-fusion": SkillEntry(
        "Reciprocal Rank Fusion", ("reciprocal rank fusion", "rrf"), ("skill-semantic-search",), (AI,)
    ),
    "skill-named-entity-recognition": SkillEntry(
        "Named Entity Recognition",
        ("named entity recognition", "ner"),
        ("skill-natural-language-processing",),
        (AI,),
    ),
    # Tools
    "skill-jira": SkillEntry("Jira", ("jira",)),
    "skill-selenium": SkillEntry("Selenium", ("selenium",)),
    "skill-excel": SkillEntry("Microsoft Excel", ("excel", "ms excel", "microsoft excel")),
    "skill-ms-office": SkillEntry("Microsoft Office", ("ms office", "microsoft office")),
    "skill-autocad": SkillEntry("AutoCAD", ("autocad",)),
}

LIST_ONLY_ALIASES = frozenset({"excel", "rest"})



def skill_aliases() -> dict[str, tuple[str, tuple[str, ...]]]:
    """The ``{concept_id: (label, aliases)}`` shape every parser consumes."""
    return {concept_id: (entry.label, entry.aliases) for concept_id, entry in SKILL_CATALOG.items()}


def role_skill_ids(role_code: str) -> frozenset[str]:
    return frozenset(concept_id for concept_id, entry in SKILL_CATALOG.items() if role_code in entry.roles)


_SEPARATORS = r"|,;/•·()\[\]:\-"


@lru_cache(maxsize=4096)
def alias_pattern(alias: str) -> re.Pattern[str]:
    """Word-bounded, case-insensitive matcher for one alias."""
    escaped = re.escape(alias)
    if alias in LIST_ONLY_ALIASES:
        before = rf"(?:^|(?<=[{_SEPARATORS}])|(?<=[{_SEPARATORS}] ))"
        after = rf"(?=[ ]?(?:$|[{_SEPARATORS}.]))"
        return re.compile(rf"{before}{escaped}{after}", re.IGNORECASE | re.MULTILINE)
    # "js" must not match the tail of "node.js".
    before = r"(?<![\w.])" if alias == "js" else r"(?<!\w)"
    return re.compile(rf"{before}{escaped}(?!\w)", re.IGNORECASE)
