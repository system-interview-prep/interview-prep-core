from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

MAX_CV_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_SUFFIXES = frozenset({".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".webp"})
_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class InvalidCvFile(ValueError):
    pass


class CvFileTooLarge(InvalidCvFile):
    pass


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


class MimeDetector(Protocol):
    def detect(self, content: bytes) -> str: ...


class SignatureMimeDetector:
    """Detects supported document types from bytes instead of trusting headers."""

    def detect(self, content: bytes) -> str:
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return "application/msword"
        if content.startswith(b"PK"):
            try:
                with ZipFile(BytesIO(content)) as archive:
                    names = set(archive.namelist())
                    if "[Content_Types].xml" in names and "word/document.xml" in names:
                        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            except BadZipFile:
                pass
        raise InvalidCvFile("File signature is not a supported CV document type")


class CvFileValidator:
    """Validates transport metadata and bounded content before persistence."""

    def __init__(self, mime_detector: MimeDetector | None = None) -> None:
        self._mime_detector = mime_detector or SignatureMimeDetector()

    def validate(self, filename: str, content_type: str | None, content: bytes) -> tuple[str, str]:
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in ALLOWED_SUFFIXES:
            raise InvalidCvFile("Only PDF, DOC, DOCX, PNG, JPEG, and WEBP files are allowed")
        if not content:
            raise InvalidCvFile("File is empty")
        if len(content) > MAX_CV_FILE_SIZE:
            raise CvFileTooLarge("File must not exceed 10 MB")
        detected_content_type = self._mime_detector.detect(content)
        if detected_content_type != _MIME_BY_SUFFIX[suffix]:
            raise InvalidCvFile("File content does not match its extension")
        if content_type and content_type not in {detected_content_type, "application/octet-stream"}:
            raise InvalidCvFile("File content does not match its declared content type")
        return safe_name, detected_content_type


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
        safe_name, detected_content_type = self._validator.validate(
            filename, content_type, content
        )
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
