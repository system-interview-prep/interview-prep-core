import pytest

from src.modules.user_cvs.application.upload_service import (
    MAX_CV_FILE_SIZE,
    CvFileTooLarge,
    CvQueueUnavailable,
    CvStorageUnavailable,
    CvUploadService,
    InvalidCvFile,
)


class MemoryUploadRepository:
    def __init__(self, existing_id: str | None = None) -> None:
        self.existing_id = existing_id
        self.created = None
        self.dispatch_failure = None

    async def find_id_by_checksum(self, user_id: str, checksum: str) -> str | None:
        return self.existing_id

    async def create(self, record) -> None:
        self.created = record

    async def mark_dispatch_failed(self, cv_id: str, error: str) -> None:
        self.dispatch_failure = (cv_id, error)


class MemoryFileStorage:
    def __init__(self) -> None:
        self.objects = {}
        self.deleted = []

    def write(self, key: str, content: bytes, content_type: str) -> None:
        self.objects[key] = (content, content_type)

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def url(self, key: str, cv_id: str) -> str:
        return f"https://storage.test/{key}"


class FailingFileStorage(MemoryFileStorage):
    def write(self, key: str, content: bytes, content_type: str) -> None:
        raise RuntimeError("bucket not found")


class RecordingPublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.cv_ids = []
        self.error = error

    def publish(self, cv_id: str) -> None:
        if self.error:
            raise self.error
        self.cv_ids.append(cv_id)


async def test_upload_service_persists_file_metadata_then_dispatches_parse() -> None:
    repository = MemoryUploadRepository()
    storage = MemoryFileStorage()
    publisher = RecordingPublisher()
    service = CvUploadService(repository=repository, storage=storage, publisher=publisher)

    result = await service.upload(
        user_id="user-1",
        filename="../resume.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.7",
    )

    assert result.created is True
    assert repository.created.filename == "resume.pdf"
    assert repository.created.storage_key in storage.objects
    assert publisher.cv_ids == [result.cv_id]


async def test_upload_service_returns_existing_cv_without_writes_or_dispatch() -> None:
    repository = MemoryUploadRepository(existing_id="cv-existing")
    storage = MemoryFileStorage()
    publisher = RecordingPublisher()
    service = CvUploadService(repository=repository, storage=storage, publisher=publisher)

    result = await service.upload(
        user_id="user-1",
        filename="resume.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.7",
    )

    assert result.cv_id == "cv-existing"
    assert result.created is False
    assert storage.objects == {}
    assert publisher.cv_ids == []


async def test_upload_service_classifies_object_storage_failure() -> None:
    service = CvUploadService(
        repository=MemoryUploadRepository(),
        storage=FailingFileStorage(),
        publisher=RecordingPublisher(),
    )

    with pytest.raises(CvStorageUnavailable, match="CV object storage is unavailable"):
        await service.upload(
            user_id="user-1",
            filename="resume.pdf",
            content_type="application/pdf",
            content=b"%PDF-1.7",
        )


@pytest.mark.parametrize(
    ("filename", "content_type", "content", "error_type"),
    [
        ("resume.exe", "application/octet-stream", b"data", InvalidCvFile),
        ("resume.pdf", "application/pdf", b"", InvalidCvFile),
        ("resume.pdf", "application/pdf", b"x" * (MAX_CV_FILE_SIZE + 1), CvFileTooLarge),
    ],
    ids=["unsupported-type", "empty-file", "file-too-large"],
)
async def test_upload_service_rejects_invalid_input_before_storage(
    filename: str, content_type: str, content: bytes, error_type: type[Exception]
) -> None:
    storage = MemoryFileStorage()
    service = CvUploadService(
        repository=MemoryUploadRepository(), storage=storage, publisher=RecordingPublisher()
    )

    with pytest.raises(error_type):
        await service.upload(
            user_id="user-1",
            filename=filename,
            content_type=content_type,
            content=content,
        )
    assert storage.objects == {}


async def test_upload_service_marks_record_failed_when_queue_dispatch_fails() -> None:
    repository = MemoryUploadRepository()
    storage = MemoryFileStorage()
    service = CvUploadService(
        repository=repository,
        storage=storage,
        publisher=RecordingPublisher(RuntimeError("broker unavailable")),
    )

    with pytest.raises(CvQueueUnavailable, match="CV parse queue is unavailable"):
        await service.upload(
            user_id="user-1",
            filename="resume.pdf",
            content_type="application/pdf",
            content=b"%PDF-1.7",
        )

    assert repository.dispatch_failure == (repository.created.cv_id, "broker unavailable")
