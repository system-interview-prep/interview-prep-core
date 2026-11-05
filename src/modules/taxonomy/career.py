"""Shared, versioned career taxonomy and evidence-grounded classification rules."""

from dataclasses import dataclass

from src.modules.taxonomy.skill_catalog import (
    AI,
    BACKEND,
    DATA,
    DEVOPS,
    EMBEDDED,
    FRONTEND,
    GAME,
    MOBILE,
    QA,
    SECURITY,
    role_skill_ids,
)

TAXONOMY_VERSION = "internal-career-2026.1"


@dataclass(frozen=True)
class CareerTaxonomyNode:
    code: str
    label: str
    dimension: str
    parent_code: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "label": self.label,
            "dimension": self.dimension,
            "parentCode": self.parent_code,
        }


@dataclass(frozen=True)
class CareerRule:
    code: str
    label: str
    keywords: tuple[str, ...]
    skill_ids: frozenset[str]
    parent_code: str


@dataclass(frozen=True)
class CareerClassificationResult:
    code: str
    label: str
    dimension: str
    taxonomy_version: str
    confidence: float
    evidence_refs: tuple[str, ...]
    is_primary: bool = False


_NODES = (
    CareerTaxonomyNode("technology", "Technology", "domain"),
    CareerTaxonomyNode("technology.software-engineering", "Software Engineering", "occupation", "technology"),
    CareerTaxonomyNode(
        "technology.software-engineering.backend",
        "Backend Engineering",
        "specialization",
        "technology.software-engineering",
    ),
    CareerTaxonomyNode(
        "technology.software-engineering.frontend",
        "Frontend Engineering",
        "specialization",
        "technology.software-engineering",
    ),
    CareerTaxonomyNode(
        "technology.software-engineering.mobile",
        "Mobile Engineering",
        "specialization",
        "technology.software-engineering",
    ),
    CareerTaxonomyNode("technology.cloud-devops", "Cloud & DevOps", "specialization", "technology"),
    CareerTaxonomyNode(
        "technology.artificial-intelligence", "Artificial Intelligence", "specialization", "technology"
    ),
    CareerTaxonomyNode("technology.ai-engineering", "AI Engineering", "specialization", "technology"),
    CareerTaxonomyNode("technology.data-engineering", "Data Engineering", "specialization", "technology"),
    CareerTaxonomyNode("technology.data-science", "Data Science", "specialization", "technology"),
    CareerTaxonomyNode("technology.cybersecurity", "Cybersecurity", "specialization", "technology"),
    CareerTaxonomyNode("technology.qa-automation", "QA & Test Automation", "specialization", "technology"),
    CareerTaxonomyNode("technology.ux-ui-design", "UX/UI Design", "specialization", "technology"),
    CareerTaxonomyNode("technology.business-analysis", "Business Analysis", "specialization", "technology"),
    CareerTaxonomyNode("technology.game-development", "Game Development", "specialization", "technology"),
    CareerTaxonomyNode(EMBEDDED, "Embedded Engineering", "specialization", "technology.software-engineering"),
    CareerTaxonomyNode(QA, "Software Testing & QA", "specialization", "technology"),
    CareerTaxonomyNode(DATA, "Data Engineering & Analytics", "specialization", "technology"),
    CareerTaxonomyNode(SECURITY, "Cybersecurity", "specialization", "technology"),
)

