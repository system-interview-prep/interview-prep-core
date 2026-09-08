"""Application services for CV queries and lifecycle actions."""

from typing import Protocol

from src.modules.user_cvs.domain.models import CvRecord


class CvNotFoundError(Exception):
    pass


class CvContentUnavailableError(Exception):
    pass


class CvContentStorage(Protocol):
    def read(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


class CvRepository(Protocol):
    async def get_owned(self, user_id: str, cv_id: str) -> CvRecord | None: ...

    async def list_owned(
        self, user_id: str, limit: int, career_code: str | None = None
    ) -> list[CvRecord]: ...

    async def delete_owned(self, user_id: str, cv_id: str) -> CvRecord | None: ...


class CvQueryService:
    def __init__(self, repository: CvRepository, storage: CvContentStorage) -> None:
        self._repository = repository
        self._storage = storage

    async def get(self, user_id: str, cv_id: str) -> CvRecord:
        record = await self._repository.get_owned(user_id, cv_id)
        if record is None:
            raise CvNotFoundError
        return record

    async def list(
        self, user_id: str, limit: int, career_code: str | None = None
    ) -> list[CvRecord]:
        return await self._repository.list_owned(user_id, limit, career_code)

    async def download(self, user_id: str, cv_id: str) -> tuple[CvRecord, bytes]:
        record = await self.get(user_id, cv_id)
        try:
            return record, self._storage.read(record.storage_key)
        except Exception as exc:
            raise CvContentUnavailableError from exc

    async def delete(self, user_id: str, cv_id: str) -> None:
        record = await self._repository.delete_owned(user_id, cv_id)
        if record is None:
            raise CvNotFoundError
        try:
            self._storage.delete(record.storage_key)
        except Exception:
            # Metadata deletion is authoritative; storage cleanup is best effort.
            pass
