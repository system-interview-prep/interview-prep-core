"""Embedding-provider adapter shared by the matching module."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from src.core.config import Settings

logger = logging.getLogger(__name__)


class EmbeddingAdapter:
    """Interface for embedding text used by semantic matching."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OpenAIEmbeddingAdapter(EmbeddingAdapter):
    """OpenAI embedding adapter with an optional local immutable cache."""

    _CACHE_SCHEMA_VERSION = 1

    def __init__(
        self,
        model_name: str,
        dimensions: int,
        *,
        api_key: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        from openai import OpenAI

        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("OPENAI_API_KEY environment variable is required.")
        self._client = OpenAI(api_key=resolved_key)
        self._model = model_name
        self._dimensions = dimensions
        self._cache_dir = cache_dir

    def _cache_path(self, text: str) -> Path | None:
        if self._cache_dir is None:
            return None
        identity = json.dumps(
            {
                "schema_version": self._CACHE_SCHEMA_VERSION,
                "provider": "openai",
                "model": self._model,
                "dimensions": self._dimensions,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return self._cache_dir / f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.json"

    def _read_cached_vector(self, text: str) -> list[float] | None:
        path = self._cache_path(text)
        if path is None or not path.is_file():
            return None
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            vector = payload["vector"]
            if (
                payload["schema_version"] != self._CACHE_SCHEMA_VERSION
                or payload["model"] != self._model
                or payload["dimensions"] != self._dimensions
                or payload["text_sha256"] != hashlib.sha256(text.encode("utf-8")).hexdigest()
                or not isinstance(vector, list)
                or len(vector) != self._dimensions
            ):
                return None
            return [float(value) for value in vector]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_cached_vector(self, text: str, vector: list[float]) -> None:
        path = self._cache_path(text)
        if path is None or len(vector) != self._dimensions:
            return
        payload = {
            "schema_version": self._CACHE_SCHEMA_VERSION,
            "model": self._model,
            "dimensions": self._dimensions,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "vector": vector,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(".tmp")
            temporary_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary_path, path)
        except OSError:
            logger.warning("Could not persist embedding cache at %s", path)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float] | None] = [self._read_cached_vector(text) for text in texts]
        missing_positions = [index for index, vector in enumerate(vectors) if vector is None]
        if not missing_positions:
            return [vector for vector in vectors if vector is not None]

        missing_texts = [texts[index] for index in missing_positions]
        request: dict[str, Any] = {"model": self._model, "input": missing_texts}
        if self._model.startswith("text-embedding-3"):
            request["dimensions"] = self._dimensions
        response = self._client.embeddings.create(**request)
        generated = [list(item.embedding) for item in sorted(response.data, key=lambda item: item.index)]
        if len(generated) != len(missing_positions):
            raise RuntimeError("Embedding provider returned an invalid vector count")
        for index, text, vector in zip(missing_positions, missing_texts, generated, strict=True):
            vectors[index] = vector
            self._write_cached_vector(text, vector)
        return [vector for vector in vectors if vector is not None]


def build_embedding_adapter_from_env() -> EmbeddingAdapter:
    """Build the configured embedding adapter for matching."""
    settings = Settings()
    if settings.embedding_provider.strip().lower() != "openai":
        raise ValueError(
            f"Invalid EMBEDDING_PROVIDER: '{settings.embedding_provider}'. Only 'openai' is supported."
        )
    configured_cache_dir = Path(settings.embedding_cache_dir)
    cache_dir = configured_cache_dir if configured_cache_dir.is_absolute() else Path(__file__).resolve().parents[4] / configured_cache_dir
    if not settings.embedding_cache_enabled:
        cache_dir = None
    return OpenAIEmbeddingAdapter(
        model_name=settings.embedding_model.strip(),
        dimensions=settings.embedding_dimension,
        api_key=settings.openai_api_key,
        cache_dir=cache_dir,
    )
