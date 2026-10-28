"""Run the local matching facade against every CV in a corpus and a stored JD.

This is an offline, read-only evaluation helper. PDF text is extracted with
pypdf, CV claims use the deterministic parser, and scores use in-memory BM25;
no CV/JD row or external object is created or modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text

try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.core.config import get_settings
from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.domain.adapters import job_description_to_matching_job
from src.modules.matching.domain.schemas import MatchRequest, MatchingPolicy
from src.modules.matching.retrieval.bm25_provider import InMemoryBm25Provider
from src.modules.user_cvs.parsing.application.pipeline import _coverage_snapshot
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser


def _sync_database_url(value: str) -> str:
    parts = urlsplit(value)
    scheme = parts.scheme.replace("+asyncpg", "")
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _load_job(job_id: str) -> tuple[str, CanonicalJobDescription]:
    settings = get_settings()
    engine = create_engine(_sync_database_url(settings.database_url), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT filename, structured_data FROM job_descriptions "
                    "WHERE id = :job_id AND item_type = 'JOB_DESCRIPTION'"
                ),
                {"job_id": job_id},
            ).mappings().one_or_none()
            if row is None or not row["structured_data"]:
                raise RuntimeError(f"JD {job_id} has no finalized structured data")
            return str(row["filename"]), CanonicalJobDescription.model_validate(row["structured_data"])
    finally:
        engine.dispose()


def _extract_pdf(path: Path) -> tuple[list[str], str]:
    reader = PdfReader(str(path), strict=False)
    pages = [page.extract_text() or "" for page in reader.pages]
    return pages, "\n\n".join(page for page in pages if page.strip())


def _extract_docx(path: Path) -> tuple[list[str], str]:
    from docx import Document

    document = Document(str(path))
    blocks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                blocks.append(" | ".join(cells))
    raw_text = "\n".join(blocks)
    return ([raw_text] if raw_text else []), raw_text


def _parse_job_docx(path: Path, job_id: str) -> tuple[str, CanonicalJobDescription]:
    pages, raw_text = _extract_docx(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    artifacts = DocumentArtifacts(
        markdown=raw_text,
        content_list=[{"type": "text", "text": page, "page_idx": index} for index, page in enumerate(pages)],
        extractor_version="python-docx-v1",
    )
    source = build_source_document(
        artifacts, document_id=f"jd-corpus-{digest[:20]}", document_sha256=digest
    )
    parsed = DeterministicJobDescriptionParser().parse(
        source, extraction_version="python-docx-v1"
    )
    return path.name, parsed


def _merge_source_jd_review(
    job_id: str, source_path: Path
) -> tuple[str, CanonicalJobDescription]:
    """Apply deterministic, reviewed corrections while retaining saved JD concepts.

    The production JD was originally parsed by the hybrid parser and contains
    canonical concepts absent from a deterministic-only reparse. Merge only
    exact-label changes so corpus evaluation still uses the system's current
    canonical JD semantics.
    """
    filename, stored = _load_job(job_id)
    _, fresh = _parse_job_docx(source_path, job_id)

    def key(value: str | None) -> str:
        folded = " ".join((value or "").replace("\u00a0", " ").casefold().split())
        return re.sub(r"[^a-z0-9]+", "", folded)

    fresh_by_label = {key(item.raw_label): item for item in fresh.requirements}
    merged = []
    for current in stored.requirements:
        candidate = fresh_by_label.get(key(current.raw_label))
        if candidate is None:
            current_key = key(current.raw_label)
            partial_matches = [
                item
                for item in fresh.requirements
                if current_key
                and (
                    key(item.raw_label).startswith(current_key)
                    or current_key.startswith(key(item.raw_label))
                )
            ]
            if len(partial_matches) == 1:
                candidate = partial_matches[0]
        if candidate is None:
            merged.append(current)
            continue
        updates: dict = {}
        if current.priority != candidate.priority:
            updates["priority"] = candidate.priority
        # Only promote deterministic atoms when the source parser can account
        # for the complete phrase through known technology aliases.
        if candidate.atomic_concepts:
            updates.update(
                {
                    "kind": candidate.kind,
                    "concept": candidate.concept,
                    "atomic_concepts": candidate.atomic_concepts,
                    "group_operator": candidate.group_operator,
                    "group_id": candidate.group_id,
                }
            )
        if candidate.minimum_experience_months is not None:
            updates["minimum_experience_months"] = candidate.minimum_experience_months
        merged.append(current.model_copy(update=updates) if updates else current)

    return filename, stored.model_copy(update={"requirements": merged})


def _evaluate_file(path: Path, job, facade: MatchingFacade, parser) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result: dict = {"filename": path.name, "sha256": digest}
    try:
        pages, raw_text = (
            _extract_docx(path) if path.suffix.casefold() == ".docx" else _extract_pdf(path)
        )
    except Exception as exc:  # malformed PDFs remain visible as corpus failures
        return {**result, "processing": "pdf_error", "error": type(exc).__name__}

    if not raw_text.strip():
        return {
            **result,
            "processing": "ocr_required",
            "page_count": len(pages),
            "raw_characters": 0,
            "requirements": [],
        }

    # A small independent lexical audit helps reviewers spot group-operator
    # errors (for example a three-technology AND-list passing on one term).
    normalized_raw_text = raw_text.casefold()
    raw_term_presence = {
        term: bool(re.search(pattern, normalized_raw_text, re.I))
        for term, pattern in {
            "csharp": r"(?<!\w)c\#(?!\w)|\bcsharp\b",
            "dotnet": r"(?<!\w)\.net(?!\w)|\bdotnet\b|\basp\.net\b",
            "kafka": r"\bkafka\b",
            "redis": r"\bredis\b",
            "mysql": r"\bmysql\b",
            "aws": r"\baws\b",
            "kubernetes": r"\bkubernetes\b|\bk8s\b",
            "docker": r"\bdocker\b",
            "docker_swarm": r"\bdocker\s+swarm\b",
            "monitoring": r"\bmonitor(?:ing|ed|s)?\b|\btelemetry\b|\bobservability\b",
            "cicd": r"\bci\s*/\s*cd\b|\bcontinuous\s+(?:integration|delivery|deployment)\b",
        }.items()
    }

    document_id = f"corpus-{digest[:24]}"
    artifacts = DocumentArtifacts(
        markdown=raw_text,
        content_list=[
            {"type": "text", "text": page, "page_idx": index}
            for index, page in enumerate(pages)
            if page.strip()
        ],
        extractor_version="pypdf-text-v1",
    )
    source = build_source_document(
        artifacts,
        document_id=document_id,
        document_sha256=digest,
    )
    try:
        parsed = parser.parse(source, extraction_version="pypdf-text-v1")
    except Exception as exc:
        return {
            **result,
            "processing": "parser_error",
            "page_count": len(pages),
            "raw_characters": len(raw_text),
            "error": type(exc).__name__,
        }
    coverage = _coverage_snapshot(parsed, source)
    metadata = parsed.resume.parsing
    if metadata is not None:
        metadata = metadata.model_copy(
            update={
                "rawTextCoverage": coverage["raw_text_coverage"],
                "canonicalSectionCoverage": coverage["canonical_section_coverage"],
                "evidenceIndexCoverage": coverage["evidence_index_coverage"],
                "coverageReasonCodes": coverage["coverage_reason_codes"],
            }
        )
        resume = parsed.resume.model_copy(update={"parsing": metadata})
    else:
        resume = parsed.resume

    request = MatchRequest(
        schemaVersion="2.1",
        resume=resume,
        job=job,
        matchingPolicy=MatchingPolicy(bm25ProviderMode="in_memory"),
    )
    match = facade.match(request)
    requirement_results = {item.requirement_id: item for item in match.requirement_results}
    requirements = []
    for requirement in job.requirements:
        evaluated = requirement_results[requirement.requirement_id]
        refs = set(evaluated.evidence_refs)
        evidence_by_id = {item.evidence_id: item for item in resume.evidence}
        requirements.append(
            {
                "requirement_id": requirement.requirement_id,
                "priority": requirement.priority,
                "kind": getattr(requirement, "kind", requirement.type),
                "raw_label": getattr(requirement, "raw_label", None)
                or getattr(getattr(requirement, "skill", None), "label", None),
                "group_operator": getattr(requirement, "group_operator", None),
                "atomic_concepts": [item.label for item in getattr(requirement, "atomic_concepts", [])],
                "status": evaluated.status,
                "reason_code": evaluated.reason_code,
                "confidence": evaluated.confidence,
                "evidence_refs": evaluated.evidence_refs,
                "evidence_text": [evidence_by_id[ref].text for ref in refs if ref in evidence_by_id],
                "concept_results": [
                    {
                        "label": item.label,
                        "status": item.status,
                        "reason_code": item.reason_code,
                    }
                    for item in evaluated.concept_results
                ],
            }
        )
    return {
        **result,
        "processing": "matched",
        "page_count": len(pages),
        "raw_characters": len(raw_text),
        "raw_term_presence": raw_term_presence,
        "section_counts": coverage["section_counts"],
        "raw_text_coverage": coverage["raw_text_coverage"],
        "canonical_section_coverage": coverage["canonical_section_coverage"],
        "eligibility": match.eligibility,
        "fit_band": match.fit_band,
        "diagnostic_score": match.diagnostic_score,
        "failed_must_have_requirement_ids": match.failed_must_have_requirements,
        "requirements": requirements,
    }


def main() -> int:
    parser_args = argparse.ArgumentParser(description=__doc__)
    parser_args.add_argument("--job-id", default="2505c411-d53e-421f-98ab-41ef54df571a")
    parser_args.add_argument("--cv-dir", type=Path, default=Path("../test/Data-CV/Data-CV"))
    parser_args.add_argument("--jd-docx", type=Path, default=None,
                             help="Parse this source JD with the current deterministic parser instead of stored structured data")
    parser_args.add_argument("--output", type=Path, default=Path("reports/jd_2505_corpus_baseline.json"))
    args = parser_args.parse_args()

    job_filename, parsed_job = (
        _merge_source_jd_review(args.job_id, args.jd_docx)
        if args.jd_docx
        else _load_job(args.job_id)
    )
    job = job_description_to_matching_job(parsed_job, job_id=args.job_id)
    facade = MatchingFacade(bm25_provider=InMemoryBm25Provider())
    resume_parser = DeterministicResumeParser()
    paths = [
        path
        for path in sorted(args.cv_dir.glob("*"), key=lambda item: item.name.casefold())
        if path.is_file() and path.suffix.casefold() in {".pdf", ".docx"}
    ]
    results = []
    for index, path in enumerate(paths, start=1):
        print(f"[{index}/{len(paths)}] {path.name}", flush=True)
        results.append(_evaluate_file(path, job, facade, resume_parser))
    status_counts = {
        requirement.requirement_id: Counter(
            item["status"]
            for case in results
            for item in case.get("requirements", [])
            if item["requirement_id"] == requirement.requirement_id
        )
        for requirement in job.requirements
    }
    report = {
        "job_id": args.job_id,
        "job_filename": job_filename,
        "job_title": parsed_job.job_title,
        "parser_version": "deterministic-corpus-eval-v1",
        "matching_policy": "MatchingFacade + in-memory BM25; no persistence or external providers",
        "cv_count": len(results),
        "processing_counts": dict(Counter(item["processing"] for item in results)),
        "requirements": [
            {
                "requirement_id": requirement.requirement_id,
                "priority": requirement.priority,
                "raw_label": getattr(requirement, "raw_label", None)
                or getattr(getattr(requirement, "skill", None), "label", None),
                "statuses": dict(status_counts[requirement.requirement_id]),
            }
            for requirement in job.requirements
        ],
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("job_id", "job_filename", "job_title", "cv_count", "processing_counts", "requirements")}, ensure_ascii=False, indent=2))
    print(f"report={args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
