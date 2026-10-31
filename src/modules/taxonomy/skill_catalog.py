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
EMBEDDED = "technology.software-engineering.embedded"
QA = "technology.quality-assurance"
DATA = "technology.data-engineering"
SECURITY = "technology.cybersecurity"


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
    "skill-kotlin": SkillEntry("Kotlin", ("kotlin",), roles=(MOBILE, BACKEND)),
    "skill-javascript": SkillEntry("JavaScript", ("javascript", "js"), roles=(FRONTEND,)),
    "skill-typescript": SkillEntry("TypeScript", ("typescript",), ("skill-javascript",), (FRONTEND,)),
    "skill-csharp": SkillEntry("C#", ("c#", "csharp"), roles=(BACKEND, GAME)),
    "skill-cpp": SkillEntry("C++", ("c++", "cpp"), roles=(GAME, EMBEDDED)),
    "skill-c": SkillEntry(
        "C", ("c", "c language", "c programming", "embedded c", "ngôn ngữ c", "lập trình c"),
        roles=(EMBEDDED,),
    ),
    "skill-go": SkillEntry("Go", ("golang", "go"), roles=(BACKEND,)),
    "skill-rust": SkillEntry("Rust", ("rust",), roles=(BACKEND, EMBEDDED)),
    "skill-ruby": SkillEntry("Ruby", ("ruby",), roles=(BACKEND,)),
    "skill-swift": SkillEntry("Swift", ("swift",), roles=(MOBILE,)),
    "skill-dart": SkillEntry("Dart", ("dart",), roles=(MOBILE,)),
    "skill-php": SkillEntry("PHP", ("php",), roles=(BACKEND,)),
    "skill-sql": SkillEntry("SQL", ("sql",), roles=(BACKEND, DATA)),
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
    "skill-vue": SkillEntry("Vue.js", ("vue", "vuejs", "vue.js"), ("skill-javascript",), (FRONTEND,)),
    "skill-nextjs": SkillEntry("Next.js", ("next.js", "nextjs"), ("skill-react",), (FRONTEND,)),
    "skill-nestjs": SkillEntry("NestJS", ("nestjs", "nest.js"), ("skill-nodejs",), (BACKEND,)),
    "skill-expressjs": SkillEntry("Express.js", ("express.js", "expressjs"), ("skill-nodejs",), (BACKEND,)),
    "skill-laravel": SkillEntry("Laravel", ("laravel",), ("skill-php",), (BACKEND,)),
    "skill-ruby-on-rails": SkillEntry(
        "Ruby on Rails", ("ruby on rails", "rails"), ("skill-ruby",), (BACKEND,)
    ),
    "skill-graphql": SkillEntry("GraphQL", ("graphql",), roles=(BACKEND,)),
    "skill-microservices": SkillEntry(
        "Microservices", ("microservices", "microservice", "micro-services"), roles=(BACKEND,)
    ),
    "skill-figma": SkillEntry("Figma", ("figma",), roles=(FRONTEND,)),
    "skill-android": SkillEntry("Android", ("android",), roles=(MOBILE,)),
    "skill-ios": SkillEntry("iOS", ("ios",), roles=(MOBILE,)),
    "skill-flutter": SkillEntry("Flutter", ("flutter",), ("skill-dart",), (MOBILE,)),
    "skill-react-native": SkillEntry("React Native", ("react native",), ("skill-react",), (MOBILE,)),
    "skill-unity": SkillEntry("Unity", ("unity", "unity engine"), ("skill-csharp",), (GAME,)),
    # Data stores and messaging
    "skill-postgresql": SkillEntry("PostgreSQL", ("postgresql", "postgres"), ("skill-sql",), (BACKEND,)),
    "skill-mysql": SkillEntry("MySQL", ("mysql",), ("skill-sql",), (BACKEND,)),
    "skill-oracle": SkillEntry("Oracle Database", ("oracle", "oracle database"), ("skill-sql",), (BACKEND,)),
    "skill-mongodb": SkillEntry("MongoDB", ("mongodb",), roles=(BACKEND,)),
    "skill-redis": SkillEntry("Redis", ("redis",), roles=(BACKEND,)),
    "skill-kafka": SkillEntry("Kafka", ("kafka",), roles=(BACKEND, DATA)),
    "skill-sql-server": SkillEntry(
        "SQL Server", ("sql server", "mssql", "microsoft sql server"), ("skill-sql",), (BACKEND,)
    ),
    "skill-elasticsearch": SkillEntry("Elasticsearch", ("elasticsearch", "elastic search"), roles=(BACKEND,)),
    "skill-rabbitmq": SkillEntry("RabbitMQ", ("rabbitmq",), roles=(BACKEND,)),
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
        "Monitoring",
        (
            "monitoring",
            "monitoring tools",
            "monitoring and alerting",
            "monitoring and observability",
            "observability",
            "telemetry",
        ),
        roles=(DEVOPS,),
    ),
    "skill-terraform": SkillEntry("Terraform", ("terraform",), roles=(DEVOPS,)),
    "skill-ansible": SkillEntry("Ansible", ("ansible",), roles=(DEVOPS,)),
    "skill-jenkins": SkillEntry("Jenkins", ("jenkins",), ("skill-cicd",), (DEVOPS,)),
    "skill-nginx": SkillEntry("Nginx", ("nginx",), roles=(DEVOPS,)),
    "skill-networking": SkillEntry(
        "Computer Networking", ("networking", "tcp/ip", "ccna", "mạng máy tính"), roles=(DEVOPS, SECURITY)
    ),
    "skill-git": SkillEntry("Git", ("git",)),
    # Computer science fundamentals: cross-cutting, like Git.
    "skill-oop": SkillEntry(
        "Object-Oriented Programming",
        ("oop", "object-oriented programming", "object oriented programming", "lập trình hướng đối tượng"),
    ),
    "skill-data-structures-algorithms": SkillEntry(
        "Data Structures and Algorithms",
        ("data structures", "algorithms", "data structures and algorithms", "cấu trúc dữ liệu", "giải thuật"),
    ),
    "skill-design-patterns": SkillEntry(
        "Design Patterns", ("design patterns", "design pattern"), ("skill-oop",)
    ),
    # Embedded systems
    "skill-microcontroller": SkillEntry(
        "Microcontrollers",
        ("microcontroller", "microcontrollers", "mcu", "stm32", "arduino", "vi điều khiển"),
        roles=(EMBEDDED,),
    ),
    "skill-rtos": SkillEntry("RTOS", ("rtos", "freertos", "real-time operating system"), roles=(EMBEDDED,)),
    "skill-embedded-linux": SkillEntry(
        "Embedded Linux", ("embedded linux", "yocto"), ("skill-linux",), (EMBEDDED,)
    ),
    "skill-communication-protocols": SkillEntry(
        "Communication Protocols", ("uart", "i2c", "can bus", "modbus"), roles=(EMBEDDED,)
    ),
    # Testing and quality assurance
    "skill-manual-testing": SkillEntry(
        "Manual Testing", ("manual testing", "manual test", "test case", "test cases", "kiểm thử thủ công"),
        roles=(QA,),
    ),
    "skill-automation-testing": SkillEntry(
        "Automation Testing",
        ("automation testing", "test automation", "automation test", "kiểm thử tự động"),
        roles=(QA,),
    ),
    "skill-api-testing": SkillEntry("API Testing", ("api testing", "postman"), roles=(QA,)),
    "skill-cypress": SkillEntry("Cypress", ("cypress",), ("skill-automation-testing",), (QA,)),
    "skill-playwright": SkillEntry("Playwright", ("playwright",), ("skill-automation-testing",), (QA,)),
    "skill-jmeter": SkillEntry("JMeter", ("jmeter", "performance testing", "load testing"), roles=(QA,)),
    "skill-unit-testing": SkillEntry(
        "Unit Testing", ("unit test", "unit testing", "unit tests", "junit", "pytest", "jest")
    ),
    # Data engineering and analytics
    "skill-spark": SkillEntry("Apache Spark", ("apache spark", "spark", "pyspark"), roles=(DATA,)),
    "skill-hadoop": SkillEntry("Hadoop", ("hadoop",), roles=(DATA,)),
    "skill-airflow": SkillEntry("Apache Airflow", ("airflow", "apache airflow"), roles=(DATA,)),
    "skill-etl": SkillEntry("ETL", ("etl", "elt", "data pipeline", "data pipelines"), roles=(DATA,)),
    "skill-data-warehouse": SkillEntry(
        "Data Warehouse", ("data warehouse", "data warehousing", "bigquery", "snowflake"), roles=(DATA,)
    ),
    "skill-power-bi": SkillEntry("Power BI", ("power bi", "powerbi"), roles=(DATA,)),
    "skill-tableau": SkillEntry("Tableau", ("tableau",), roles=(DATA,)),
    # Security
    "skill-network-security": SkillEntry(
        "Network Security",
        ("network security", "firewall", "ids/ips", "bảo mật mạng"),
        ("skill-networking",),
        (SECURITY,),
    ),
    "skill-penetration-testing": SkillEntry(
        "Penetration Testing", ("penetration testing", "pentest", "pentesting"), roles=(SECURITY,)
    ),
    "skill-web-security": SkillEntry(
        "Web Security", ("owasp", "web security", "xss", "sql injection"), roles=(SECURITY,)
    ),
    "skill-siem": SkillEntry("SIEM", ("siem", "splunk"), roles=(SECURITY,)),
    # AI and search
    "skill-artificial-intelligence": SkillEntry(
        "Artificial Intelligence",
        # Bare "ai" is list-only; in prose only "AI + engineering noun" means the skill
        # ("building AI systems"), unlike "AI-assisted tools".
        (
            "artificial intelligence",
            "ai",
            "agentic ai",
            "ai agents",
            "ai systems",
            "ai models",
            "ai applications",
            "ai solutions",
            "ai products",
            "ai engineering",
        ),
        roles=(AI,),
    ),
    "skill-machine-learning": SkillEntry(
        "Machine Learning",
        ("machine learning", "ml", "ml models", "ml systems", "ml pipelines", "ml engineering"),
        ("skill-artificial-intelligence",),
        (AI,),
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
    "skill-pytorch": SkillEntry("PyTorch", ("pytorch",), ("skill-machine-learning",), (AI,)),
    "skill-scikit-learn": SkillEntry(
        "scikit-learn", ("scikit-learn", "sklearn"), ("skill-machine-learning",), (AI,)
    ),
    "skill-deep-learning": SkillEntry(
        "Deep Learning", ("deep learning",), ("skill-machine-learning",), (AI,)
    ),
    "skill-computer-vision": SkillEntry(
        "Computer Vision", ("computer vision", "opencv"), ("skill-machine-learning",), (AI,)
    ),
    "skill-numpy": SkillEntry("NumPy", ("numpy",), ("skill-python",), (AI,)),
    "skill-pandas": SkillEntry("pandas", ("pandas",), ("skill-python",), (AI, DATA)),
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
    "skill-selenium": SkillEntry("Selenium", ("selenium",), ("skill-automation-testing",), (QA,)),
    "skill-excel": SkillEntry("Microsoft Excel", ("excel", "ms excel", "microsoft excel"), roles=(DATA,)),
    "skill-ms-office": SkillEntry("Microsoft Office", ("ms office", "microsoft office")),
    "skill-autocad": SkillEntry("AutoCAD", ("autocad",)),
}

