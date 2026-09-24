"""Greenhouse Job Board API adapter."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from src.modules.job_descriptions.ingestion.models import ExternalJobCandidate, IngestionConfig
from src.modules.job_descriptions.ingestion.sanitizer import HtmlSanitizer

logger = logging.getLogger(__name__)


class GreenhouseError(Exception):
    """Base exception for Greenhouse ingestion operations."""


class GreenhouseBoardNotFoundError(GreenhouseError):
    """Raised when the specified Greenhouse board token does not exist (HTTP 404)."""


class GreenhouseRateLimitError(GreenhouseError):
    """Raised when Greenhouse returns HTTP 429 and retries have been exhausted."""


class GreenhouseApiError(GreenhouseError):
    """Raised when Greenhouse returns an unexpected HTTP error."""


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Safely parses an ISO 8601 timestamp string into a UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        # Replace 'Z' with '+00:00' for standard fromisoformat compatibility
        cleaned = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


class GreenhouseJobBoardAdapter:
    """Client and mapper for Greenhouse public Job Board API."""

    BASE_URL = "https://boards-api.greenhouse.io/v1/boards"

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client

    def parse_job_item(self, item: dict[str, Any], config: IngestionConfig) -> ExternalJobCandidate | None:
        """Maps a single Greenhouse job item into an ExternalJobCandidate."""
        raw_id = item.get("id")
        if raw_id is None:
            return None

        external_job_id = str(raw_id).strip()
        title = str(item.get("title") or "").strip()
        if not title:
            return None

        location_obj = item.get("location")
        location_name: str | None = None
        if isinstance(location_obj, dict):
            location_name = location_obj.get("name")
            if location_name:
                location_name = str(location_name).strip() or None

        absolute_url = item.get("absolute_url")
        valid_url = HtmlSanitizer.validate_url(str(absolute_url) if absolute_url else None)

        posted_at = parse_iso_datetime(item.get("updated_at"))

        metadata = {
            "requisition_id": item.get("requisition_id"),
            "departments": item.get("departments"),
            "offices": item.get("offices"),
            "internal_job_id": item.get("internal_job_id"),
        }

        return ExternalJobCandidate(
            external_job_id=external_job_id,
            title=title,
            company_name=config.company_name,
            company_logo_url=config.company_logo_url,
            location=location_name,
            raw_html=str(item.get("content") or ""),
            source_type="greenhouse",
            source_key=f"tenant:{config.board_token}",
            source_name=config.company_name,
            source_url=valid_url,
            apply_url=valid_url,
            posted_at=posted_at,
            raw_payload=item,
            metadata=metadata,
        )

    def parse_board_payload(
        self, payload: dict[str, Any], config: IngestionConfig
    ) -> list[ExternalJobCandidate]:
        """Maps a full Greenhouse board API response payload into a list of ExternalJobCandidate."""
        jobs_data = payload.get("jobs")
        if not isinstance(jobs_data, list):
            return []

        candidates: list[ExternalJobCandidate] = []
        for item in jobs_data:
            if isinstance(item, dict):
                cand = self.parse_job_item(item, config)
                if cand:
                    candidates.append(cand)
        return candidates

    async def fetch_board_jobs(self, config: IngestionConfig) -> list[ExternalJobCandidate]:
        """Fetches all jobs with full content from the Greenhouse Job Board API with retry backoff."""
        url = f"{self.BASE_URL}/{config.board_token}/jobs?content=true"

        client_created = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.timeout_seconds),
                headers={"User-Agent": "Intervia-JobCrawler/1.0"},
            )
            client_created = True

        try:
            attempt = 0
            while attempt < config.max_retries:
                attempt += 1
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        payload = response.json()
                        return self.parse_board_payload(payload, config)

                    if response.status_code == 404:
                        raise GreenhouseBoardNotFoundError(
                            f"Greenhouse board '{config.board_token}' was not found (HTTP 404)."
                        )

                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        wait_seconds = float(retry_after) if retry_after else float(2**attempt)
                        logger.warning(
                            "Greenhouse rate limit 429 on board '%s', waiting %.1fs (attempt %d/%d)",
                            config.board_token,
                            wait_seconds,
                            attempt,
                            config.max_retries,
                        )
                        await asyncio.sleep(wait_seconds)
                        continue

                    if response.status_code >= 500:
                        wait_seconds = float(2**attempt)
                        logger.warning(
                            "Greenhouse server error %d on board '%s', waiting %.1fs (attempt %d/%d)",
                            response.status_code,
                            config.board_token,
                            wait_seconds,
                            attempt,
                            config.max_retries,
                        )
                        await asyncio.sleep(wait_seconds)
                        continue

                    raise GreenhouseApiError(
                        f"Greenhouse API returned unexpected status {response.status_code} for board '{config.board_token}'."
                    )
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                    if attempt >= config.max_retries:
                        raise GreenhouseApiError(
                            f"Network error connecting to Greenhouse board '{config.board_token}': {exc}"
                        ) from exc
                    wait_seconds = float(2**attempt)
                    logger.warning(
                        "Network error on board '%s': %s, waiting %.1fs (attempt %d/%d)",
                        config.board_token,
                        exc,
                        wait_seconds,
                        attempt,
                        config.max_retries,
                    )
                    await asyncio.sleep(wait_seconds)

            raise GreenhouseRateLimitError(
                f"Exhausted {config.max_retries} attempts fetching board '{config.board_token}'."
            )
        finally:
            if client_created:
                await client.aclose()
