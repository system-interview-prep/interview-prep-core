import json

from src.infrastructure.r2 import get_object, put_object


class R2ObjectStorage:
    """R2 adapter for the storage port used by the parsing pipeline."""

    def read(self, key: str) -> bytes:
        return get_object(key)

    def write_json(self, key: str, payload: dict) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        put_object(key, content, "application/json")
