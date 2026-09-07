"""Insert/update/search Interview Document embeddings into vector store (PostgreSQL pgvector or PyVector).

This module provides:
- insert_vectors(records)
- upsert_vectors(records)
- upsert_interview_document(...)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import psycopg2
from pgvector.psycopg2 import register_vector
from src.modules.matching.rag.embedding import generate_embedding_payloads

logger = logging.getLogger(__name__)


def _vector_to_list(val: Any) -> Optional[List[float]]:
    if val is None:
        return None
    if hasattr(val, "tolist"):
        return [float(x) for x in val.tolist()]
    if hasattr(val, "to_list"):
        return [float(x) for x in val.to_list()]
    try:
        return [float(x) for x in val]
    except (TypeError, ValueError):
        return None


class VectorStoreAdapter:
    """Safe env-based adapter for vector insertion/upsert and search on PostgreSQL or PyVector."""

    def __init__(self):
        self.provider = os.getenv("VECTOR_DB_PROVIDER", "postgresql").strip().lower()
        self.collection = os.getenv("PYVECTOR_COLLECTION", "interview_knowledge_vectors")
        self.db_url = os.getenv("DATABASE_URL", "")

        if self.provider == "mock":
            raise ValueError(
                "Mock Vector Database is deprecated and disabled. "
                "Please configure VECTOR_DB_PROVIDER=postgresql or pyvector."
            )

        self._client = None
        if self.provider == "pyvector":
            try:
                from pyvector import Client  # type: ignore
            except Exception as exc:
                raise RuntimeError(
                    "VECTOR_DB_PROVIDER=pyvector but pyvector package/client is unavailable."
                ) from exc

            self._client = Client(
                base_url=os.getenv("PYVECTOR_URL"),
                api_key=os.getenv("PYVECTOR_API_KEY"),
            )
        elif self.provider in {"postgresql", "postgres"}:
            if not self.db_url:
                raise ValueError("DATABASE_URL environment variable is required for VECTOR_DB_PROVIDER=postgresql.")

    def _get_db_connection(self):
        conn = psycopg2.connect(self.db_url)
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        conn.commit()
        register_vector(conn)
        return conn

    def init_rag_table(self):
        if self.provider not in {"postgresql", "postgres"}:
            return
        conn = self._get_db_connection()
        dim = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS rag_knowledge_chunks (
                    chunk_id VARCHAR(255) PRIMARY KEY,
                    document_id VARCHAR(255),
                    text TEXT,
                    vector vector({dim}),
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
               );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_rag_document_id ON rag_knowledge_chunks(document_id);
            """)
        conn.commit()
        conn.close()

    def insert(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.upsert(records)

    def upsert(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        if self.provider in {"postgresql", "postgres"}:
            self.init_rag_table()
            conn = self._get_db_connection()
            with conn.cursor() as cur:
                for r in records:
                    chunk_id = r["id"]
                    text = r["text"]
                    vector = r["vector"]
                    metadata = r["metadata"]
                    doc_id = metadata.get("knowledge_unit_id")
                    
                    cur.execute("""
                        INSERT INTO rag_knowledge_chunks (chunk_id, document_id, text, vector, metadata)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            document_id = EXCLUDED.document_id,
                            text = EXCLUDED.text,
                            vector = EXCLUDED.vector,
                            metadata = EXCLUDED.metadata;
                    """, (chunk_id, doc_id, text, vector, json.dumps(metadata)))
            conn.commit()
            conn.close()
            return {"success": True, "upserted": len(records), "provider": "postgresql"}

        # PyVector provider
        self._client.upsert(
            collection=self.collection,
            records=records,
        )
        return {"success": True, "upserted": len(records), "provider": "pyvector"}

    def search(
        self,
        query_vector: List[float],
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search similar vectors with metadata filtering."""
        if self.provider in {"postgresql", "postgres"}:
            self.init_rag_table()
            conn = self._get_db_connection()
            results = []
            
            where_clauses = []
            params = []
            
            if filters:
                for k, v in filters.items():
                    if isinstance(v, bool):
                        where_clauses.append(f"(metadata->>'{k}')::boolean = %s")
                        params.append(v)
                    elif isinstance(v, (int, float)):
                        where_clauses.append(f"(metadata->>'{k}')::numeric = %s")
                        params.append(v)
                    else:
                        where_clauses.append(f"(metadata->>'{k}' = %s OR metadata->'{k}' @> %s)")
                        params.append(str(v))
                        params.append(json.dumps(v))
            
            where_sql = ""
            if where_clauses:
                where_sql = "WHERE " + " AND ".join(where_clauses)
            
            sql = f"""
                SELECT chunk_id, text, metadata, vector, (1 - (vector <=> %s::vector)) as similarity
                FROM rag_knowledge_chunks
                {where_sql}
                ORDER BY vector <=> %s::vector
                LIMIT %s;
            """
            
            all_params = [query_vector] + params + [query_vector, limit]
            
            with conn.cursor() as cur:
                cur.execute(sql, all_params)
                rows = cur.fetchall()
                for row in rows:
                    results.append({
                        "id": row[0],
                        "text": row[1],
                        "metadata": row[2] if isinstance(row[2], dict) else json.loads(row[2] or '{}'),
                        "vector": _vector_to_list(row[3]),
                        "score": float(row[4]) if row[4] is not None else 0.0
                    })
            conn.close()
            return results

        # PyVector API search
        try:
            return self._client.search(
                collection=self.collection,
                vector=query_vector,
                filter=filters,
                limit=limit,
            )
        except AttributeError:
            return self._client.query(
                collection=self.collection,
                vector=query_vector,
                filter=filters,
                limit=limit,
            )

    def get_all_documents(self) -> List[Dict[str, Any]]:
        """Group chunks by knowledge_unit_id and return a summary of each document."""
        if self.provider in {"postgresql", "postgres"}:
            self.init_rag_table()
            conn = self._get_db_connection()
            docs = []
            
            sql = """
                SELECT DISTINCT ON (document_id)
                    document_id,
                    metadata->>'topic',
                    metadata->>'domain',
                    metadata->>'difficulty',
                    metadata->>'language',
                    (metadata->>'is_active')::boolean,
                    (metadata->>'quality_score')::numeric,
                    metadata->>'updated_at',
                    COUNT(*) OVER (PARTITION BY document_id) as chunk_count
                FROM rag_knowledge_chunks
                ORDER BY document_id, metadata->>'updated_at' DESC;
            """
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                for row in rows:
                    docs.append({
                        "document_id": row[0],
                        "topic": row[1] or "unknown",
                        "domain": row[2] or "general",
                        "difficulty": row[3] or "intermediate",
                        "language": row[4] or "vi",
                        "is_active": row[5] if row[5] is not None else True,
                        "quality_score": float(row[6]) if row[6] is not None else 0.8,
                        "updated_at": row[7] or "",
                        "chunk_count": int(row[8])
                    })
            conn.close()
            docs.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
            return docs
        
        logger.warning("get_all_documents is only fully implemented for postgresql provider")
        return []

    def get_document_chunks(self, document_id: str) -> List[Dict[str, Any]]:
        """Get all chunks associated with a document_id."""
        if self.provider in {"postgresql", "postgres"}:
            self.init_rag_table()
            conn = self._get_db_connection()
            chunks = []
            sql = """
                SELECT chunk_id, text, metadata, vector
                FROM rag_knowledge_chunks
                WHERE document_id = %s
                ORDER BY chunk_id;
            """
            with conn.cursor() as cur:
                cur.execute(sql, (document_id,))
                rows = cur.fetchall()
                for row in rows:
                    chunks.append({
                        "id": row[0],
                        "text": row[1],
                        "metadata": row[2] if isinstance(row[2], dict) else json.loads(row[2] or '{}'),
                        "vector": _vector_to_list(row[3])
                    })
            conn.close()
            return chunks
        
        logger.warning("get_document_chunks is only fully implemented for postgresql provider")
        return []

    def delete_document(self, document_id: str) -> Dict[str, Any]:
        """Delete all chunks for a document."""
        if self.provider in {"postgresql", "postgres"}:
            self.init_rag_table()
            conn = self._get_db_connection()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM rag_knowledge_chunks WHERE document_id = %s;", (document_id,))
                deleted = cur.rowcount
            conn.commit()
            conn.close()
            return {"success": True, "deleted": deleted, "provider": "postgresql"}
        
        logger.warning("delete_document is only fully implemented for postgresql provider")
        return {"success": False, "deleted": 0}

    def toggle_document_active(self, document_id: str, is_active: bool) -> Dict[str, Any]:
        """Toggle active status of a document (updates all chunks)."""
        if self.provider in {"postgresql", "postgres"}:
            self.init_rag_table()
            conn = self._get_db_connection()
            with conn.cursor() as cur:
                active_str = "true" if is_active else "false"
                cur.execute(f"""
                    UPDATE rag_knowledge_chunks
                    SET metadata = jsonb_set(metadata, '{{is_active}}', %s::jsonb)
                    WHERE document_id = %s;
                """, (active_str, document_id))
                updated = cur.rowcount
            conn.commit()
            conn.close()
            return {"success": True, "updated": updated, "provider": "postgresql"}
        
        logger.warning("toggle_document_active is only fully implemented for postgresql provider")
        return {"success": False, "updated": 0}

    def update_chunk(self, chunk_id: str, new_text: str, new_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Update a specific chunk text and metadata. Re-computes embeddings if text is updated."""
        if self.provider in {"postgresql", "postgres"}:
            self.init_rag_table()
            conn = self._get_db_connection()
            
            with conn.cursor() as cur:
                cur.execute("SELECT text, metadata FROM rag_knowledge_chunks WHERE chunk_id = %s;", (chunk_id,))
                row = cur.fetchone()
            
            if not row:
                conn.close()
                return {"success": False, "error": f"Chunk '{chunk_id}' not found."}
            
            old_text, old_meta_json = row
            old_meta = old_meta_json if isinstance(old_meta_json, dict) else json.loads(old_meta_json or '{}')
            old_meta.update(new_metadata)
            
            vector = None
            if new_text != old_text:
                from src.modules.matching.rag.embedding import build_embedding_adapter_from_env
                embedder = build_embedding_adapter_from_env()
                vectors = embedder.embed_texts([new_text])
                if vectors:
                    vector = [float(x) for x in vectors[0]]
            
            with conn.cursor() as cur:
                if vector:
                    cur.execute("""
                        UPDATE rag_knowledge_chunks
                        SET text = %s, vector = %s, metadata = %s
                        WHERE chunk_id = %s;
                    """, (new_text, vector, json.dumps(old_meta), chunk_id))
                else:
                    cur.execute("""
                        UPDATE rag_knowledge_chunks
                        SET text = %s, metadata = %s
                        WHERE chunk_id = %s;
                    """, (new_text, json.dumps(old_meta), chunk_id))
            conn.commit()
            conn.close()
            return {"success": True, "chunk_id": chunk_id, "provider": "postgresql"}
        
        logger.warning("update_chunk is only fully implemented for postgresql provider")
        return {"success": False, "error": "Not implemented for this provider"}


def insert_vectors(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    store = VectorStoreAdapter()
    return store.insert(records)


def upsert_vectors(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    store = VectorStoreAdapter()
    return store.upsert(records)


def upsert_interview_document(
    *,
    file_path: Optional[str] = None,
    raw_text: Optional[str] = None,
    document_obj: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Read doc -> embed -> upsert vectors."""
    records = generate_embedding_payloads(
        file_path=file_path,
        raw_text=raw_text,
        document_obj=document_obj,
    )
    result = upsert_vectors(records)
    result["records"] = len(records)
    return result

