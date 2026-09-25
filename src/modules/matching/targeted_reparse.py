from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from src.core.trace_logging import trace_event
from src.modules.matching.schemas import CanonicalJob, UnresolvedRequirement
from src.modules.user_cvs.schemas import CanonicalResume, EvidenceSpan

# This is intentionally a small, explicit alias safety net.  It is not a
# generic keyword matcher: generic words such as "backend" or "experience"
# can never create fallback evidence.
_ALIASES: dict[str, tuple[str, ...]] = {
    "c#": ("c#", "csharp"),
    ".net": (".net", "dotnet", "asp.net", "aspnet"),
    "python": ("python", "py"),
    "java": ("java",),
    "kafka": ("kafka",),
    "redis": ("redis",),
    "mysql": ("mysql",),
    "postgresql": ("postgresql", "postgres", "psql"),
    "docker": ("docker",),
    "kubernetes": ("kubernetes", "k8s"),
    "aws": ("aws", "amazon web services"),
    "linux": ("linux",),
}


def _explicit_aliases(requirement: UnresolvedRequirement) -> list[tuple[str, str]]:
    labels: list[str] = []
    labels.extend(concept.label for concept in requirement.atomic_concepts)
    labels.extend(_ALIASES)
    raw = requirement.raw_label.casefold()
    found: list[tuple[str, str]] = []
    for label in labels:
        key = label.casefold().strip()
        aliases = _ALIASES.get(key, (key,))
        key_is_explicit = key in raw
        known_concept = key in _ALIASES
        for alias in aliases:
            # Alias must be explicitly present in the JD, or be a known alias
            # for a concept that is explicitly present in the JD.
            if not known_concept and not key_is_explicit:
                continue
            if known_concept and key_is_explicit:
                found.append((key, alias))
            elif re.search(rf"(?<![\w+#]){re.escape(alias.casefold())}(?![\w+#])", raw):
                found.append((key, alias))
    # If no taxonomy concepts were provided, scan only known technology names
    # in the raw label (never arbitrary requirement words).
    if not requirement.atomic_concepts:
        for key, aliases in _ALIASES.items():
            if re.search(rf"(?<![\w+#]){re.escape(key)}(?![\w+#])", raw):
                found.extend((key, alias) for alias in aliases if alias in raw)
    return list(dict.fromkeys(found))


def _find_spans(raw_text: str, aliases: Iterable[tuple[str, str]]) -> list[tuple[str, str, int, int]]:
    found: list[tuple[str, str, int, int]] = []
    for concept, alias in aliases:
        for match in re.finditer(rf"(?<![\w+#]){re.escape(alias)}(?![\w+#])", raw_text, re.I):
            found.append((concept, alias, match.start(), match.end()))
    return sorted(found, key=lambda item: (item[2], item[3], item[0], item[1]))


def _evidence_id(resume: CanonicalResume, start: int, end: int, text: str) -> str:
    digest = hashlib.sha256(f"{resume.document_id}:{start}:{end}:{text}".encode()).hexdigest()[:16]
    return f"fallback-evidence-{digest}"


def reparse_partial_resume(resume: CanonicalResume, job: CanonicalJob) -> CanonicalResume:
    parsing = resume.parsing
    raw_text = resume.raw_text
    if parsing and parsing.status == "ready":
        return resume
    canonical_partial = bool(
        parsing
        and any(
            value in {"partial", "unavailable"}
            for value in parsing.canonical_section_coverage.values()
        )
    )
    if (
        parsing
        and parsing.raw_text_coverage == "complete"
        and parsing.evidence_index_coverage == "complete"
        and not canonical_partial
    ):
        return resume
    if not raw_text:
        trace_event(
            "matching",
            "targeted_reparse",
            resume_id=resume.resume_id,
            status="no_recoverable_evidence",
            source="raw_text",
            reason_code="raw_text_unavailable",
            raw_text_coverage=parsing.raw_text_coverage if parsing else "legacy",
            canonical_section_coverage=parsing.canonical_section_coverage if parsing else {},
            evidence_index_coverage=parsing.evidence_index_coverage if parsing else "legacy",
        )
        return resume

    existing = {item.evidence_id for item in resume.evidence}
    recovered: list[EvidenceSpan] = []
    recovered_by_requirement: list[dict[str, object]] = []
    for requirement in job.requirements:
        if not isinstance(requirement, UnresolvedRequirement):
            continue
        aliases = _explicit_aliases(requirement)
        spans = _find_spans(raw_text, aliases)
        refs: list[str] = []
        matched_aliases: list[str] = []
        concept_ids: list[str] = []
        for concept, alias, start, end in spans:
            text = raw_text[start:end]
            evidence_id = _evidence_id(resume, start, end, text)
            if evidence_id in existing:
                refs.append(evidence_id)
                continue
            existing.add(evidence_id)
            matched_aliases.append(alias)
            concept_ids.append(concept)
            recovered.append(
                EvidenceSpan(
                    evidenceId=evidence_id,
                    documentId=resume.document_id,
                    documentSha256=resume.document_sha256,
                    section="raw_text_fallback",
                    text=text,
                    charStart=start,
                    charEnd=end,
                )
            )
            refs.append(evidence_id)
        if refs:
            recovered_by_requirement.append(
                {
                    "requirement_id": requirement.requirement_id,
                    "concept_ids": list(dict.fromkeys(concept_ids)),
                    "matched_aliases": list(dict.fromkeys(matched_aliases)),
                    "evidence_refs": refs,
                }
            )

    trace_event(
        "matching",
        "targeted_reparse",
        resume_id=resume.resume_id,
        status="evidence_recovered" if recovered else "no_recoverable_evidence",
        source="raw_text",
        parser_status=parsing.status if parsing else None,
        parser_version=parsing.parser_version if parsing else None,
        parser_warning_codes=[warning.code for warning in parsing.warnings] if parsing else [],
        raw_text_coverage=parsing.raw_text_coverage if parsing else "legacy",
        canonical_section_coverage=parsing.canonical_section_coverage if parsing else {},
        evidence_index_coverage=parsing.evidence_index_coverage if parsing else "legacy",
        canonical_evidence_count=len(resume.evidence),
        recovered_count=len(recovered),
        recovered_by_requirement=recovered_by_requirement,
        recovered_evidence_refs=[item.evidence_id for item in recovered],
    )
    if not recovered:
        return resume
    return resume.model_copy(update={"evidence": [*resume.evidence, *recovered]})

