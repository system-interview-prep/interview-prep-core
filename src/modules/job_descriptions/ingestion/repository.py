"""Database repository for external job ingestion and idempotent upserts."""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.ingestion.models import ExternalJobCandidate

logger = logging.getLogger(__name__)


class JobIngestionRepository:
    """Handles database queries, upserts, human-review protection, and lifecycle updates."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_source_identity(
        self, source_type: str, source_key: str, external_job_id: str
    ) -> dict[str, Any] | None:
        """Finds an existing job description by its multi-tenant external identity."""
        query = text(
            """
            SELECT id, title, company_name, location, work_mode, employment_type, seniority,
                   experience_min_years, experience_max_years, salary_min, salary_max,
                   salary_currency, salary_period, salary_negotiable,
                   primary_taxonomy_concept_id, primary_taxonomy_version,
                   source_url, apply_url, posted_at, first_seen_at, last_seen_at,
                   listing_status, processing_status, extracted_metadata, structured_data
            FROM job_descriptions
            WHERE source_type = :source_type
              AND source_key = :source_key
              AND external_job_id = :external_job_id
            LIMIT 1;
            """
        )
        res = await self._session.execute(
            query,
            {
                "source_type": source_type,
                "source_key": source_key,
                "external_job_id": external_job_id,
            },
        )
        row = res.mappings().one_or_none()
        return dict(row) if row else None

    async def insert_candidate(
        self,
        candidate: ExternalJobCandidate,
        *,
        sanitized_html: str,
        plain_text: str,
        content_hash: str,
        parsed: CanonicalJobDescription | None,
        taxonomy_concept_id: str | None = None,
        taxonomy_version: str | None = "v1",
        now: datetime | None = None,
    ) -> str:
        """Inserts a newly discovered external job into job_descriptions."""
        now_dt = now or datetime.now(UTC)
        job_id = str(uuid.uuid4())

        # Extract facts from parser output if present
        exp_min = parsed.experience_min_years if parsed else None
        exp_max = parsed.experience_max_years if parsed else None
        sal_min = parsed.salary_min if parsed else None
        sal_max = parsed.salary_max if parsed else None
        sal_curr = parsed.salary_currency if parsed else None
        sal_period = parsed.salary_period if parsed else None
        sal_neg = parsed.salary_negotiable if parsed else None
        work_mode = parsed.work_mode if parsed else None
        seniority = parsed.seniority if parsed else None

        keywords: list[str] = []
        if parsed and parsed.requirements:
            for req in parsed.requirements:
                if req.skill_id and req.skill_id.startswith("skill-"):
                    clean_kw = req.skill_id.replace("skill-", "")
                    if clean_kw not in keywords:
                        keywords.append(clean_kw)

        extracted_metadata = {
            "ingestion": {
                "source_type": candidate.source_type,
                "source_key": candidate.source_key,
                "content_hash": content_hash,
                "fetched_at": now_dt.isoformat(),
                **candidate.metadata,
            },
            "raw_external_job": candidate.raw_payload,
            "is_human_reviewed": False,
        }

        structured_data = parsed.model_dump(by_alias=True) if parsed else None

        search_text = f"{candidate.title} {candidate.company_name} {candidate.location or ''} {plain_text[:2000]}".strip()

        insert_stmt = text(
            """
            INSERT INTO job_descriptions (
                id, item_type, title, company_name, company_logo_url, location,
                work_mode, employment_type, seniority,
                experience_min_years, experience_max_years,
                salary_min, salary_max, salary_currency, salary_period, salary_negotiable,
                primary_taxonomy_concept_id, primary_taxonomy_version,
                source_type, source_key, source_name, source_url, apply_url,
                external_job_id, posted_at, first_seen_at, last_seen_at, fetched_at,
                processing_status, listing_status, status,
                keywords, description, search_text, raw_text,
                structured_data, extracted_metadata, parse_source, extraction_version,
                extracted_at, created_at, updated_at
            ) VALUES (
                :id, 'JOB_DESCRIPTION', :title, :company_name, :company_logo_url, :location,
                :work_mode, :employment_type, :seniority,
                :experience_min_years, :experience_max_years,
                :salary_min, :salary_max, :salary_currency, :salary_period, :salary_negotiable,
                :primary_taxonomy_concept_id, :primary_taxonomy_version,
                :source_type, :source_key, :source_name, :source_url, :apply_url,
                :external_job_id, :posted_at, :first_seen_at, :last_seen_at, :fetched_at,
                'DONE', 'ACTIVE', 'ACTIVE',
                :keywords, :description, :search_text, :raw_text,
                CAST(:structured_data AS jsonb), CAST(:extracted_metadata AS jsonb),
                'greenhouse_adapter+deterministic_v4', '1.0',
                now(), :first_seen_at, :first_seen_at
            );
            """
        )

        await self._session.execute(
            insert_stmt,
            {
                "id": job_id,
                "title": candidate.title,
                "company_name": candidate.company_name,
                "company_logo_url": candidate.company_logo_url,
                "location": candidate.location,
                "work_mode": work_mode,
                "employment_type": None,
                "seniority": seniority,
                "experience_min_years": exp_min,
                "experience_max_years": exp_max,
                "salary_min": sal_min,
                "salary_max": sal_max,
                "salary_currency": sal_curr,
                "salary_period": sal_period,
                "salary_negotiable": sal_neg,
                "primary_taxonomy_concept_id": taxonomy_concept_id,
                "primary_taxonomy_version": taxonomy_version,
                "source_type": candidate.source_type,
                "source_key": candidate.source_key,
                "source_name": candidate.source_name,
                "source_url": candidate.source_url,
                "apply_url": candidate.apply_url,
                "external_job_id": candidate.external_job_id,
                "posted_at": candidate.posted_at,
                "first_seen_at": now_dt,
                "last_seen_at": now_dt,
                "fetched_at": now_dt,
                "keywords": keywords,
                "description": sanitized_html,
                "search_text": search_text,
                "raw_text": plain_text,
                "structured_data": json.dumps(structured_data) if structured_data else None,
                "extracted_metadata": json.dumps(extracted_metadata),
            },
        )
        return job_id

    async def update_candidate(
        self,
        existing_id: str,
        candidate: ExternalJobCandidate,
        *,
        sanitized_html: str,
        plain_text: str,
        content_hash: str,
        parsed: CanonicalJobDescription | None,
        is_human_reviewed: bool,
        taxonomy_concept_id: str | None = None,
        taxonomy_version: str | None = "v1",
        now: datetime | None = None,
    ) -> None:
        """Updates an existing external job, strictly protecting human-reviewed fields."""
        now_dt = now or datetime.now(UTC)

        if is_human_reviewed:
            # Protected mode: only touch telemetry, timestamps, and raw audit payload
            stmt = text(
                """
                UPDATE job_descriptions SET
                    last_seen_at = :last_seen_at,
                    fetched_at = :fetched_at,
                    listing_status = CASE
                        WHEN listing_status IN ('CLOSED', 'EXPIRED') THEN 'ACTIVE'
                        ELSE listing_status
                    END,
                    extracted_metadata = jsonb_set(
                        COALESCE(extracted_metadata, '{}'::jsonb),
                        '{raw_external_job}',
                        CAST(:raw_external_job AS jsonb)
                    ),
                    updated_at = now()
                WHERE id = :id;
                """
            )
            await self._session.execute(
                stmt,
                {
                    "id": existing_id,
                    "last_seen_at": now_dt,
                    "fetched_at": now_dt,
                    "raw_external_job": json.dumps(candidate.raw_payload),
                },
            )
            return

        # Not human reviewed: update published fields from external source & parser
        exp_min = parsed.experience_min_years if parsed else None
        exp_max = parsed.experience_max_years if parsed else None
        sal_min = parsed.salary_min if parsed else None
        sal_max = parsed.salary_max if parsed else None
        sal_curr = parsed.salary_currency if parsed else None
        sal_period = parsed.salary_period if parsed else None
        sal_neg = parsed.salary_negotiable if parsed else None
        work_mode = parsed.work_mode if parsed else None
        seniority = parsed.seniority if parsed else None

        keywords: list[str] = []
        if parsed and parsed.requirements:
            for req in parsed.requirements:
                if req.skill_id and req.skill_id.startswith("skill-"):
                    clean_kw = req.skill_id.replace("skill-", "")
                    if clean_kw not in keywords:
                        keywords.append(clean_kw)

        extracted_metadata = {
            "ingestion": {
                "source_type": candidate.source_type,
                "source_key": candidate.source_key,
                "content_hash": content_hash,
                "fetched_at": now_dt.isoformat(),
                **candidate.metadata,
            },
            "raw_external_job": candidate.raw_payload,
            "is_human_reviewed": False,
        }

        structured_data = parsed.model_dump(by_alias=True) if parsed else None
        search_text = f"{candidate.title} {candidate.company_name} {candidate.location or ''} {plain_text[:2000]}".strip()

        stmt = text(
            """
            UPDATE job_descriptions SET
                title = :title,
                company_name = :company_name,
                company_logo_url = :company_logo_url,
                location = :location,
                work_mode = :work_mode,
                seniority = :seniority,
                experience_min_years = :experience_min_years,
                experience_max_years = :experience_max_years,
                salary_min = :salary_min,
                salary_max = :salary_max,
                salary_currency = :salary_currency,
                salary_period = :salary_period,
                salary_negotiable = :salary_negotiable,
                primary_taxonomy_concept_id = COALESCE(:taxonomy_concept_id, primary_taxonomy_concept_id),
                primary_taxonomy_version = COALESCE(:taxonomy_version, primary_taxonomy_version),
                source_name = :source_name,
                source_url = :source_url,
                apply_url = :apply_url,
                posted_at = COALESCE(:posted_at, posted_at),
                last_seen_at = :last_seen_at,
                fetched_at = :fetched_at,
                listing_status = 'ACTIVE',
                status = 'ACTIVE',
                keywords = :keywords,
                description = :description,
                search_text = :search_text,
                raw_text = :raw_text,
                structured_data = CAST(:structured_data AS jsonb),
                extracted_metadata = CAST(:extracted_metadata AS jsonb),
                updated_at = now()
            WHERE id = :id;
            """
        )

        await self._session.execute(
            stmt,
            {
                "id": existing_id,
                "title": candidate.title,
                "company_name": candidate.company_name,
                "company_logo_url": candidate.company_logo_url,
                "location": candidate.location,
                "work_mode": work_mode,
                "seniority": seniority,
                "experience_min_years": exp_min,
                "experience_max_years": exp_max,
                "salary_min": sal_min,
                "salary_max": sal_max,
                "salary_currency": sal_curr,
                "salary_period": sal_period,
                "salary_negotiable": sal_neg,
                "taxonomy_concept_id": taxonomy_concept_id,
                "taxonomy_version": taxonomy_version,
                "source_name": candidate.source_name,
                "source_url": candidate.source_url,
                "apply_url": candidate.apply_url,
                "posted_at": candidate.posted_at,
                "last_seen_at": now_dt,
                "fetched_at": now_dt,
                "keywords": keywords,
                "description": sanitized_html,
                "search_text": search_text,
                "raw_text": plain_text,
                "structured_data": json.dumps(structured_data) if structured_data else None,
                "extracted_metadata": json.dumps(extracted_metadata),
            },
        )

    async def touch_timestamps(self, existing_id: str, now: datetime | None = None) -> None:
        """Fast path: updates only last_seen_at and fetched_at when content hash is unchanged."""
        now_dt = now or datetime.now(UTC)
        stmt = text(
            """
            UPDATE job_descriptions SET
                last_seen_at = :last_seen_at,
                fetched_at = :fetched_at,
                listing_status = CASE
                    WHEN listing_status IN ('CLOSED', 'EXPIRED') THEN 'ACTIVE'
                    ELSE listing_status
                END
            WHERE id = :id;
            """
        )
        await self._session.execute(stmt, {"id": existing_id, "last_seen_at": now_dt, "fetched_at": now_dt})

    async def close_missing_jobs(
        self,
        source_type: str,
        source_key: str,
        seen_external_ids: list[str],
        *,
        threshold_dt: datetime,
    ) -> int:
        """Safely marks jobs as CLOSED when absent from complete board sync and older than grace threshold."""
        if not seen_external_ids:
            # Do not close jobs if seen list is empty (potential board error protection)
            return 0

        stmt = text(
            """
            UPDATE job_descriptions SET
                listing_status = 'CLOSED',
                updated_at = now()
            WHERE source_type = :source_type
              AND source_key = :source_key
              AND listing_status = 'ACTIVE'
              AND external_job_id NOT IN :seen_ids
              AND last_seen_at < :threshold;
            """
        )
        res = await self._session.execute(
            stmt,
            {
                "source_type": source_type,
                "source_key": source_key,
                "seen_ids": tuple(seen_external_ids),
                "threshold": threshold_dt,
            },
        )
        return res.rowcount or 0
