"""Schema and prompt for untrusted LLM JD extraction candidates."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


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

Prefer precision over coverage: an extra item is harmful. Treat each explicit
bullet as at most one fact. For a non-bulleted paragraph, return at most one
combined fact of each type; do not split prose into many overlapping facts.
Ignore company/product marketing and background. Return responsibilities only
for activities the role performs, and requirements only from qualification or
preferred-qualification sections. Do not repeat the same fact in different
wording. Keep the output concise: normally no more than 5 responsibilities,
8 requirements, and 3 benefits; exceed a cap only when the source contains
more explicit, distinct bullets in that field.
"""
