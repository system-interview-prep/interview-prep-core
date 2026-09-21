"""Evidence-first hybrid JD parser with deterministic fallback."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

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


def _safe_json_parse(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        first_line = lines[0]
        if first_line.startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx : end_idx + 1]
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _sanitize_candidate_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    title = data.get("jobTitle") or data.get("job_title")
    if isinstance(title, dict):
        val = str(title.get("value") or "").strip()
        quote = str(title.get("quote") or "").strip()
        data["jobTitle"] = {"value": val[:200], "quote": quote[:500]} if val and quote else None
    else:
        data["jobTitle"] = None

    company = data.get("companyName") or data.get("company_name")
    if isinstance(company, dict):
        val = str(company.get("value") or "").strip()
        quote = str(company.get("quote") or "").strip()
        data["companyName"] = {"value": val[:200], "quote": quote[:500]} if val and quote else None
    else:
        data["companyName"] = None

    for list_field in ("responsibilities", "benefits"):
        clean_list = []
        for item in data.get(list_field, []):
            if not isinstance(item, dict):
                continue
            val = str(item.get("value") or "").strip()
            quote = str(item.get("quote") or "").strip()
            if val and quote:
                clean_list.append({"value": val[:2000], "quote": quote[:4000]})
        data[list_field] = clean_list[:30]

    clean_reqs = []
    for item in data.get("requirements", []):
        if not isinstance(item, dict):
            continue
        val = str(item.get("value") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not (val and quote):
            continue
        kind = str(item.get("kind") or "other").lower().strip()
        if kind not in {"skill", "experience", "education", "language", "other"}:
            kind = "other"
        priority = str(item.get("priority") or "must_have").lower().strip()
        if priority not in {"must_have", "preferred"}:
            priority = "must_have"
        clean_reqs.append(
            {
                "value": val[:2000],
                "quote": quote[:4000],
                "kind": kind,
                "priority": priority,
            }
        )
    data["requirements"] = clean_reqs[:40]
    return data


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
            parsed_json = _safe_json_parse(output)
            sanitized = _sanitize_candidate_payload(parsed_json)
            candidate = JobDescriptionCandidate.model_validate(sanitized)
        except (ModelServiceError, RuntimeError, json.JSONDecodeError, ValidationError, ValueError, Exception) as exc:
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
    def _quote_offset(raw_text: str, quote: str) -> tuple[int, int] | None:
        if not quote or not quote.strip():
            return None
        # 1. Exact match
        matches = list(re.finditer(re.escape(quote), raw_text))
        if matches:
            return matches[0].start(), matches[0].end()
        # 2. Whitespace-tolerant match
        words = quote.split()
        if words:
            pattern = re.compile(r"\s+".join(re.escape(w) for w in words))
            matches = list(pattern.finditer(raw_text))
            if matches:
                return matches[0].start(), matches[0].end()
        return None

    def _merge(
        self, baseline: CanonicalJobDescription, source: SourceDocument, candidate: JobDescriptionCandidate
    ) -> CanonicalJobDescription:
        mapper = EvidenceMapper(source)
        evidence = {item.evidence_id: item for item in baseline.evidence}
        warnings: list[ParserWarning] = []

        def ground(kind: str, item: TextCandidate) -> str | None:
            span = self._quote_offset(source.text, item.quote)
            if span is None:
                warnings.append(
                    ParserWarning(
                        code="llm_claim_rejected",
                        severity="warning",
                        message=f"{kind} quote is missing or ambiguous",
                    )
                )
                return None
            start, end = span
            evidence_id = _evidence_id(kind, start, end)
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
            result = list(existing)
            for item in items:
                evidence_id = ground(kind, item)
                if not evidence_id:
                    continue
                val = item.value.strip()
                replaced = False
                for idx, ex in enumerate(result):
                    if (
                        val.casefold() == ex.text.casefold()
                        or (len(val) >= 5 and len(ex.text) >= 5 and (val.casefold() in ex.text.casefold() or ex.text.casefold() in val.casefold()))
                    ):
                        result[idx] = GroundedJobText(text=val, evidenceRefs=[evidence_id])
                        replaced = True
                        break
                if not replaced:
                    result.append(GroundedJobText(text=val, evidenceRefs=[evidence_id]))
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

        company_name = baseline.company_name
        if candidate.company_name:
            company_evidence = ground("company", candidate.company_name)
            if company_evidence and not company_name:
                company_name = candidate.company_name.value.strip()

        title = baseline.job_title
        if candidate.job_title:
            title_evidence = ground("title", candidate.job_title)
            if title_evidence:
                candidate_title = candidate.job_title.value.strip()
                weak_titles = {
                    "description", "job description", "summary", "overview", "tldr",
                    "your job", "who we are", "our vision", "purpose of job"
                }
                if not title or title.casefold() in weak_titles:
                    title = candidate_title
                elif candidate_title.casefold() in title.casefold() or title.casefold() in candidate_title.casefold():
                    title = candidate_title
                elif not self._has_explicit_title_header(source.text, title):
                    title = candidate_title
                else:
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
            companyName=company_name,
            careerClassifications=classifications,
            seniority=baseline.seniority,
            employmentType=baseline.employment_type,
            workMode=baseline.work_mode,
            location=baseline.location,
            experienceMinYears=baseline.experience_min_years,
            experienceMaxYears=baseline.experience_max_years,
            experienceRaw=baseline.experience_raw,
            salaryMin=baseline.salary_min,
            salaryMax=baseline.salary_max,
            salaryCurrency=baseline.salary_currency,
            salaryPeriod=baseline.salary_period,
            salaryNegotiable=baseline.salary_negotiable,
            salaryRaw=baseline.salary_raw,
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

    @staticmethod
    def _has_explicit_title_header(raw_text: str, title: str) -> bool:
        from src.modules.job_descriptions.parsing.deterministic import _key
        for line in raw_text.splitlines():
            label, separator, value = line.partition(":")
            if separator and _key(label) in {"job title", "position", "vi tri", "chuc danh"}:
                if value.strip().casefold() == title.casefold():
                    return True
        return False
