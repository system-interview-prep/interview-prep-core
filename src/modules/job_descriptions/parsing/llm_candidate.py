"""Schema and prompt for untrusted LLM JD extraction candidates."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _CandidateModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TextCandidate(_CandidateModel):
    value: str = Field(min_length=1, max_length=2_000)
    quote: str = Field(min_length=1, max_length=4_000)


class RequirementCandidate(TextCandidate):
    kind: Literal["skill", "experience", "education", "language", "other"] = "other"
    priority: Literal["must_have", "preferred"] = "must_have"


class JobDescriptionCandidate(_CandidateModel):
    job_title: TextCandidate | None = Field(default=None, alias="jobTitle")
    responsibilities: list[TextCandidate] = Field(default_factory=list, max_length=30)
    requirements: list[RequirementCandidate] = Field(default_factory=list, max_length=40)
    benefits: list[TextCandidate] = Field(default_factory=list, max_length=30)


JD_EXTRACTION_INSTRUCTIONS = """You extract facts from a job description. Return JSON only, no markdown.
Use exactly this schema:
{
  "jobTitle": {"value": "normalized short title", "quote": "exact source substring"} | null,
  "responsibilities": [{"value": "short fact", "quote": "exact source substring"}],
  "requirements": [{"value": "short fact", "quote": "exact source substring", "kind": "skill|experience|education|language|other", "priority": "must_have|preferred"}],
  "benefits": [{"value": "short fact", "quote": "exact source substring"}]
}
Every quote must be copied verbatim from the supplied document. Do not infer,
invent, translate, or summarize facts that lack a quote. If uncertain, omit it.

For jobTitle:
- Extract the exact professional job title for the position (e.g. 'Senior Software Engineer', 'Product Manager', 'Data Analyst').
- Do not include marketing slogans, sentences, company names, or location prefixes in jobTitle value.

For responsibilities, requirements, and benefits:
- Extract all distinct, explicit items listed in the document.
- Treat each explicit bullet as one fact. For non-bulleted paragraphs, extract only the distinct core facts.
- Ignore company/product marketing, introduction, and boilerplate background.
- Return responsibilities only for activities the role performs, requirements only from qualification or preferred-qualification sections, and benefits only for employee compensation/perks.
- Do not repeat the same fact in different wording.
"""