_RULES = (
    CareerRule(
        "technology.ai-engineering",
        "AI Engineering",
        (
            "ai engineer",
            "ai engineering",
            "llm engineer",
            "generative ai engineer",
            "machine learning engineer",
        ),
        frozenset(
            {
                "skill-machine-learning",
                "skill-natural-language-processing",
                "skill-generative-ai",
                "skill-large-language-models",
                "skill-retrieval-augmented-generation",
                "skill-langgraph",
            }
        ),
        "technology",
    ),
    CareerRule(
        "technology.data-engineering",
        "Data Engineering",
        ("data engineer", "data engineering", "analytics engineer"),
        frozenset(),
        "technology",
    ),
    CareerRule(
        "technology.data-science",
        "Data Science",
        ("data scientist", "data science", "research scientist"),
        frozenset(),
        "technology",
    ),
    CareerRule(
        "technology.cybersecurity",
        "Cybersecurity",
        ("cybersecurity", "cyber security", "security engineer", "penetration tester", "penetration testing"),
        frozenset(),
        "technology",
    ),
    CareerRule(
        "technology.qa-automation",
        "QA & Test Automation",
        ("qa engineer", "quality assurance", "test automation", "software tester", "sdet"),
        frozenset(),
        "technology",
    ),
    CareerRule(
        "technology.ux-ui-design",
        "UX/UI Design",
        ("ux designer", "ui designer", "ux/ui designer", "product designer"),
        frozenset(),
        "technology",
    ),
    CareerRule(
        "technology.business-analysis",
        "Business Analysis",
        ("business analyst", "business analysis", "systems analyst"),
        frozenset(),
        "technology",
    ),
    CareerRule(
        "technology.game-development",
        "Game Development",
        ("game developer", "unity developer"),
        role_skill_ids(GAME),
        "technology",
    ),
    CareerRule(
        "technology.artificial-intelligence",
        "Artificial Intelligence",
        ("artificial intelligence", "machine learning"),
        role_skill_ids(AI),
        "technology",
    ),
    CareerRule(
        "technology.software-engineering.backend",
        "Backend Engineering",
        ("backend", "back-end", "server-side", "full stack", "fullstack", "full-stack"),
        role_skill_ids(BACKEND),
        "technology.software-engineering",
    ),
    CareerRule(
        "technology.software-engineering.frontend",
        "Frontend Engineering",
        ("frontend", "front-end", "ui developer"),
        role_skill_ids(FRONTEND),
        "technology.software-engineering",
    ),
    CareerRule(
        "technology.software-engineering.mobile",
        "Mobile Engineering",
        ("mobile developer", "mobile engineer", "android", "ios"),
        role_skill_ids(MOBILE),
        "technology.software-engineering",
    ),
    CareerRule(
        "technology.cloud-devops",
        "Cloud & DevOps",
        ("devops", "cloud engineer", "site reliability", "sre"),
        role_skill_ids(DEVOPS),
        "technology",
    ),
    CareerRule(
        EMBEDDED,
        "Embedded Engineering",
        ("embedded", "firmware", "iot", "nhúng"),
        role_skill_ids(EMBEDDED),
        "technology.software-engineering",
    ),
    CareerRule(
        QA,
        "Software Testing & QA",
        ("tester", "qa", "qc", "quality assurance", "test engineer", "automation test", "kiểm thử"),
        role_skill_ids(QA),
        "technology",
    ),
    CareerRule(
        DATA,
        "Data Engineering & Analytics",
        (
            "data engineer",
            "data analyst",
            "data analytics",
            "business intelligence",
            "bi developer",
            "phân tích dữ liệu",
        ),
        role_skill_ids(DATA),
        "technology",
    ),
    CareerRule(
        SECURITY,
        "Cybersecurity",
        ("security", "pentest", "penetration", "soc analyst", "bảo mật", "an toàn thông tin"),
        role_skill_ids(SECURITY),
        "technology",
    ),
)


def career_taxonomy() -> tuple[CareerTaxonomyNode, ...]:
    return _NODES


def _specializations(skill_refs, title_refs, *, minimum_skill_signals: int):
    results: list[CareerClassificationResult] = []
    for rule in _RULES:
        supporting_ids = sorted(rule.skill_ids.intersection(skill_refs))
        matching_titles = [
            (title, refs)
            for title, refs in title_refs
            if any(word in title.casefold() for word in rule.keywords)
        ]
        # A rule with neither a title match nor a supporting skill has no
        # evidence at all; with minimum_skill_signals=0 it used to pass with the
        # base confidence, so the first declared rule (game development) won
        # every title the keywords did not recognise.
        if not matching_titles and (not supporting_ids or len(supporting_ids) < minimum_skill_signals):
            continue
        refs = list(
            dict.fromkeys(
                [ref for concept_id in supporting_ids for ref in skill_refs[concept_id]]
                + [ref for _, title_evidence in matching_titles for ref in title_evidence]
            )
        )
        # A title is explicit intent, while supported skills establish the domain.
        # This makes an AI Engineer with a backend implementation stack classify
        # by its AI specialization instead of relying on declaration order ties.
        confidence = min(0.95, 0.45 + 0.15 * len(supporting_ids) + 0.30 * bool(matching_titles))
        results.append(
            CareerClassificationResult(
                code=rule.code,
                label=rule.label,
                dimension="specialization",
                taxonomy_version=TAXONOMY_VERSION,
                confidence=confidence,
                evidence_refs=tuple(refs),
            )
        )
    # Python's sort is stable, so equal-confidence results retain the explicit
    # domain-priority order declared in _RULES.
    return sorted(results, key=lambda item: -item.confidence)


def classify_career(
    skill_refs: dict[str, list[str]],
    title_refs: list[tuple[str, list[str]]],
    *,
    minimum_skill_signals: int,
    include_ancestors: bool,
) -> list[CareerClassificationResult]:
    specializations = _specializations(
        skill_refs,
        title_refs,
        minimum_skill_signals=minimum_skill_signals,
    )
    if not specializations:
        return []
    specializations = [
        CareerClassificationResult(
            **{
                **item.__dict__,
                "is_primary": index == 0,
            }
        )
        for index, item in enumerate(specializations)
    ]
    if not include_ancestors:
        return specializations
    primary = specializations[0]
    nodes = {node.code: node for node in _NODES}
    ancestor_codes = ["technology"]
    if nodes[primary.code].parent_code == "technology.software-engineering":
        ancestor_codes.append("technology.software-engineering")
    ancestors = [
        CareerClassificationResult(
            code=code,
            label=nodes[code].label,
            dimension=nodes[code].dimension,
            taxonomy_version=TAXONOMY_VERSION,
            confidence=primary.confidence,
            evidence_refs=primary.evidence_refs,
        )
        for code in ancestor_codes
    ]
    return [*ancestors, *specializations]
