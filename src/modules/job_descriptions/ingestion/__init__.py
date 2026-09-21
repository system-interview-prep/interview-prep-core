"""External Job Ingestion package."""

from src.modules.job_descriptions.ingestion.adapters.greenhouse import (
    GreenhouseApiError,
    GreenhouseBoardNotFoundError,
    GreenhouseJobBoardAdapter,
    GreenhouseRateLimitError,
)
from src.modules.job_descriptions.ingestion.models import (
    ExternalJobCandidate,
    IngestionConfig,
    IngestionMetrics,
    IngestionSummary,
)
from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository
from src.modules.job_descriptions.ingestion.sanitizer import HtmlSanitizer
from src.modules.job_descriptions.ingestion.service import JobIngestionService

__all__ = [
    "ExternalJobCandidate",
    "GreenhouseApiError",
    "GreenhouseBoardNotFoundError",
    "GreenhouseJobBoardAdapter",
    "GreenhouseRateLimitError",
    "HtmlSanitizer",
    "IngestionConfig",
    "IngestionMetrics",
    "IngestionSummary",
    "JobIngestionRepository",
    "JobIngestionService",
]
