"""R2 adapter for user-CV retrieval and cleanup."""

from src.infrastructure.r2 import delete_object, get_object


class R2CvContentStorage:
    def read(self, key: str) -> bytes:
        return get_object(key)

    def delete(self, key: str) -> None:
        delete_object(key)
