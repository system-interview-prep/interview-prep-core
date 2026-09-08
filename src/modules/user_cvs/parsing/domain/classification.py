"""Evidence-grounded, deterministic career classification for CVs."""

from dataclasses import dataclass

from src.modules.user_cvs.domain.schemas import CareerClassification, EmploymentEntry, SkillClaim

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


_TAXONOMY = (
    CareerTaxonomyNode("technology", "Technology", "domain"),
    CareerTaxonomyNode(
        "technology.software-engineering",
        "Software Engineering",
        "occupation",
        "technology",
    ),
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
)


def career_taxonomy() -> tuple[CareerTaxonomyNode, ...]:
    """Public catalogue; FE must use this rather than hard-coded labels."""
    return _TAXONOMY


@dataclass(frozen=True)
class CareerRule:
    code: str
    label: str
    keywords: tuple[str, ...]
    skill_ids: frozenset[str]


_SPECIALIZATIONS = (
    CareerRule(
        code="technology.software-engineering.backend",
        label="Backend Engineering",
        keywords=("backend", "back-end", "server-side"),
        skill_ids=frozenset({"skill-java", "skill-spring-boot", "skill-fastapi", "skill-postgresql"}),
    ),
    CareerRule(
        code="technology.software-engineering.frontend",
        label="Frontend Engineering",
        keywords=("frontend", "front-end", "ui developer"),
        skill_ids=frozenset({"skill-javascript", "skill-typescript", "skill-react"}),
    ),
    CareerRule(
        code="technology.cloud-devops",
        label="Cloud & DevOps",
        keywords=("devops", "cloud engineer", "site reliability", "sre"),
        skill_ids=frozenset({"skill-docker", "skill-kubernetes", "skill-aws"}),
    ),
)


class DeterministicCareerClassifier:
    """Classifies only when title or multiple explicit skill signals support it."""

    def classify(
        self, *, skills: list[SkillClaim], employment: list[EmploymentEntry]
    ) -> list[CareerClassification]:
        results: list[CareerClassification] = []
        specializations: list[CareerClassification] = []
        for rule in _SPECIALIZATIONS:
            supporting_skills = [
                skill for skill in skills if skill.concept.concept_id in rule.skill_ids
            ]
            title_entries = [
                entry
                for entry in employment
                if any(keyword in entry.job_title.casefold() for keyword in rule.keywords)
            ]
            if not title_entries and len(supporting_skills) < 2:
                continue
            refs = list(
                dict.fromkeys(
                    [ref for skill in supporting_skills for ref in skill.evidence_refs]
                    + [ref for entry in title_entries for ref in entry.evidence_refs]
                )
            )
            confidence = min(0.95, 0.45 + 0.15 * len(supporting_skills) + 0.25 * bool(title_entries))
            specializations.append(
                CareerClassification(
                    code=rule.code,
                    label=rule.label,
                    dimension="specialization",
                    taxonomyVersion=TAXONOMY_VERSION,
                    confidence=confidence,
                    evidenceRefs=refs,
                )
            )
        if not specializations:
            return results
        specializations.sort(key=lambda item: (-item.confidence, item.code))
        primary = specializations[0]
        results.extend(
            [
                CareerClassification(
                    code="technology",
                    label="Technology",
                    dimension="domain",
                    taxonomyVersion=TAXONOMY_VERSION,
                    confidence=primary.confidence,
                    evidenceRefs=primary.evidence_refs,
                ),
                CareerClassification(
                    code="technology.software-engineering",
                    label="Software Engineering",
                    dimension="occupation",
                    taxonomyVersion=TAXONOMY_VERSION,
                    confidence=primary.confidence,
                    evidenceRefs=primary.evidence_refs,
                ),
            ]
        )
        for index, classification in enumerate(specializations):
            results.append(classification.model_copy(update={"is_primary": index == 0}))
        return results
