"""Evidence-first hybrid JD parser with deterministic fallback."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import ValidationError

from src.core.config import get_settings
from src.modules.ai import GenerationRequest, ModelServiceClient, ModelServiceError, generate_text
from src.modules.job_descriptions.domain.schemas import (
    CanonicalJobDescription,
    GroundedJobText,
    JobRequirement,
)
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.job_descriptions.parsing.llm_candidate import (
    JD_EXTRACTION_INSTRUCTIONS,
    JobDescriptionCandidate,
    TextCandidate,
)
from src.modules.user_cvs.facade import EvidenceMapper, SourceDocument
from src.modules.user_cvs.schemas import ParserWarning, TaxonomyRef

PARSER_VERSION = "hybrid-jd-v2"


def _evidence_id(kind: str, start: int, end: int) -> str:
    digest = hashlib.sha1(f"llm:{start}:{end}".encode(), usedforsecurity=False).hexdigest()[:12]
    return f"ev-jd-{kind}-{digest}"


class HybridJobDescriptionParser:
    """Augment deterministic extraction with OpenAI, retaining injected clients for tests."""

    def __init__(
        self,
        taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        taxonomy_version: str = "internal-2026.1",
        *,
        client: ModelServiceClient | None = None,
    ) -> None:
        self._deterministic = DeterministicJobDescriptionParser(taxonomy, taxonomy_version)
        self._taxonomy = self._deterministic._taxonomy
        self._taxonomy_version = taxonomy_version
        # An explicit client remains supported for tests and temporary adapters.
        # Normal application/evaluation execution uses the configured OpenAI API.
        self._client = client

    async def parse(
        self,
        source: SourceDocument,
        *,
        extraction_version: str,
        artifact_key: str | None = None,
    ) -> CanonicalJobDescription:
        baseline = self._deterministic.parse(
            source, extraction_version=extraction_version, artifact_key=artifact_key
        )
        if self._client is None and not get_settings().openai_api_key:
            return self._with_warning(
                baseline, "llm_not_configured", "Hybrid mode requested but OPENAI_API_KEY is not configured."
            )
        if self._client is not None and not self._client.enabled:
            return self._with_warning(
                baseline, "llm_not_configured", "Hybrid mode requested but its AI client is not configured."
            )
        try:
            if self._client is not None:
                output = await self._client.generate(
                    GenerationRequest(
                        input_text=source.text,
                        instructions=JD_EXTRACTION_INSTRUCTIONS,
                        temperature=0.0,
                    )
                )
            else:
                output = await generate_text(
                    instructions=JD_EXTRACTION_INSTRUCTIONS,
                    input_text=source.text,
                    max_output_tokens=get_settings().jd_parser_max_output_tokens,
                    temperature=0.0,
                )
            candidate = JobDescriptionCandidate.model_validate(json.loads(output))
        except (ModelServiceError, RuntimeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            return self._with_warning(baseline, "llm_fallback", f"LLM candidate rejected: {str(exc)[:300]}")
        try:
            return self._merge(baseline, source, candidate)
        except (ValidationError, ValueError) as exc:
            return self._with_warning(
                baseline,
                "llm_fallback",
                f"LLM candidate merge rejected: {str(exc)[:300]}",
            )

    @staticmethod
    def _with_warning(parsed: CanonicalJobDescription, code: str, message: str) -> CanonicalJobDescription:
        warning = ParserWarning(code=code, severity="warning", message=message)
        metadata = parsed.parsing.model_copy(
            update={"parser_version": PARSER_VERSION, "warnings": [*parsed.parsing.warnings, warning]}
        )
        return parsed.model_copy(update={"parsing": metadata})

    @staticmethod
    def _quote_offset(raw_text: str, quote: str) -> int | None:
        starts = [match.start() for match in re.finditer(re.escape(quote), raw_text)]
        return starts[0] if len(starts) == 1 else None

    def _merge(
        self, baseline: CanonicalJobDescription, source: SourceDocument, candidate: JobDescriptionCandidate
    ) -> CanonicalJobDescription:
        mapper = EvidenceMapper(source)
        evidence = {item.evidence_id: item for item in baseline.evidence}
        warnings: list[ParserWarning] = []

        def ground(kind: str, item: TextCandidate) -> str | None:
            start = self._quote_offset(source.text, item.quote)
            if start is None:
                warnings.append(
                    ParserWarning(
                        code="llm_claim_rejected",
                        severity="warning",
                        message=f"{kind} quote is missing or ambiguous",
                    )
                )
                return None
            end, evidence_id = start + len(item.quote), _evidence_id(kind, start, start + len(item.quote))
            evidence.setdefault(
                evidence_id,
                mapper.from_offsets(
                    evidence_id=evidence_id, char_start=start, char_end=end, section="job_description"
                ),
            )
            return evidence_id

        def unique_text(
            existing: list[GroundedJobText], kind: str, items: list[TextCandidate]
        ) -> list[GroundedJobText]:
            result, seen = list(existing), {item.text.casefold().strip() for item in existing}
            for item in items:
                evidence_id = ground(kind, item)
                normalized = item.value.casefold().strip()
                if evidence_id and normalized not in seen:
                    result.append(GroundedJobText(text=item.value.strip(), evidenceRefs=[evidence_id]))
                    seen.add(normalized)
            return result

        responsibilities = unique_text(
            baseline.responsibilities, "responsibility", candidate.responsibilities
        )
        benefits = unique_text(baseline.benefits, "benefit", candidate.benefits)
        requirements = list(baseline.requirements)
        requirement_keys = {(item.raw_label.casefold().strip(), item.priority) for item in requirements}
        for index, item in enumerate(candidate.requirements, start=len(requirements) + 1):
            evidence_id = ground("requirement", item)
            key = (item.value.casefold().strip(), item.priority)
            if not evidence_id or key in requirement_keys:
                continue
            concept = self._resolve_concept(item.quote)
            requirements.append(
                JobRequirement(
                    requirementId=f"req-llm-{index:03d}",
                    kind="skill" if concept else item.kind,
                    priority=item.priority,
                    concept=concept,
                    rawLabel=item.value.strip(),
                    minimumExperienceMonths=self._deterministic._experience_months(item.quote),
                    evidenceRefs=[evidence_id],
                )
            )
            requirement_keys.add(key)

        title = baseline.job_title
        if candidate.job_title:
            title_evidence = ground("title", candidate.job_title)
            if title_evidence and (
                not title or title.casefold() in {"description", "job description", "summary"}
            ):
                title = candidate.job_title.value.strip()
            elif title_evidence and candidate.job_title.value.casefold() in title.casefold():
                title = candidate.job_title.value.strip()
            elif title_evidence and title.casefold() != candidate.job_title.value.casefold():
                warnings.append(
                    ParserWarning(
                        code="llm_title_conflict",
                        severity="warning",
                        message="LLM title differs from deterministic title; deterministic title retained",
                    )
                )

        classifications = self._deterministic._classifications(requirements)
        metadata = baseline.parsing.model_copy(
            update={
                "parser_version": PARSER_VERSION,
                "warnings": [*baseline.parsing.warnings, *warnings],
            }
        )
        return CanonicalJobDescription(
            schemaVersion="1.0",
            jobTitle=title,
            careerClassifications=classifications,
            seniority=baseline.seniority,
            employmentType=baseline.employment_type,
            workMode=baseline.work_mode,
            location=baseline.location,
            responsibilities=responsibilities,
            requirements=requirements,
            benefits=benefits,
            evidence=list(evidence.values()),
            parsing=metadata,
        )

    def _resolve_concept(self, text: str) -> TaxonomyRef | None:
        for concept_id, (label, aliases) in self._taxonomy.items():
            if any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.I) for alias in aliases):
                return TaxonomyRef(
                    conceptId=concept_id,
                    scheme="internal",
                    taxonomyVersion=self._taxonomy_version,
                    label=label,
                )
        return None
