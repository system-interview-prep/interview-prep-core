import pytest

from src.modules.user_cvs.application.review_service import CvReviewConflictError, CvReviewService
from src.modules.user_cvs.domain.models import CvRecord
from src.modules.user_cvs.domain.schemas import CanonicalResume, ParsingMetadata


def _resume(status="review_required"):
    return CanonicalResume(
        schemaVersion="2.1",
        resumeId="cv-1",
        documentId="cv-1",
        documentSha256="a" * 64,
        parsing=ParsingMetadata(
            parserVersion="test", extractionVersion="test", parsedAt="2026-01-01T00:00:00Z", status=status
        ),
    )


class Repository:
    def __init__(self):
        self.record = CvRecord(
            id="cv-1",
            user_id="user-1",
            checksum="a" * 64,
            filename="cv.pdf",
            content_type="application/pdf",
            size=10,
            storage_key="cv.pdf",
            url=None,
            status="DONE",
            score=None,
            error=None,
            parse_source="test",
            raw_text="text",
            parsed_data=_resume().model_dump(by_alias=True),
            created_at=None,
            updated_at=None,
        )
        self.saved = None
        self.queued = False
        self.dispatch_failure = None

    async def get_owned(self, user_id, cv_id):
        return self.record if (user_id, cv_id) == ("user-1", "cv-1") else None

    async def update_parsed_owned(self, user_id, cv_id, parsed):
        self.saved = parsed
        return True

    async def queue_reparse_owned(self, user_id, cv_id):
        self.queued = True
        return True

    async def mark_dispatch_failed(self, cv_id, error):
        self.dispatch_failure = (cv_id, error)


class Publisher:
    def __init__(self, error=None):
        self.ids, self.error = [], error

    def publish(self, cv_id):
        if self.error:
            raise self.error
        self.ids.append(cv_id)


async def test_review_approval_changes_canonical_status_to_ready() -> None:
    repository, publisher = Repository(), Publisher()
    parsed = await CvReviewService(repository, publisher).set_review_status("user-1", "cv-1", approved=True)
    assert parsed.parsing.status == "ready"
    assert repository.saved.parsing.status == "ready"


async def test_human_edit_returns_resume_to_review_required() -> None:
    repository = Repository()
    edited = _resume(status="ready")
    saved = await CvReviewService(repository, Publisher()).replace_parsed_data("user-1", "cv-1", edited)
    assert saved.parsing.status == "review_required"
    assert any(warning.code == "user_edited" for warning in saved.parsing.warnings)


async def test_review_rejects_cross_document_payload() -> None:
    bad = _resume().model_copy(update={"document_sha256": "b" * 64})
    with pytest.raises(CvReviewConflictError, match="checksum"):
        await CvReviewService(Repository(), Publisher()).replace_parsed_data("user-1", "cv-1", bad)


async def test_reparse_marks_dispatch_failure() -> None:
    repository = Repository()
    with pytest.raises(CvReviewConflictError, match="queue"):
        await CvReviewService(repository, Publisher(RuntimeError("offline"))).reparse("user-1", "cv-1")
    assert repository.queued is True
    assert repository.dispatch_failure == ("cv-1", "offline")
