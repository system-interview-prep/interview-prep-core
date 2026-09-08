from calendar import monthrange
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CanonicalModel(BaseModel):
    """Strict base contract for persisted, versioned CV data."""

    model_config = ConfigDict(extra="forbid", alias_generator=_to_camel, populate_by_name=True)


class EvidenceSpan(CanonicalModel):
    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]+$")
    section: str = Field(min_length=1)
    text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    page: int | None = Field(default=None, ge=1)
    source_block_id: str | None = None
    reading_order: int | None = Field(default=None, ge=0)
    bounding_box: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def offsets_are_ordered(self) -> "EvidenceSpan":
        if self.char_end <= self.char_start:
            raise ValueError("charEnd must be greater than charStart")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("evidence text length must equal charEnd - charStart")
        if self.bounding_box is not None:
            x0, y0, x1, y1 = self.bounding_box
            if min(self.bounding_box) < 0 or x1 <= x0 or y1 <= y0:
                raise ValueError("boundingBox must be a non-negative [x0, y0, x1, y1] rectangle")
        return self


class PartialDate(CanonicalModel):
    value: str = Field(pattern=r"^\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$")
    precision: Literal["year", "month", "day"]

    @model_validator(mode="after")
    def precision_matches_value(self) -> "PartialDate":
        expected_length = {"year": 4, "month": 7, "day": 10}[self.precision]
        if len(self.value) != expected_length:
            raise ValueError("date precision does not match value")
        if self.precision == "day":
            try:
                datetime.strptime(self.value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("invalid calendar date") from exc
        return self


def _partial_date_bounds(value: PartialDate) -> tuple[date, date]:
    parts = [int(part) for part in value.value.split("-")]
    year = parts[0]
    if value.precision == "year":
        return date(year, 1, 1), date(year, 12, 31)
    month = parts[1]
    if value.precision == "month":
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    exact = date(year, month, parts[2])
    return exact, exact


def _starts_definitely_after(start: PartialDate, end: PartialDate) -> bool:
    start_lower, _ = _partial_date_bounds(start)
    _, end_upper = _partial_date_bounds(end)
    return start_lower > end_upper


class TaxonomyRef(CanonicalModel):
    concept_id: str = Field(min_length=1)
    scheme: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    label: str = Field(min_length=1)


class SkillClaim(CanonicalModel):
    claim_id: str = Field(min_length=1)
    concept: TaxonomyRef
    raw_label: str = Field(min_length=1)
    experience_months: int | None = Field(default=None, ge=0)
    evidence_refs: list[str] = Field(default_factory=list)
    assertion_source: Literal["explicit", "inferred"] = "explicit"
    confidence: float | None = Field(default=None, ge=0, le=1)


class AchievementMetric(CanonicalModel):
    name: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)


class Achievement(CanonicalModel):
    text: str = Field(min_length=1)
    metrics: list[AchievementMetric] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class EmploymentEntry(CanonicalModel):
    employment_id: str = Field(min_length=1)
    job_title: str = Field(min_length=1)
    organization: str | None = None
    start_date: PartialDate | None = None
    end_date: PartialDate | None = None
    is_current: bool = False
    responsibilities: list[str] = Field(default_factory=list)
    achievements: list[Achievement] = Field(default_factory=list)
    skill_claim_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def dates_are_consistent(self) -> "EmploymentEntry":
        if self.is_current and self.end_date is not None:
            raise ValueError("a current employment entry must not have endDate")
        if self.start_date and self.end_date and _starts_definitely_after(self.start_date, self.end_date):
            raise ValueError("startDate must not be after endDate")
        return self


class EducationEntry(CanonicalModel):
    education_id: str = Field(min_length=1)
    institution: str = Field(min_length=1)
    degree: str | None = None
    field_of_study: str | None = None
    start_date: PartialDate | None = None
    end_date: PartialDate | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "EducationEntry":
        if self.start_date and self.end_date and _starts_definitely_after(self.start_date, self.end_date):
            raise ValueError("startDate must not be after endDate")
        return self


class ProjectEntry(CanonicalModel):
    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    start_date: PartialDate | None = None
    end_date: PartialDate | None = None
    skill_claim_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "ProjectEntry":
        if self.start_date and self.end_date and _starts_definitely_after(self.start_date, self.end_date):
            raise ValueError("startDate must not be after endDate")
        return self


