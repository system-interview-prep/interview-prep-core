"""Domain and API view models owned by the user-CV module."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class CvRecord:
    id: str
    user_id: str
    filename: str
    content_type: str
    size: int
    storage_key: str
    url: str | None
    status: str
    score: float | None
    error: str | None
    parse_source: str | None
    raw_text: str | None
    parsed_data: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CvRecord":
        return cls(
            id=str(row["id"]), user_id=str(row["user_id"]), filename=str(row["filename"]),
            content_type=str(row["content_type"]), size=int(row["size"]),
            storage_key=str(row["storage_key"]), url=row["url"], status=str(row["status"]),
            score=row["score"], error=row["error"], parse_source=row["parse_source"],
            raw_text=row["raw_text"], parsed_data=row.get("parsed_data"),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def as_response(self) -> dict[str, Any]:
        return {
            "id": self.id, "userId": self.user_id, "originalName": self.filename,
            "filename": self.filename, "contentType": self.content_type, "size": self.size,
            "storageKey": self.storage_key, "url": self.url, "status": self.status,
            "score": self.score, "error": self.error, "parseSource": self.parse_source,
            "rawText": self.raw_text, "parsedData": self.parsed_data,
            "createdAt": self.created_at.isoformat(), "updatedAt": self.updated_at.isoformat(),
        }
