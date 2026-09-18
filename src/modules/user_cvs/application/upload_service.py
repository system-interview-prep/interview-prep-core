from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from src.modules.documents.facade import (
    ALLOWED_DOCUMENT_SUFFIXES,
    MAX_DOCUMENT_FILE_SIZE,
    DocumentFileTooLarge,
    DocumentFileValidator,
    InvalidDocumentFile,
)

MAX_CV_FILE_SIZE = MAX_DOCUMENT_FILE_SIZE
ALLOWED_SUFFIXES = ALLOWED_DOCUMENT_SUFFIXES


InvalidCvFile = InvalidDocumentFile
CvFileTooLarge = DocumentFileTooLarge


class DuplicateCvError(RuntimeError):
    pass


class CvStorageUnavailable(RuntimeError):
    pass


class CvPersistenceUnavailable(RuntimeError):
    pass


class CvQueueUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CvUploadRecord:
    cv_id: str
    user_id: str
    checksum: str
    filename: str
    content_type: str
    size: int
    storage_key: str
    url: str


@dataclass(frozen=True)
class CvUploadResult:
    cv_id: str
    created: bool


class CvUploadRepository(Protocol):
    async def find_id_by_checksum(self, user_id: str, checksum: str) -> str | None: ...

    async def create(self, record: CvUploadRecord) -> None: ...

    async def mark_dispatch_failed(self, cv_id: str, error: str) -> None: ...


class CvFileStorage(Protocol):
    def write(self, key: str, content: bytes, content_type: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def url(self, key: str, cv_id: str) -> str: ...


class CvParsePublisher(Protocol):
    def publish(self, cv_id: str) -> None: ...


class CvFileValidator(DocumentFileValidator):
    """Backwards-compatible CV name for the shared validator."""


class CvUploadService:
    """Coordinates an upload through narrow repository, storage and queue ports."""

    def __init__(
        self,
        *,
        repository: CvUploadRepository,
        storage: CvFileStorage,
        publisher: CvParsePublisher,
        validator: CvFileValidator | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._publisher = publisher
        self._validator = validator or CvFileValidator()

    async def upload(
        self,
        *,
        user_id: str,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> CvUploadResult:
        safe_name, detected_content_type = self._validator.validate(filename, content_type, content)
        checksum = sha256(content).hexdigest()
        existing_id = await self._repository.find_id_by_checksum(user_id, checksum)
        if existing_id:
            return CvUploadResult(cv_id=existing_id, created=False)

        cv_id = str(uuid4())
        suffix = Path(safe_name).suffix.lower()
        storage_key = f"cvs/{user_id}/{cv_id}{suffix}"
        try:
            self._storage.write(storage_key, content, detected_content_type)
        except Exception as exc:
            raise CvStorageUnavailable("CV object storage is unavailable") from exc
        record = CvUploadRecord(
            cv_id=cv_id,
            user_id=user_id,
            checksum=checksum,
            filename=safe_name,
            content_type=detected_content_type,
            size=len(content),
            storage_key=storage_key,
            url=self._storage.url(storage_key, cv_id),
        )
        try:
            await self._repository.create(record)
        except DuplicateCvError:
            self._storage.delete(storage_key)
            raise
        except Exception as exc:
            self._storage.delete(storage_key)
            raise CvPersistenceUnavailable("CV metadata persistence is unavailable") from exc
        try:
            self._publisher.publish(cv_id)
        except Exception as exc:
            await self._repository.mark_dispatch_failed(cv_id, str(exc)[:1000])
            raise CvQueueUnavailable("CV parse queue is unavailable") from exc
        return CvUploadResult(cv_id=cv_id, created=True)