class CertificationEntry(CanonicalModel):
    certification_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    issuer: str | None = None
    issued_date: PartialDate | None = None
    expires_date: PartialDate | None = None
    credential_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "CertificationEntry":
        if self.issued_date and self.expires_date and _starts_definitely_after(
            self.issued_date, self.expires_date
        ):
            raise ValueError("issuedDate must not be after expiresDate")
        return self


class LanguageClaim(CanonicalModel):
    code: str = Field(pattern=r"^[a-z]{2,3}$")
    level: str | None = None
    framework: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class CareerClassification(CanonicalModel):
    code: str = Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
    label: str = Field(min_length=1)
    dimension: Literal["domain", "occupation", "specialization"]
    taxonomy_version: str = Field(min_length=1)
    is_primary: bool = False
    confidence: float = Field(ge=0, le=1)
    assertion_source: Literal["explicit", "inferred"] = "inferred"
    evidence_refs: list[str] = Field(default_factory=list)


class ResumeProfile(CanonicalModel):
    headline: str | None = None
    summary: str | None = None


class IdentityValue(CanonicalModel):
    value: str
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)


class ResumeIdentity(CanonicalModel):
    full_name: IdentityValue | None = None
    date_of_birth: IdentityValue | None = None
    emails: list[IdentityValue] = Field(default_factory=list)
    phones: list[IdentityValue] = Field(default_factory=list)


class ParserWarning(CanonicalModel):
    code: str = Field(min_length=1)
    path: str | None = None
    severity: Literal["info", "warning", "error"] = "warning"
    message: str = Field(min_length=1)


class ParsingMetadata(CanonicalModel):
    parser_version: str = Field(min_length=1)
    extraction_version: str = Field(min_length=1)
    parsed_at: datetime
    status: Literal["ready", "review_required"]
    source_artifact_key: str | None = None
    warnings: list[ParserWarning] = Field(default_factory=list)


class CanonicalResume(CanonicalModel):
    schema_version: Literal["2.1"]
    resume_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]+$")
    document_languages: list[str] = Field(default_factory=list)
    profile: ResumeProfile = Field(default_factory=ResumeProfile)
    skills: list[SkillClaim] = Field(default_factory=list)
    employment: list[EmploymentEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    certifications: list[CertificationEntry] = Field(default_factory=list)
    languages: list[LanguageClaim] = Field(default_factory=list)
    career_classifications: list[CareerClassification] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    parsing: ParsingMetadata | None = None

    @model_validator(mode="after")
    def references_are_consistent(self) -> "CanonicalResume":
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        evidence_set = set(evidence_ids)
        skill_ids = [item.claim_id for item in self.skills]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("skill claim IDs must be unique")
        skill_set = set(skill_ids)
        entity_id_groups = {
            "employment": [item.employment_id for item in self.employment],
            "education": [item.education_id for item in self.education],
            "project": [item.project_id for item in self.projects],
            "certification": [item.certification_id for item in self.certifications],
        }
        for kind, identifiers in entity_id_groups.items():
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{kind} IDs must be unique")
        evidence_owners = [
            *self.skills,
            *self.employment,
            *self.education,
            *self.projects,
            *self.certifications,
            *self.languages,
            *self.career_classifications,
        ]
        for owner in evidence_owners:
            if not set(owner.evidence_refs).issubset(evidence_set):
                raise ValueError("all evidenceRefs must resolve inside the resume")
        for employment in self.employment:
            for achievement in employment.achievements:
                if not set(achievement.evidence_refs).issubset(evidence_set):
                    raise ValueError("all achievement evidenceRefs must resolve inside the resume")
        for owner in [*self.employment, *self.projects]:
            if not set(owner.skill_claim_ids).issubset(skill_set):
                raise ValueError("all skillClaimIds must resolve inside the resume")
        for item in self.evidence:
            if item.document_id != self.document_id or item.document_sha256 != self.document_sha256:
                raise ValueError("evidence must belong to the resume document revision")
        return self


class ParsedResume(CanonicalModel):
    """PII and matching-safe canonical data are deliberately separate."""

    resume: CanonicalResume
    identity: ResumeIdentity = Field(default_factory=ResumeIdentity)
