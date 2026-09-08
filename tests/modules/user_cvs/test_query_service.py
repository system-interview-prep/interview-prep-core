from datetime import datetime, timezone

import pytest

from src.modules.user_cvs.application.query_service import CvContentUnavailableError, CvNotFoundError, CvQueryService
from src.modules.user_cvs.domain.models import CvRecord


def _record() -> CvRecord:
    now = datetime.now(timezone.utc)
    return CvRecord(
        id="cv-1", user_id="user-1", filename="resume.pdf", content_type="application/pdf",
        size=42, storage_key="cvs/user-1/cv-1.pdf", url=None, status="DONE", score=None,
        error=None, parse_source="mineru", raw_text="text", parsed_data={"skills": []},
        created_at=now, updated_at=now,
    )


class FakeRepository:
    def __init__(self, record: CvRecord | None) -> None:
        self.record = record
        self.deleted = None
        self.list_request = None

    async def get_owned(self, user_id: str, cv_id: str) -> CvRecord | None:
        return self.record if self.record and (user_id, cv_id) == ("user-1", "cv-1") else None

    async def list_owned(
        self, user_id: str, limit: int, career_code: str | None = None
    ) -> list[CvRecord]:
        self.list_request = (user_id, limit, career_code)
        return [self.record] if self.record and user_id == "user-1" and limit else []

    async def delete_owned(self, user_id: str, cv_id: str) -> CvRecord | None:
        self.deleted = (user_id, cv_id)
        return await self.get_owned(user_id, cv_id)


class FakeStorage:
    def __init__(self, content: bytes = b"pdf", fail_read: bool = False) -> None:
        self.content = content
        self.fail_read = fail_read
        self.deleted: list[str] = []

    def read(self, key: str) -> bytes:
        if self.fail_read:
            raise OSError("unavailable")
        return self.content

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def test_query_service_keeps_transport_data_outside_service_boundary() -> None:
    storage = FakeStorage()
    service = CvQueryService(FakeRepository(_record()), storage)

    record, content = await service.download("user-1", "cv-1")

    assert content == b"pdf"
    assert record.as_response()["parsedData"] == {"skills": []}
    await service.delete("user-1", "cv-1")
    assert storage.deleted == ["cvs/user-1/cv-1.pdf"]


async def test_query_service_passes_career_code_to_repository_filter() -> None:
    repository = FakeRepository(_record())
    service = CvQueryService(repository, FakeStorage())

    records = await service.list("user-1", 50, "technology.software-engineering.backend")

    assert records == [repository.record]
    assert repository.list_request == ("user-1", 50, "technology.software-engineering.backend")


async def test_query_service_exposes_domain_errors_not_http_errors() -> None:
    missing = CvQueryService(FakeRepository(None), FakeStorage())
    with pytest.raises(CvNotFoundError):
        await missing.get("user-1", "cv-1")

    unavailable = CvQueryService(FakeRepository(_record()), FakeStorage(fail_read=True))
    with pytest.raises(CvContentUnavailableError):
        await unavailable.download("user-1", "cv-1")