# Short or everyday words that are a skill only inside a list ("Python, AI, ML",
# "- Monitoring"), never in prose: "AI-assisted tools", "post-release
# monitoring" made AI/Monitoring must-have targets of plain backend JDs.
# "c" and "go" are letters/verbs in prose; "C#" and "C++" never match "c"
# because "#" and "+" are not separators.
LIST_ONLY_ALIASES = frozenset({
    "excel", "rest", "ai", "ml", "monitoring", "c", "go", "rust", "swift", "dart", "spark",
    "rails", "networking", "algorithms", "jest", "elt", "mcu",
})



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
    # The DB taxonomy also lists each label as an alias ("Monitoring"); the
    # match is case-insensitive, so the list-only rule must be too.
    if alias.casefold() in LIST_ONLY_ALIASES:
        before = rf"(?:^|(?<=[{_SEPARATORS}])|(?<=[{_SEPARATORS}] ))"
        # "-" separates list items, but "AI-assisted" is a compound word.
        after = rf"(?!-\w)(?=[ ]?(?:$|[{_SEPARATORS}.]))"
        pattern = rf"{before}{escaped}{after}"
        if alias.casefold() == "c":
            # "C/C++" is how JDs write C in prose ("proficient in C/C++"); only
            # the "C" is consumed, so C++ still matches its own alias.
            pattern += r"|(?<![\w.])c(?=\s*/\s*c\+\+)"
        return re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    # "js" must not match the tail of "node.js".
    before = r"(?<![\w.])" if alias == "js" else r"(?<!\w)"
    return re.compile(rf"{before}{escaped}(?!\w)", re.IGNORECASE)
