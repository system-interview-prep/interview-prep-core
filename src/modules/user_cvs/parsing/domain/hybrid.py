"""Evidence-first resume parser: deterministic baseline plus grounded LLM candidates."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import ValidationError

from src.core.config import get_settings
from src.modules.ai import GenerationRequest, ModelServiceClient, ModelServiceError, generate_text
from src.modules.user_cvs.domain.schemas import (
    CanonicalResume,
    CertificationEntry,
    EducationEntry,
    EmploymentEntry,
    ParsedResume,
    ParserWarning,
    PartialDate,
    ProjectEntry,
    ResumeProfile,
)
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.llm_candidate import CV_EXTRACTION_INSTRUCTIONS, ResumeCandidate
from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, SourceDocument

PARSER_VERSION = "hybrid-resume-v1"


class HybridResumeParser:
    def __init__(
        self,
        taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        taxonomy_version: str = "internal-2026.1",
        *,
        client: ModelServiceClient | None = None,
    ) -> None:
        self._deterministic = DeterministicResumeParser(taxonomy=taxonomy, taxonomy_version=taxonomy_version)
        self._client = client

    async def parse(
        self,
        source: SourceDocument,
        *,
        extraction_version: str,
        source_artifact_key: str | None = None,
    ) -> ParsedResume:
        baseline = self._deterministic.parse(
            source,
            extraction_version=extraction_version,
            source_artifact_key=source_artifact_key,
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
            safe_text = self._redact_identity(source.text, baseline)
            if self._client is not None:
                output = await self._client.generate(
                    GenerationRequest(
                        input_text=safe_text,
                        instructions=CV_EXTRACTION_INSTRUCTIONS,
                        temperature=0.0,
                    )
                )
            else:
                output = await generate_text(
                    instructions=CV_EXTRACTION_INSTRUCTIONS,
                    input_text=safe_text,
                    max_output_tokens=get_settings().cv_parser_max_output_tokens,
                    temperature=0.0,
                )
            candidate = ResumeCandidate.model_validate(json.loads(output))
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
    def _redact_identity(text: str, parsed: ParsedResume) -> str:
        spans = [
            item
            for item in [
                parsed.identity.full_name,
                parsed.identity.date_of_birth,
                *parsed.identity.emails,
                *parsed.identity.phones,
            ]
            if item is not None
        ]
        characters = list(text)
        for span in spans:
            characters[span.char_start : span.char_end] = "█" * (span.char_end - span.char_start)
        return "".join(characters)

    @staticmethod
    def _with_warning(parsed: ParsedResume, code: str, message: str) -> ParsedResume:
        metadata = parsed.resume.parsing
        if metadata is None:
            return parsed
        warning = ParserWarning(code=code, severity="warning", message=message)
        resume = parsed.resume.model_copy(
            update={
                "parsing": metadata.model_copy(
                    update={"parser_version": PARSER_VERSION, "warnings": [*metadata.warnings, warning]}
                )
            }
        )
        return ParsedResume(resume=resume, identity=parsed.identity)

    @staticmethod
    def _key(*values: str | None) -> tuple[str, ...]:
        return tuple((value or "").casefold().strip() for value in values)

    def _merge(
        self, baseline: ParsedResume, source: SourceDocument, candidate: ResumeCandidate
    ) -> ParsedResume:
        mapper = EvidenceMapper(source)
        evidence = {item.evidence_id: item for item in baseline.resume.evidence}
        warnings: list[ParserWarning] = []

        def ground(kind: str, quote: str) -> str | None:
            matches = list(re.finditer(re.escape(quote), source.text))
            if len(matches) != 1:
                warnings.append(
                    ParserWarning(
                        code="llm_claim_rejected",
                        path=kind,
                        message=f"{kind} quote is missing or ambiguous",
                    )
                )
                return None
            match = matches[0]
            digest = hashlib.sha1(
                f"llm:{kind}:{match.start()}:{match.end()}".encode(), usedforsecurity=False
            ).hexdigest()[:12]
            evidence_id = f"ev-{kind}-{digest}"
            evidence.setdefault(
                evidence_id,
                mapper.from_offsets(
                    evidence_id=evidence_id,
                    char_start=match.start(),
                    char_end=match.end(),
                ),
            )
            return evidence_id

        profile = baseline.resume.profile
        headline = profile.headline
        summary = profile.summary
        if candidate.headline and ground("headline", candidate.headline.quote):
            headline = headline or candidate.headline.value.strip()
        if candidate.summary and ground("summary", candidate.summary.quote):
            summary = summary or candidate.summary.value.strip()

        employment = list(baseline.resume.employment)
        seen_employment = {self._key(item.job_title, item.organization) for item in employment}
        for index, item in enumerate(candidate.employment, start=1):
            ref = ground("employment", item.quote)
            key = self._key(item.job_title, item.organization)
            if not ref or key in seen_employment:
                continue
            employment.append(
                EmploymentEntry(
                    employmentId=f"employment-llm-{index:03d}",
                    jobTitle=item.job_title.strip(),
                    organization=item.organization.strip() if item.organization else None,
                    startDate=PartialDate.model_validate(item.start_date.model_dump())
                    if item.start_date
                    else None,
                    endDate=PartialDate.model_validate(item.end_date.model_dump()) if item.end_date else None,
                    isCurrent=item.is_current,
                    responsibilities=[value.strip() for value in item.responsibilities if value.strip()],
                    evidenceRefs=[ref],
                )
            )
            seen_employment.add(key)

        education = list(baseline.resume.education)
        seen_education = {self._key(item.institution, item.degree, item.field_of_study) for item in education}
        for index, item in enumerate(candidate.education, start=1):
            ref = ground("education", item.quote)
            key = self._key(item.institution, item.degree, item.field_of_study)
            if not ref or key in seen_education:
                continue
            education.append(
                EducationEntry(
                    educationId=f"education-llm-{index:03d}",
                    institution=item.institution.strip(),
                    degree=item.degree.strip() if item.degree else None,
                    fieldOfStudy=item.field_of_study.strip() if item.field_of_study else None,
                    startDate=PartialDate.model_validate(item.start_date.model_dump())
                    if item.start_date
                    else None,
                    endDate=PartialDate.model_validate(item.end_date.model_dump()) if item.end_date else None,
                    evidenceRefs=[ref],
                )
            )
            seen_education.add(key)

        projects = list(baseline.resume.projects)
        seen_projects = {self._key(item.name) for item in projects}
        for index, item in enumerate(candidate.projects, start=1):
            ref = ground("project", item.quote)
            key = self._key(item.name)
            if not ref or key in seen_projects:
                continue
            projects.append(
                ProjectEntry(
                    projectId=f"project-llm-{index:03d}",
                    name=item.name.strip(),
                    description=item.description.strip() if item.description else None,
                    evidenceRefs=[ref],
                )
            )
            seen_projects.add(key)

        certifications = list(baseline.resume.certifications)
        seen_certifications = {self._key(item.name, item.issuer) for item in certifications}
        for index, item in enumerate(candidate.certifications, start=1):
            ref = ground("certification", item.quote)
            key = self._key(item.name, item.issuer)
            if not ref or key in seen_certifications:
                continue
            certifications.append(
                CertificationEntry(
                    certificationId=f"certification-llm-{index:03d}",
                    name=item.name.strip(),
                    issuer=item.issuer.strip() if item.issuer else None,
                    credentialId=item.credential_id.strip() if item.credential_id else None,
                    evidenceRefs=[ref],
                )
            )
            seen_certifications.add(key)

        metadata = baseline.resume.parsing
        assert metadata is not None
        resume = CanonicalResume.model_validate(
            baseline.resume.model_copy(
                update={
                    "profile": ResumeProfile(headline=headline, summary=summary),
                    "employment": employment,
                    "education": education,
                    "projects": projects,
                    "certifications": certifications,
                    "evidence": list(evidence.values()),
                    "parsing": metadata.model_copy(
                        update={
                            "parser_version": PARSER_VERSION,
                            "warnings": [*metadata.warnings, *warnings],
                        }
                    ),
                }
            ).model_dump()
        )
        return ParsedResume(resume=resume, identity=baseline.identity)
