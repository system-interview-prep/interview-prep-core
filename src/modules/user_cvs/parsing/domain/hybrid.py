"""Evidence-first resume parser: deterministic baseline plus grounded LLM candidates."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

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
from src.modules.user_cvs.parsing.domain.classification import DeterministicCareerClassifier
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.llm_candidate import CV_EXTRACTION_INSTRUCTIONS, ResumeCandidate
from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, SourceDocument
from src.modules.user_cvs.parsing.domain.structured import (
    _is_founder_employer_project,
    _looks_like_project_title,
    _project_name_key,
    _project_title_in_quote,
    _project_titles_overlap,
)

PARSER_VERSION = "hybrid-resume-v2"


def _sanitize_date_candidate(val: Any) -> tuple[dict[str, str] | None, bool]:
    """Sanitize date values into valid PartialDateCandidate shape or (None, is_current=True)."""
    if val is None:
        return None, False
    if isinstance(val, dict):
        raw_val = str(val.get("value", "")).strip()
        precision = val.get("precision")
    else:
        raw_val = str(val).strip()
        precision = None

    if raw_val.casefold() in {"present", "current", "now", "hiện tại", "nay", "today"}:
        return None, True

    m = re.search(r"\b(\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?)\b", raw_val)
    if not m:
        m_year = re.search(r"\b(\d{4})\b", raw_val)
        if m_year:
            return {"value": m_year.group(1), "precision": "year"}, False
        return None, False

    clean_val = m.group(1)
    if precision not in {"year", "month", "day"}:
        parts = clean_val.split("-")
        precision = "day" if len(parts) == 3 else ("month" if len(parts) == 2 else "year")
    return {"value": clean_val, "precision": precision}, False


def _safe_json_parse(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    cleaned = text
    if cleaned.count('"') % 2 != 0:
        cleaned += '"'
    open_braces = cleaned.count("{") - cleaned.count("}")
    open_brackets = cleaned.count("[") - cleaned.count("]")
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    cleaned = re.sub(r",\s*$", "", cleaned)
    cleaned += ("]" * max(0, open_brackets)) + ("}" * max(0, open_braces))
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        raise


_STUDENT_STATUSES = {"student", "final_year", "recent_graduate"}


def _text(value: Any) -> str:
    """Coerce an untrusted field to a stripped string, treating null-ish values as empty."""
    return str(value or "").strip()


def _capped(value: Any, limit: int) -> str | None:
    """Stripped and length-capped, or None when the field carries nothing."""
    text = _text(value)
    return text[:limit] if text else None


def _first_text(item: dict[str, Any], *keys: str) -> str:
    """First non-empty value among the spellings a model may emit (camelCase or snake_case)."""
    for key in keys:
        text = _text(item.get(key))
        if text:
            return text
    return ""


def _string_list(value: Any, limit: int) -> list[str]:
    """Accept either a newline-joined block or a list, and return clean non-empty lines."""
    if isinstance(value, str):
        items = [line.strip() for line in value.split("\n")]
    elif isinstance(value, list):
        items = [str(entry).strip() for entry in value]
    else:
        return []
    return [entry for entry in items if entry][:limit]


def _gpa_pair(item: dict[str, Any]) -> tuple[float | None, float | None]:
    """Keep a GPA only when it and its scale are both numeric and mutually consistent."""
    try:
        raw_gpa = item.get("gpa")
        raw_scale = item.get("gpaScale", item.get("gpa_scale"))
        gpa = float(raw_gpa) if raw_gpa is not None else None
        scale = float(raw_scale) if raw_scale is not None else None
    except (TypeError, ValueError):
        return None, None
    if gpa is None or scale is None or not (0 < gpa <= scale):
        return None, None
    return gpa, scale


def _grounded_entries(
    raw: Any, *, quote_limit: int, limit: int, clean: Callable[[dict[str, Any]], None]
) -> list[dict[str, Any]]:
    """Drop entries without a quote - an ungrounded claim is discarded, never repaired."""
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        quote = _text(item.get("quote"))
        if not quote:
            continue
        item["quote"] = quote[:quote_limit]
        clean(item)
        entries.append(item)
    return entries[:limit]


def _clean_employment(emp: dict[str, Any]) -> None:
    emp["jobTitle"] = (
        _first_text(emp, "jobTitle", "job_title") or _text(emp.get("organization")) or "Professional"
    )[:300]
    emp["organization"] = _capped(emp.get("organization"), 300)
    emp["startDate"], _ = _sanitize_date_candidate(emp.get("startDate"))
    end_date, is_current = _sanitize_date_candidate(emp.get("endDate"))
    emp["endDate"] = end_date
    emp["isCurrent"] = is_current or bool(emp.get("isCurrent"))
    emp["responsibilities"] = _string_list(emp.get("responsibilities"), 20)


def _clean_education(edu: dict[str, Any]) -> None:
    # A degree line stands in for a missing institution rather than losing the entry.
    edu["institution"] = (_text(edu.get("institution")) or _text(edu.get("degree")) or "Institution")[:500]
    edu["degree"] = _capped(edu.get("degree"), 300)
    edu["fieldOfStudy"] = _capped(_first_text(edu, "fieldOfStudy", "field_of_study"), 300)
    edu["startDate"], _ = _sanitize_date_candidate(edu.get("startDate"))
    edu["endDate"], _ = _sanitize_date_candidate(edu.get("endDate"))
    status = _first_text(edu, "studentStatus", "student_status")
    edu["studentStatus"] = status if status in _STUDENT_STATUSES else None
    edu["gpa"], edu["gpaScale"] = _gpa_pair(edu)


def _clean_project(proj: dict[str, Any]) -> None:
    proj["name"] = (_text(proj.get("name")) or "Project")[:500]
    proj["description"] = _capped(proj.get("description"), 4000)


def _clean_certification(cert: dict[str, Any]) -> None:
    cert["name"] = (_text(cert.get("name")) or "Certification")[:500]
    cert["issuer"] = _capped(cert.get("issuer"), 300)
    cert["credentialId"] = _capped(_first_text(cert, "credentialId", "credential_id"), 300)


def _sanitize_candidate_payload(data: Any) -> dict[str, Any]:
    """Repair an untrusted LLM payload into the shape ``ResumeCandidate`` accepts.

    The schema's limits reject a payload; this layer caps and coerces instead,
    so one over-long field does not throw away an otherwise usable extraction.
    """
    if not isinstance(data, dict):
        return {}

    for field in ("headline", "summary"):
        item = data.get(field)
        if not isinstance(item, dict):
            data[field] = None
            continue
        value = _capped(item.get("value"), 2_000)
        quote = _capped(item.get("quote"), 6_000)
        data[field] = {"value": value, "quote": quote} if value and quote else None

    data["employment"] = _grounded_entries(
        data.get("employment", []), quote_limit=8_000, limit=20, clean=_clean_employment
    )
    data["education"] = _grounded_entries(
        data.get("education", []), quote_limit=8_000, limit=20, clean=_clean_education
    )
    data["projects"] = _grounded_entries(
        data.get("projects", []), quote_limit=8_000, limit=30, clean=_clean_project
    )
    data["certifications"] = _grounded_entries(
        data.get("certifications", []), quote_limit=4_000, limit=30, clean=_clean_certification
    )
    return data


class HybridResumeParser:
    def __init__(
        self,
        taxonomy: dict[str, tuple[str, tuple[str, ...]]] | None = None,
        taxonomy_version: str = "internal-2026.1",
        *,
        client: ModelServiceClient | None = None,
    ) -> None:
        self._deterministic = DeterministicResumeParser(taxonomy=taxonomy, taxonomy_version=taxonomy_version)
        self._career_classifier = DeterministicCareerClassifier()
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
            raw_data = _safe_json_parse(output)
            sanitized_data = _sanitize_candidate_payload(raw_data)
            candidate = ResumeCandidate.model_validate(sanitized_data)
        except (
            ModelServiceError,
            RuntimeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            Exception,
        ) as exc:
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
            if not quote or not quote.strip():
                return None
            matches = list(re.finditer(re.escape(quote), source.text))
            if not matches:
                words = quote.split()
                if words:
                    pattern = re.compile(r"\s+".join(re.escape(w) for w in words))
                    matches = list(pattern.finditer(source.text))
            if not matches:
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

        # Both readings are evidence-grounded; this only decides which one wins
        # a clash. The baseline used to come first everywhere and the LLM could
        # only append what the baseline's keys did not already contain, so a
        # wrong baseline entry permanently shadowed the correct LLM one.
        llm_first = get_settings().parser_llm_first_merge_enabled

        def merge_sources(llm_entries, baseline_entries, same):
            primary, secondary = (
                (llm_entries, baseline_entries) if llm_first else (baseline_entries, llm_entries)
            )
            merged = list(primary)
            for entry in secondary:
                if not any(same(entry, kept) for kept in merged):
                    merged.append(entry)
            return merged

        profile = baseline.resume.profile
        headline = profile.headline
        summary = profile.summary
        # Whatever the deterministic pass grounded the headline on, if anything.
        headline_refs = [
            item.evidence_id
            for item in baseline.resume.evidence
            if item.evidence_id.startswith("ev-headline-")
        ]
        if candidate.headline:
            # Ground it either way: a rejected quote still has to raise its warning.
            headline_ref = ground("headline", candidate.headline.quote)
            if headline_ref and (llm_first or not headline):
                headline = candidate.headline.value.strip()
                headline_refs = [headline_ref]
        if candidate.summary:
            summary_ref = ground("summary", candidate.summary.quote)
            if summary_ref and (llm_first or not summary):
                summary = candidate.summary.value.strip()

        llm_employment: list[EmploymentEntry] = []
        seen_employment: set[tuple[str, ...]] = set()
        for index, item in enumerate(candidate.employment, start=1):
            ref = ground("employment", item.quote)
            key = self._key(item.job_title, item.organization)
            if not ref or key in seen_employment:
                continue
            llm_employment.append(
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
        employment = merge_sources(
            llm_employment,
            list(baseline.resume.employment),
            lambda a, b: self._key(a.job_title, a.organization)
            == self._key(b.job_title, b.organization),
        )

        llm_education: list[EducationEntry] = []
        seen_education: set[tuple[str, ...]] = set()
        for index, item in enumerate(candidate.education, start=1):
            ref = ground("education", item.quote)
            key = self._key(item.institution, item.degree, item.field_of_study)
            if not ref or key in seen_education:
                continue
            llm_education.append(
                EducationEntry(
                    educationId=f"education-llm-{index:03d}",
                    institution=item.institution.strip(),
                    degree=item.degree.strip() if item.degree else None,
                    fieldOfStudy=item.field_of_study.strip() if item.field_of_study else None,
                    startDate=PartialDate.model_validate(item.start_date.model_dump())
                    if item.start_date
                    else None,
                    endDate=PartialDate.model_validate(item.end_date.model_dump()) if item.end_date else None,
                    studentStatus=item.student_status,
                    gpa=item.gpa,
                    gpaScale=item.gpa_scale,
                    evidenceRefs=[ref],
                )
            )
            seen_education.add(key)
        education = merge_sources(
            llm_education,
            list(baseline.resume.education),
            lambda a, b: self._key(a.institution, a.degree, a.field_of_study)
            == self._key(b.institution, b.degree, b.field_of_study),
        )

        llm_projects: list[ProjectEntry] = []
        for index, item in enumerate(candidate.projects, start=1):
            if (
                not _looks_like_project_title(item.name)
                or not _project_title_in_quote(item.name, item.quote)
                or any(_project_titles_overlap(item.name, kept.name) for kept in llm_projects)
                or any(
                    _project_name_key(item.name) == _project_name_key(job.job_title)
                    for job in baseline.resume.employment
                )
                or _is_founder_employer_project(item.name, baseline.resume.employment)
            ):
                continue
            ref = ground("project", item.quote)
            if not ref:
                continue
            llm_projects.append(
                ProjectEntry(
                    projectId=f"project-llm-{index:03d}",
                    name=item.name.strip(),
                    description=item.description.strip() if item.description else None,
                    evidenceRefs=[ref],
                )
            )
        projects = merge_sources(
            llm_projects,
            list(baseline.resume.projects),
            lambda a, b: _project_titles_overlap(a.name, b.name),
        )

        llm_certifications: list[CertificationEntry] = []
        seen_certifications: set[tuple[str, ...]] = set()
        for index, item in enumerate(candidate.certifications, start=1):
            ref = ground("certification", item.quote)
            key = self._key(item.name, item.issuer)
            if not ref or key in seen_certifications:
                continue
            llm_certifications.append(
                CertificationEntry(
                    certificationId=f"certification-llm-{index:03d}",
                    name=item.name.strip(),
                    issuer=item.issuer.strip() if item.issuer else None,
                    credentialId=item.credential_id.strip() if item.credential_id else None,
                    evidenceRefs=[ref],
                )
            )
            seen_certifications.add(key)
        certifications = merge_sources(
            llm_certifications,
            list(baseline.resume.certifications),
            lambda a, b: self._key(a.name, a.issuer) == self._key(b.name, b.issuer),
        )

        metadata = baseline.resume.parsing
        assert metadata is not None
        # The baseline classified before the LLM supplied a headline or extra
        # employment, so a CV whose only role signal is its headline came out
        # classified from skills alone. Redo it on the merged resume.
        resume = CanonicalResume.model_validate(
            baseline.resume.model_copy(
                update={
                    "profile": ResumeProfile(headline=headline, summary=summary),
                    "careerClassifications": self._career_classifier.classify(
                        skills=baseline.resume.skills,
                        employment=employment,
                        headline=headline,
                        headline_evidence_refs=headline_refs,
                    ),
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
