"""Embedding utilities for Interview Documents (env-based, provider-agnostic).

Responsibilities:
- Read Interview Document from file path / raw text / dict
- Normalize + chunk content
- Generate embedding vectors through OpenAI
- Return payloads ready for vector insertion
"""

from __future__ import annotations

import json
import logging
import os
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.config import Settings

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_get(d: Dict[str, Any], path: List[str], default: Any = None) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join([_to_text(x) for x in value if _to_text(x)])
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            rendered = _to_text(v)
            if rendered:
                lines.append(f"{k}: {rendered}")
        return "\n".join(lines)
    return str(value)


class EmbeddingAdapter:
    """Base adapter interface for embedding providers."""

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError



class OpenAIEmbeddingAdapter(EmbeddingAdapter):
    """OpenAI embeddings adapter.
    Env:
    - OPENAI_API_KEY
    - EMBEDDING_MODEL (default: text-embedding-3-small)
    """

    _CACHE_SCHEMA_VERSION = 1

    def __init__(
        self,
        model_name: str,
        dimensions: int,
        *,
        api_key: str | None = None,
        cache_dir: Path | None = None,
    ):
        from openai import OpenAI  # pip install openai>=1.x
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required.")
        self._client = OpenAI(api_key=api_key)
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
            payload = json.loads(path.read_text(encoding="utf-8"))
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
            "provider": "openai",
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

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
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
    settings = Settings()
    provider = settings.embedding_provider.strip().lower()
    model = settings.embedding_model.strip()
    dimensions = settings.embedding_dimension

    if provider == "openai":
        configured_cache_dir = Path(settings.embedding_cache_dir)
        cache_dir = (
            configured_cache_dir
            if configured_cache_dir.is_absolute()
            else Path(__file__).resolve().parents[4] / configured_cache_dir
        )
        if not settings.embedding_cache_enabled:
            cache_dir = None
        return OpenAIEmbeddingAdapter(
            model_name=model,
            dimensions=dimensions,
            api_key=settings.openai_api_key,
            cache_dir=cache_dir,
        )

    raise ValueError(
        f"Invalid EMBEDDING_PROVIDER: '{provider}'. Only 'openai' is supported."
    )


@dataclass
class ChunkRecord:
    chunk_id: str
    text: str
    metadata: Dict[str, Any]


def _load_document_from_file(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        raw = f.read()

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    import yaml
    obj = yaml.safe_load(raw)
    if isinstance(obj, dict):
        return obj
    raise ValueError("Unsupported document format. Provide valid JSON/YAML content.")


def _build_chunks_from_interview_document(doc: Dict[str, Any]) -> List[ChunkRecord]:
    document_id = _safe_get(doc, ["document", "document_id"], "unknown_document")
    version = _safe_get(doc, ["document", "version"], "1.0.0")

    topic_name = _safe_get(doc, ["topic", "topic_name"], "unknown-topic")
    domain = _safe_get(doc, ["topic", "domain"], "general")
    difficulty = _safe_get(doc, ["difficulty", "level"], "intermediate")
    roles = _ensure_list(_safe_get(doc, ["topic", "role_targets"], []))
    job_levels = _ensure_list(_safe_get(doc, ["topic", "job_levels"], []))
    language = _safe_get(doc, ["document", "language"], "vi")

    common_meta = {
        "knowledge_unit_id": document_id,
        "version": version,
        "topic": topic_name,
        "domain": domain,
        "difficulty": difficulty,
        "roles": roles,
        "job_levels": job_levels,
        "language": language,
        "is_active": _safe_get(doc, ["metadata", "retrieval", "is_active"], True),
        "quality_score": _safe_get(doc, ["metadata", "retrieval", "quality_score"], 0.8),
        "updated_at": _safe_get(doc, ["document", "updated_at"], _utc_now_iso()),
    }

    chunks: List[ChunkRecord] = []
    chunk_specs = [
        ("knowledge", _safe_get(doc, ["knowledge"], {})),
        ("expected_points", _safe_get(doc, ["expected_points"], {})),
        ("common_mistakes", _safe_get(doc, ["common_mistakes"], {})),
        ("follow_up", _safe_get(doc, ["follow_up"], {})),
        ("reference_source", _safe_get(doc, ["reference_source"], {})),
        ("deliverables", _safe_get(doc, ["deliverables"], {})),
    ]

    idx = 1
    for chunk_type, payload in chunk_specs:
        text = _to_text(payload)
        if not text:
            continue
        chunk_id = f"{document_id}_chunk_{idx:02d}"
        meta = dict(common_meta)
        meta.update(
            {
                "chunk_id": chunk_id,
                "chunk_type": chunk_type,
                "source": "interview_document",
            }
        )
        chunks.append(ChunkRecord(chunk_id=chunk_id, text=text, metadata=meta))
        idx += 1

    if not chunks:
        raise ValueError("No chunkable content found in Interview Document.")
    return chunks


def read_interview_document(
    *,
    file_path: Optional[str] = None,
    raw_text: Optional[str] = None,
    document_obj: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    provided = [x is not None for x in (file_path, raw_text, document_obj)]
    if sum(provided) != 1:
        raise ValueError("Provide exactly one of: file_path, raw_text, document_obj")

    if file_path:
        return _load_document_from_file(file_path)

    if raw_text is not None:
        try:
            obj = json.loads(raw_text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        import yaml
        obj = yaml.safe_load(raw_text)
        if isinstance(obj, dict):
            return obj
        raise ValueError("raw_text is not valid JSON/YAML object")

    return document_obj or {}


def generate_embedding_payloads(
    *,
    file_path: Optional[str] = None,
    raw_text: Optional[str] = None,
    document_obj: Optional[Dict[str, Any]] = None,
    adapter: Optional[EmbeddingAdapter] = None,
) -> List[Dict[str, Any]]:
    doc = read_interview_document(file_path=file_path, raw_text=raw_text, document_obj=document_obj)
    chunks = _build_chunks_from_interview_document(doc)

    embedder = adapter or build_embedding_adapter_from_env()
    vectors = embedder.embed_texts([c.text for c in chunks])

    if len(vectors) != len(chunks):
        raise RuntimeError("Embedding output size mismatch")

    records: List[Dict[str, Any]] = []
    for chunk, vec in zip(chunks, vectors):
        records.append(
            {
                "id": chunk.chunk_id,
                "vector": [float(x) for x in vec],
                "text": chunk.text,
                "metadata": chunk.metadata,
            }
        )
    return records

