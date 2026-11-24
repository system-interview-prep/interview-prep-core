"""Domain models and data structures for external job ingestion."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class ExternalJobCandidate(BaseModel):
    """Raw-to-normalized job payload extracted from an external provider (e.g. Greenhouse)."""

    external_job_id: str
    title: str
    company_name: str
    company_logo_url: str | None = None
    location: str | None = None
    raw_html: str = ""
    source_type: str = "greenhouse"
    source_key: str
    source_name: str | None = None
    source_url: str | None = None
    apply_url: str | None = None
    posted_at: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestionConfig(BaseModel):
    """Configuration for ingesting a specific company's external job board."""

    board_token: str
    company_name: str
    company_logo_url: str | None = None
    source_type: str = "greenhouse"
    timeout_seconds: float = 15.0
    max_retries: int = 3
    grace_period_hours: float = 24.0


@dataclass
class IngestionMetrics:
    total_fetched: int = 0
    created_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    closed_count: int = 0
    failed_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total_fetched": self.total_fetched,
            "created_count": self.created_count,
            "updated_count": self.updated_count,
            "unchanged_count": self.unchanged_count,
            "closed_count": self.closed_count,
            "failed_count": self.failed_count,
        }


class IngestionSummary(BaseModel):
    """Execution report and audit telemetry for an ingestion run."""

    source_type: str
    source_key: str
    board_token: str
    status: Literal["COMPLETED", "FAILED", "PARTIAL"]
    metrics: dict[str, int]
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    errors: list[str] = Field(default_factory=list)
