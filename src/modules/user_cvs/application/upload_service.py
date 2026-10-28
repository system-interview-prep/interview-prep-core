import time
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
from src.modules.user_cvs.application import run_tracking

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
    # Set when the candidate already has a CV with this name (different file):
    # the new CV is stored as "<name> (2).pdf" and this keeps the name they sent.
    renamed_from: str | None = None


def unique_filename(name: str, taken: set[str]) -> str:
    """``name`` or ``"<stem> (n)<suffix>"`` with the lowest free n >= 2 (case-insensitive)."""
    used = {item.casefold() for item in taken}
    if name.casefold() not in used:
        return name
    path = Path(name)
    stem, suffix = path.stem, path.suffix
    index = 2
    while f"{stem} ({index}){suffix}".casefold() in used:
        index += 1
    return f"{stem} ({index}){suffix}"


class CvUploadRepository(Protocol):
    async def find_id_by_checksum(self, user_id: str, checksum: str) -> str | None: ...

    async def filenames(self, user_id: str) -> set[str]: ...

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
        started = time.perf_counter()
        safe_name, detected_content_type = self._validator.validate(filename, content_type, content)
        checksum = sha256(content).hexdigest()
        existing_id = await self._repository.find_id_by_checksum(user_id, checksum)
        if existing_id:
            # Same file again: no new CV, one event on the existing one.
            with run_tracking.cv_run(existing_id, user_id=user_id):
                run_tracking.emit(
                    "cv_upload_duplicate",
                    workflow=run_tracking.UPLOAD_WORKFLOW,
                    **self._file_fields(safe_name, detected_content_type, content),
                    duplicate=True,
                )
            return CvUploadResult(cv_id=existing_id, created=False)

        original_name = safe_name
        try:
            safe_name = unique_filename(safe_name, await self._repository.filenames(user_id))
        except Exception:  # noqa: BLE001 - a name lookup failure must not block the upload
            safe_name = original_name
        renamed_from = original_name if safe_name != original_name else None

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
        with run_tracking.cv_run(cv_id, user_id=user_id):
            run_tracking.emit(
                "cv_uploaded",
                workflow=run_tracking.UPLOAD_WORKFLOW,
                **self._file_fields(safe_name, detected_content_type, content),
                duplicate=False,
                duration_ms=run_tracking.elapsed_ms(started),
            )
            try:
                self._publisher.publish(cv_id)
            except Exception as exc:
                await self._repository.mark_dispatch_failed(cv_id, str(exc)[:1000])
                run_tracking.emit(
                    "cv_dispatch_failed",
                    workflow=run_tracking.UPLOAD_WORKFLOW,
                    error_type=type(exc).__name__,
                    reason="dispatch_failed",
                )
                raise CvQueueUnavailable("CV parse queue is unavailable") from exc
            run_tracking.emit("cv_enqueued", workflow=run_tracking.UPLOAD_WORKFLOW, reparse=False)
        return CvUploadResult(cv_id=cv_id, created=True, renamed_from=renamed_from)

    @staticmethod
    def _file_fields(filename: str, content_type: str, content: bytes) -> dict:
        """Upload event fields: the extension only, never the (identifying) file name."""
        return {
            "extension": run_tracking.file_extension(filename),
            "content_type": content_type,
            "size_bytes": len(content),
        }
