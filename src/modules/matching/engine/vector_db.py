"""PostgreSQL + pgvector database module for managing JD vector embeddings."""

import logging
import psycopg2
from psycopg2.extras import execute_values
from pgvector.psycopg2 import register_vector

logger = logging.getLogger(__name__)


class VectorDB:
    """Manages PostgreSQL connection and pgvector operations for JD embeddings."""

    def __init__(self, database_url: str, vector_dimension: int = 384):
        self.database_url = database_url
        self.vector_dimension = vector_dimension
        self._conn = None

    def _get_connection(self):
        """Get or create a database connection."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.database_url)
            # Must create extension before registering vector type!
            with self._conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            self._conn.commit()
            register_vector(self._conn)
        return self._conn

    def init_tables(self):
        """Create the job_profiles_vector table and ensure all model/version columns exist."""
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS job_profiles_vector (
                    job_id VARCHAR(255) PRIMARY KEY,
                    job_vector vector({self.vector_dimension}),
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                ALTER TABLE job_profiles_vector ADD COLUMN IF NOT EXISTS bert_vector vector(768);
            """)
            cur.execute("""
                ALTER TABLE job_profiles_vector ADD COLUMN IF NOT EXISTS distilbert_vector vector(768);
            """)
            cur.execute("""
                ALTER TABLE job_profiles_vector ADD COLUMN IF NOT EXISTS checksum VARCHAR(64);
            """)
            cur.execute("""
                ALTER TABLE job_profiles_vector ADD COLUMN IF NOT EXISTS version INT DEFAULT 1;
            """)
            cur.execute("""
                ALTER TABLE job_profiles_vector ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
            """)
            cur.execute("""
                ALTER TABLE job_profiles_vector ADD COLUMN IF NOT EXISTS job_text TEXT;
            """)
            conn.commit()
            logger.info("job_profiles_vector table initialized successfully with Version, Checksum & Job Text support")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to initialize vector table: {e}")
            raise
        finally:
            cur.close()

    def upsert_jd_vector(self, job_id: str, vector: list, checksum: str = ""):
        """Insert or update a JD vector embedding (defaults to sBERT column)."""
        self.upsert_jd_cache(job_id, checksum, 'sbert', vector)

    def get_jd_vector(self, job_id: str):
        """Retrieve a JD vector embedding by job_id (defaults to sBERT column)."""
        cache = self.get_jd_cache(job_id)
        return cache.get('sbert') if cache else None

    def get_jd_cache(self, job_id: str) -> dict | None:
        """Retrieve cache record for a job including all vectors, checksum, version, and job_text."""
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT job_vector, bert_vector, distilbert_vector, checksum, version, job_text 
                FROM job_profiles_vector 
                WHERE job_id = %s;
            """, (job_id,))
            result = cur.fetchone()
            if result:
                return {
                    'sbert': result[0].tolist() if result[0] is not None and hasattr(result[0], 'tolist') else result[0],
                    'bert': result[1].tolist() if result[1] is not None and hasattr(result[1], 'tolist') else result[1],
                    'distilbert': result[2].tolist() if result[2] is not None and hasattr(result[2], 'tolist') else result[2],
                    'checksum': result[3],
                    'version': result[4],
                    'job_text': result[5]
                }
            return None
        except Exception as e:
            logger.error(f"Failed to get cache for job_id={job_id}: {e}")
            raise
        finally:
            cur.close()

    def upsert_jd_cache(self, job_id: str, checksum: str, model_name: str, vector: list, job_text: str = None):
        """Upsert vector for a specific model under a specific checksum, managing versions and raw text."""
        column_map = {
            'sbert': 'job_vector',
            'bert': 'bert_vector',
            'distilbert': 'distilbert_vector'
        }
        col = column_map.get(model_name)
        if not col:
            raise ValueError(f"Unsupported model name for database caching: {model_name}")

        conn = self._get_connection()
        cur = conn.cursor()
        try:
            # Check existing checksum
            cur.execute("SELECT checksum, version FROM job_profiles_vector WHERE job_id = %s;", (job_id,))
            row = cur.fetchone()

            if row:
                stored_checksum, stored_version = row[0], row[1]
                if stored_checksum != checksum:
                    # Checksum changed! Increment version and clear all other model vectors
                    other_cols = [c for c in column_map.values() if c != col]
                    set_clause = ", ".join([f"{c} = NULL" for c in other_cols])
                    
                    # Update row including new job_text
                    cur.execute(f"""
                        UPDATE job_profiles_vector
                        SET {col} = %s, checksum = %s, version = %s, job_text = %s, {set_clause}, updated_at = NOW()
                        WHERE job_id = %s;
                    """, (vector, checksum, (stored_version or 1) + 1, job_text, job_id))
                    logger.info(f"JD updated for {job_id}. Version bumped to {(stored_version or 1) + 1}, cleared other stale vectors.")
                else:
                    # Checksum matches, just update this model's vector
                    if job_text is not None:
                        cur.execute(f"""
                            UPDATE job_profiles_vector
                            SET {col} = %s, job_text = %s, updated_at = NOW()
                            WHERE job_id = %s;
                        """, (vector, job_text, job_id))
                    else:
                        cur.execute(f"""
                            UPDATE job_profiles_vector
                            SET {col} = %s, updated_at = NOW()
                            WHERE job_id = %s;
                        """, (vector, job_id))
            else:
                # No record exists. Insert new record as version 1
                cur.execute(f"""
                    INSERT INTO job_profiles_vector (job_id, checksum, version, job_text, {col})
                    VALUES (%s, %s, 1, %s, %s);
                """, (job_id, checksum, job_text, vector))
                logger.info(f"Initialized new JD cache for {job_id} at version 1.")

            conn.commit()
            logger.info(f"Upserted {model_name} vector cache for job_id={job_id}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to upsert cache for job_id={job_id}: {e}")
            raise
        finally:
            cur.close()

    def cosine_similarity_score(self, job_id: str, cv_vector: list) -> float | None:
        """Calculate cosine similarity between a CV vector and a stored JD vector.
        
        Uses pgvector's <=> (cosine distance) operator.
        Returns similarity score (1 - distance), or None if job_id not found.
        """
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT 1 - (job_vector <=> %s::vector) AS similarity_score
                FROM job_profiles_vector
                WHERE job_id = %s;
            """, (cv_vector, job_id))
            result = cur.fetchone()
            return float(result[0]) if result else None
        except Exception as e:
            logger.error(f"Failed to compute similarity for job_id={job_id}: {e}")
            raise
        finally:
            cur.close()

    def delete_jd_vector(self, job_id: str):
        """Delete a JD vector embedding."""
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM job_profiles_vector WHERE job_id = %s;",
                (job_id,)
            )
            conn.commit()
            logger.info(f"Deleted vector for job_id={job_id}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to delete vector for job_id={job_id}: {e}")
            raise
        finally:
            cur.close()

    def init_cv_table(self):
        """Create the cv_profiles_vector table and ensure all model/version columns exist."""
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS cv_profiles_vector (
                    cv_id VARCHAR(255) PRIMARY KEY,
                    cv_vector vector({self.vector_dimension}),
                    bert_vector vector(768),
                    distilbert_vector vector(768),
                    checksum VARCHAR(64),
                    version INT DEFAULT 1,
                    cv_text TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()
            logger.info("cv_profiles_vector table initialized successfully")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to initialize CV vector table: {e}")
            raise
        finally:
            cur.close()

    def get_cv_cache(self, cv_id: str) -> dict | None:
        """Retrieve cache record for a CV including all vectors, checksum, version, and cv_text."""
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT cv_vector, bert_vector, distilbert_vector, checksum, version, cv_text 
                FROM cv_profiles_vector 
                WHERE cv_id = %s;
            """, (cv_id,))
            result = cur.fetchone()
            if result:
                return {
                    'sbert': result[0].tolist() if result[0] is not None and hasattr(result[0], 'tolist') else result[0],
                    'bert': result[1].tolist() if result[1] is not None and hasattr(result[1], 'tolist') else result[1],
                    'distilbert': result[2].tolist() if result[2] is not None and hasattr(result[2], 'tolist') else result[2],
                    'checksum': result[3],
                    'version': result[4],
                    'cv_text': result[5]
                }
            return None
        except Exception as e:
            logger.error(f"Failed to get cache for cv_id={cv_id}: {e}")
            raise
        finally:
            cur.close()

    def upsert_cv_cache(self, cv_id: str, checksum: str, model_name: str, vector: list, cv_text: str = None):
        """Upsert vector for a specific model under a specific checksum for a CV."""
        column_map = {
            'sbert': 'cv_vector',
            'bert': 'bert_vector',
            'distilbert': 'distilbert_vector'
        }
        col = column_map.get(model_name)
        if not col:
            raise ValueError(f"Unsupported model name for CV database caching: {model_name}")

        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT checksum, version FROM cv_profiles_vector WHERE cv_id = %s;", (cv_id,))
            row = cur.fetchone()

            if row:
                stored_checksum, stored_version = row[0], row[1]
                if stored_checksum != checksum:
                    other_cols = [c for c in column_map.values() if c != col]
                    set_clause = ", ".join([f"{c} = NULL" for c in other_cols])
                    cur.execute(f"""
                        UPDATE cv_profiles_vector
                        SET {col} = %s, checksum = %s, version = %s, cv_text = %s, {set_clause}, updated_at = NOW()
                        WHERE cv_id = %s;
                    """, (vector, checksum, (stored_version or 1) + 1, cv_text, cv_id))
                    logger.info(f"CV updated for {cv_id}. Version bumped to {(stored_version or 1) + 1}, cleared other stale vectors.")
                else:
                    if cv_text is not None:
                        cur.execute(f"""
                            UPDATE cv_profiles_vector
                            SET {col} = %s, cv_text = %s, updated_at = NOW()
                            WHERE cv_id = %s;
                        """, (vector, cv_text, cv_id))
                    else:
                        cur.execute(f"""
                            UPDATE cv_profiles_vector
                            SET {col} = %s, updated_at = NOW()
                            WHERE cv_id = %s;
                        """, (vector, cv_id))
            else:
                cur.execute(f"""
                    INSERT INTO cv_profiles_vector (cv_id, checksum, version, cv_text, {col})
                    VALUES (%s, %s, 1, %s, %s);
                """, (cv_id, checksum, cv_text, vector))
                logger.info(f"Initialized new CV cache for {cv_id} at version 1.")

            conn.commit()
            logger.info(f"Upserted {model_name} vector cache for cv_id={cv_id}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to upsert CV cache for cv_id={cv_id}: {e}")
            raise
        finally:
            cur.close()

    def delete_cv_vector(self, cv_id: str):
        """Delete a CV vector embedding."""
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM cv_profiles_vector WHERE cv_id = %s;",
                (cv_id,)
            )
            conn.commit()
            logger.info(f"Deleted vector for cv_id={cv_id}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to delete CV vector for cv_id={cv_id}: {e}")
            raise
        finally:
            cur.close()

    def close(self):
        """Close the database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("Database connection closed")

