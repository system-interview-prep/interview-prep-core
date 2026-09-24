"""Strict schema and prompt for untrusted LLM resume extraction candidates."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TextCandidate(_CandidateModel):
    value: str = Field(min_length=1, max_length=2_000)
    quote: str = Field(min_length=1, max_length=6_000)


class PartialDateCandidate(_CandidateModel):
    value: str = Field(pattern=r"^\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$")
    precision: Literal["year", "month", "day"]


class EmploymentCandidate(_CandidateModel):
    job_title: str = Field(min_length=1, max_length=300, alias="jobTitle")
    organization: str | None = Field(default=None, max_length=300)
    start_date: PartialDateCandidate | None = Field(default=None, alias="startDate")
    end_date: PartialDateCandidate | None = Field(default=None, alias="endDate")
    is_current: bool = Field(default=False, alias="isCurrent")
    responsibilities: list[str] = Field(default_factory=list, max_length=20)
    quote: str = Field(min_length=1, max_length=8_000)


class EducationCandidate(_CandidateModel):
    institution: str = Field(min_length=1, max_length=500)
    degree: str | None = Field(default=None, max_length=300)
    field_of_study: str | None = Field(default=None, max_length=300, alias="fieldOfStudy")
    start_date: PartialDateCandidate | None = Field(default=None, alias="startDate")
    end_date: PartialDateCandidate | None = Field(default=None, alias="endDate")
    student_status: Literal["student", "final_year", "recent_graduate"] | None = Field(
        default=None, alias="studentStatus"
    )
    gpa: float | None = Field(default=None, gt=0)
    gpa_scale: float | None = Field(default=None, gt=0, alias="gpaScale")
    quote: str = Field(min_length=1, max_length=8_000)


class ProjectCandidate(_CandidateModel):
    name: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4_000)
    quote: str = Field(min_length=1, max_length=8_000)


class CertificationCandidate(_CandidateModel):
    name: str = Field(min_length=1, max_length=500)
    issuer: str | None = Field(default=None, max_length=300)
    credential_id: str | None = Field(default=None, max_length=300, alias="credentialId")
    quote: str = Field(min_length=1, max_length=4_000)


class ResumeCandidate(_CandidateModel):
    headline: TextCandidate | None = None
    summary: TextCandidate | None = None
    employment: list[EmploymentCandidate] = Field(default_factory=list, max_length=20)
    education: list[EducationCandidate] = Field(default_factory=list, max_length=20)
    projects: list[ProjectCandidate] = Field(default_factory=list, max_length=30)
    certifications: list[CertificationCandidate] = Field(default_factory=list, max_length=30)


CV_EXTRACTION_INSTRUCTIONS = """Extract explicit facts from a resume and return JSON only.
Use exactly this top-level schema: headline, summary, employment, education, projects, certifications.
headline and summary are {"value": string, "quote": exact source substring} or null.
Each structured entry must contain a quote copied verbatim from one unique contiguous source span.
Employment fields: jobTitle, organization, startDate, endDate, isCurrent, responsibilities, quote.
Education fields: institution, degree, fieldOfStudy, startDate, endDate, studentStatus, gpa, gpaScale, quote.
Project fields: name, description, quote. Certification fields: name, issuer, credentialId, quote.
Dates use {"value":"YYYY" or "YYYY-MM" or "YYYY-MM-DD", "precision":"year|month|day"}.
Never extract names, email addresses, phone numbers, birth dates, protected traits, or inferred skills.
Do not infer, translate, or add facts not explicitly supported by the quote. Omit uncertain facts.
Prefer precision over coverage and do not repeat an entity already stated in another form.
"""
