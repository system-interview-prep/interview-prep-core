from typing import Protocol

from src.modules.user_cvs.domain.models import CvRecord
from src.modules.user_cvs.domain.schemas import CanonicalResume, ParserWarning


class CvReviewNotFoundError(LookupError):
    pass


class CvReviewConflictError(RuntimeError):
    pass


class CvReviewRepository(Protocol):
    async def get_owned(self, user_id: str, cv_id: str) -> CvRecord | None: ...

    async def update_parsed_owned(self, user_id: str, cv_id: str, parsed: CanonicalResume) -> bool: ...

    async def queue_reparse_owned(self, user_id: str, cv_id: str) -> bool: ...

    async def mark_dispatch_failed(self, cv_id: str, error: str) -> None: ...


class CvReviewPublisher(Protocol):
    def publish(self, cv_id: str) -> None: ...


class CvReviewService:
    def __init__(self, repository: CvReviewRepository, publisher: CvReviewPublisher) -> None:
        self._repository = repository
        self._publisher = publisher

    async def replace_parsed_data(self, user_id: str, cv_id: str, parsed: CanonicalResume) -> CanonicalResume:
        record = await self._record(user_id, cv_id)
        if record.status != "DONE" or record.parsed_data is None:
            raise CvReviewConflictError("CV has no completed parse to review")
        if parsed.resume_id != cv_id or parsed.document_id != cv_id:
            raise CvReviewConflictError("Parsed resume identifiers must match the CV")
        if parsed.document_sha256 != record.checksum:
            raise CvReviewConflictError("Parsed resume checksum must match the uploaded document")
        if parsed.parsing is None:
            raise CvReviewConflictError("Parsed resume must include parsing metadata")
        warning = ParserWarning(
            code="user_edited",
            severity="info",
            message="Canonical resume was edited during human review.",
        )
        parsed = parsed.model_copy(
            update={
                "parsing": parsed.parsing.model_copy(
                    update={
                        "status": "review_required",
                        "warnings": [*parsed.parsing.warnings, warning],
                    }
                )
            }
        )
        if not await self._repository.update_parsed_owned(user_id, cv_id, parsed):
            raise CvReviewNotFoundError
        return parsed

    async def set_review_status(self, user_id: str, cv_id: str, *, approved: bool) -> CanonicalResume:
        record = await self._record(user_id, cv_id)
        if record.status != "DONE" or not record.parsed_data:
            raise CvReviewConflictError("CV has no completed parse to review")
        parsed = CanonicalResume.model_validate(record.parsed_data)
        if parsed.parsing is None:
            raise CvReviewConflictError("Parsed resume must include parsing metadata")
        parsed = parsed.model_copy(
            update={
                "parsing": parsed.parsing.model_copy(
                    update={"status": "ready" if approved else "review_required"}
                )
            }
        )
        if not await self._repository.update_parsed_owned(user_id, cv_id, parsed):
            raise CvReviewNotFoundError
        return parsed

    async def reparse(self, user_id: str, cv_id: str) -> None:
        if not await self._repository.queue_reparse_owned(user_id, cv_id):
            raise CvReviewNotFoundError
        try:
            self._publisher.publish(cv_id)
        except Exception as exc:
            await self._repository.mark_dispatch_failed(cv_id, str(exc)[:1000])
            raise CvReviewConflictError("CV parsing queue is unavailable") from exc

    async def _record(self, user_id: str, cv_id: str) -> CvRecord:
        record = await self._repository.get_owned(user_id, cv_id)
        if record is None:
            raise CvReviewNotFoundError
        return record
