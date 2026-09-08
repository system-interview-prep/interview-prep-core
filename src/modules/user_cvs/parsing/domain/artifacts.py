from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DocumentArtifacts:
    """Provider-neutral document extraction result consumed by CV parsing."""

    markdown: str
    content_list: list[Any] = field(default_factory=list)
    middle: dict[str, Any] | list[Any] | None = None
    extractor_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "contentList": self.content_list,
            "middle": self.middle,
            "extractorVersion": self.extractor_version,
        }
