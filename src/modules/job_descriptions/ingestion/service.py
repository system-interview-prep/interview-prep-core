"""Orchestration service for external job ingestion."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from src.modules.job_descriptions.ingestion.adapters.greenhouse import GreenhouseJobBoardAdapter
from src.modules.job_descriptions.ingestion.models import (
    ExternalJobCandidate,
    IngestionConfig,
    IngestionMetrics,
    IngestionSummary,
)
from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository
from src.modules.job_descriptions.ingestion.sanitizer import HtmlSanitizer
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.taxonomy.facade import classify_career
from src.modules.user_cvs.facade import SourceBlock, SourceDocument

logger = logging.getLogger(__name__)


def _build_source_doc(job_id: str, content_hash: str, plain_text: str) -> SourceDocument:
    block = SourceBlock(
        block_id="block-0000",
        text=plain_text,
        page=1,
        reading_order=0,
        bounding_box=None,
        block_type="text",
        char_start=0,
        char_end=len(plain_text),
        section="other",
    )
    return SourceDocument(
        document_id=job_id,
        document_sha256=content_hash,
        text=plain_text,
        blocks=(block,),
    )


class JobIngestionService:
    """Orchestrates fetching, sanitizing, parsing, precedence resolution, and persisting external jobs."""

    def __init__(
        self,
        repository: JobIngestionRepository,
        adapter: GreenhouseJobBoardAdapter | None = None,
        parser: DeterministicJobDescriptionParser | None = None,
    ) -> None:
        self._repo = repository
        self._adapter = adapter or GreenhouseJobBoardAdapter()
        self._parser = parser or DeterministicJobDescriptionParser()

    async def ingest_candidates(
        self,
        config: IngestionConfig,
        candidates: list[ExternalJobCandidate],
        *,
        now: datetime | None = None,
    ) -> IngestionSummary:
        """Processes a list of pre-fetched candidates through sanitization, parsing, and database persistence."""
        started_at = now or datetime.now(UTC)
        metrics = IngestionMetrics()
        metrics.total_fetched = len(candidates)
        errors: list[str] = []
        seen_external_ids: list[str] = []

        for cand in candidates:
            try:
                seen_external_ids.append(cand.external_job_id)
                # 1. Sanitize HTML description and convert to plain text
                sanitized_html = HtmlSanitizer.sanitize_html(cand.raw_html)
                plain_text = HtmlSanitizer.to_plain_text(cand.raw_html)
                content_hash = hashlib.sha256((cand.raw_html or "").encode("utf-8")).hexdigest()

                # 2. Check if job already exists in DB
                existing = await self._repo.get_by_source_identity(
                    cand.source_type, cand.source_key, cand.external_job_id
                )

                if existing is None:
                    # New Job: Parse plain text with deterministic parser
                    parsed = None
                    try:
                        source = _build_source_doc(cand.external_job_id, content_hash, plain_text)
                        parsed = self._parser.parse(source, extraction_version="1.0")
                    except Exception as p_err:
                        logger.warning(
                            "Parser failed for job '%s' (%s): %s", cand.external_job_id, cand.title, p_err
                        )

                    # Classify taxonomy
                    taxonomy_concept_id: str | None = None
                    taxonomy_version: str | None = "v1"
                    if parsed and parsed.career_classifications:
                        taxonomy_concept_id = parsed.career_classifications[0].code
                        taxonomy_version = getattr(parsed.career_classifications[0], "taxonomy_version", "v1") or "v1"
                    else:
                        try:
                            classifications = classify_career(
                                {},
                                [(cand.title, [])],
                                minimum_skill_signals=0,
                                include_ancestors=False,
                            )
                            if classifications:
                                taxonomy_concept_id = classifications[0].code
                                taxonomy_version = classifications[0].taxonomy_version
                        except Exception as t_err:
                            logger.warning("Taxonomy classification failed for '%s': %s", cand.title, t_err)

                    # Insert new record
                    await self._repo.insert_candidate(
                        cand,
                        sanitized_html=sanitized_html,
                        plain_text=plain_text,
                        content_hash=content_hash,
                        parsed=parsed,
                        taxonomy_concept_id=taxonomy_concept_id,
                        taxonomy_version=taxonomy_version,
                        now=started_at,
                    )
                    metrics.created_count += 1
                else:
                    # Existing Job: Check human review flag
                    meta = existing.get("extracted_metadata") or {}
                    is_human_reviewed = bool(meta.get("is_human_reviewed") is True)

                    # Check if content changed
                    old_hash = meta.get("ingestion", {}).get("content_hash")
                    content_identical = (
                        old_hash == content_hash
                        and existing.get("title") == cand.title
                        and existing.get("location") == cand.location
                        and existing.get("apply_url") == cand.apply_url
                    )

                    if content_identical and not is_human_reviewed:
                        # Fast path: touch timestamps only
                        await self._repo.touch_timestamps(existing["id"], now=started_at)
                        metrics.unchanged_count += 1
                        metrics.updated_count += 1
                    else:
                        # Content changed or needs re-sync
                        parsed = None
                        taxonomy_concept_id = existing.get("primary_taxonomy_concept_id")
                        taxonomy_version = existing.get("primary_taxonomy_version") or "v1"

                        if not is_human_reviewed:
                            try:
                                source = _build_source_doc(cand.external_job_id, content_hash, plain_text)
                                parsed = self._parser.parse(source, extraction_version="1.0")
                            except Exception as p_err:
                                logger.warning(
                                    "Parser failed on update for '%s': %s", cand.external_job_id, p_err
                                )

                            if parsed and parsed.career_classifications:
                                taxonomy_concept_id = parsed.career_classifications[0].code
                                taxonomy_version = getattr(parsed.career_classifications[0], "taxonomy_version", "v1") or "v1"
                            else:
                                try:
                                    classifications = classify_career(
                                        {},
                                        [(cand.title, [])],
                                        minimum_skill_signals=0,
                                        include_ancestors=False,
                                    )
                                    if classifications:
                                        taxonomy_concept_id = classifications[0].code
                                        taxonomy_version = classifications[0].taxonomy_version
                                except Exception:
                                    pass

                        await self._repo.update_candidate(
                            existing["id"],
                            cand,
                            sanitized_html=sanitized_html,
                            plain_text=plain_text,
                            content_hash=content_hash,
                            parsed=parsed,
                            is_human_reviewed=is_human_reviewed,
                            taxonomy_concept_id=taxonomy_concept_id,
                            taxonomy_version=taxonomy_version,
                            now=started_at,
                        )
                        metrics.updated_count += 1
            except Exception as job_err:
                logger.error("Failed ingesting candidate '%s': %s", cand.external_job_id, job_err)
                errors.append(f"Job {cand.external_job_id}: {job_err}")
                metrics.failed_count += 1

        # 3. Close absent jobs older than grace threshold (if full batch succeeded with at least 1 job)
        if len(seen_external_ids) > 0 and len(errors) == 0:
            threshold = started_at - timedelta(hours=config.grace_period_hours)
            closed = await self._repo.close_missing_jobs(
                config.source_type,
                f"tenant:{config.board_token}",
                seen_external_ids,
                threshold_dt=threshold,
            )
            metrics.closed_count = closed

        finished_at = datetime.now(UTC)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        status = "COMPLETED" if not errors else ("PARTIAL" if metrics.created_count or metrics.updated_count else "FAILED")

        summary = IngestionSummary(
            source_type=config.source_type,
            source_key=f"tenant:{config.board_token}",
            board_token=config.board_token,
            status=status,
            metrics=metrics.as_dict(),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            errors=errors,
        )

        logger.info(
            "Ingestion finished for '%s' in %dms: %d fetched, %d created, %d updated, %d unchanged, %d closed, %d failed",
            config.board_token,
            duration_ms,
            metrics.total_fetched,
            metrics.created_count,
            metrics.updated_count,
            metrics.unchanged_count,
            metrics.closed_count,
            metrics.failed_count,
        )
        return summary

    async def ingest_board(self, config: IngestionConfig) -> IngestionSummary:
        """Fetches from Greenhouse API and ingests all jobs for the configured board."""
        started_at = datetime.now(UTC)
        try:
            candidates = await self._adapter.fetch_board_jobs(config)
            return await self.ingest_candidates(config, candidates, now=started_at)
        except Exception as exc:
            finished_at = datetime.now(UTC)
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)
            logger.error("Failed fetching board '%s': %s", config.board_token, exc)
            return IngestionSummary(
                source_type=config.source_type,
                source_key=f"tenant:{config.board_token}",
                board_token=config.board_token,
                status="FAILED",
                metrics=IngestionMetrics().as_dict(),
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                errors=[str(exc)],
            )
