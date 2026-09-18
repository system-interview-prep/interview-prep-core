"""Shared, versioned career taxonomy and evidence-grounded classification rules."""

from dataclasses import dataclass

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
    CareerTaxonomyNode("technology.cloud-devops", "Cloud & DevOps", "specialization", "technology"),
    CareerTaxonomyNode(
        "technology.artificial-intelligence", "Artificial Intelligence", "specialization", "technology"
    ),
    CareerTaxonomyNode("technology.game-development", "Game Development", "specialization", "technology"),
)

_RULES = (
    CareerRule(
        "technology.game-development",
        "Game Development",
        ("game developer", "unity developer"),
        frozenset({"skill-unity"}),
        "technology",
    ),
    CareerRule(
        "technology.artificial-intelligence",
        "Artificial Intelligence",
        ("ai engineer", "machine learning", "data scientist"),
        frozenset(
            {
                "skill-artificial-intelligence",
                "skill-machine-learning",
                "skill-natural-language-processing",
                "skill-generative-ai",
                "skill-large-language-models",
            }
        ),
        "technology",
    ),
    CareerRule(
        "technology.software-engineering.backend",
        "Backend Engineering",
        ("backend", "back-end", "server-side"),
        frozenset({"skill-java", "skill-spring-boot", "skill-fastapi", "skill-postgresql"}),
        "technology.software-engineering",
    ),
    CareerRule(
        "technology.software-engineering.frontend",
        "Frontend Engineering",
        ("frontend", "front-end", "ui developer"),
        frozenset({"skill-javascript", "skill-typescript", "skill-react"}),
        "technology.software-engineering",
    ),
    CareerRule(
        "technology.cloud-devops",
        "Cloud & DevOps",
        ("devops", "cloud engineer", "site reliability", "sre"),
        frozenset({"skill-docker", "skill-kubernetes", "skill-aws"}),
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
        if not matching_titles and len(supporting_ids) < minimum_skill_signals:
            continue
        refs = list(
            dict.fromkeys(
                [ref for concept_id in supporting_ids for ref in skill_refs[concept_id]]
                + [ref for _, title_evidence in matching_titles for ref in title_evidence]
            )
        )
        confidence = min(0.95, 0.45 + 0.15 * len(supporting_ids) + 0.25 * bool(matching_titles))
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
