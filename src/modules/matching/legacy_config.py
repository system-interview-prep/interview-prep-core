from src.core.config import get_settings


class MatchingConfig:
    """Compatibility configuration for the migrated matching/RAG implementation."""

    @property
    def DATABASE_URL(self) -> str:
        return get_settings().database_url

    @property
    def VECTOR_DIMENSION(self) -> int:
        return get_settings().vector_dimension

    @property
    def DEVICE(self) -> str:
        return "cuda" if get_settings().use_gpu else "cpu"

    @property
    def MODEL_CACHE_DIR(self) -> str:
        return get_settings().model_cache_dir

    DEFAULT_ALGORITHMS = ["requirements", "bm25", "cosine", "ner"]
    ALGORITHM_TIMEOUT = 300
    BATCH_SIZE = 32
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024
    MAX_FILES_PER_REQUEST = 50
    ALLOWED_EXTENSIONS = {"pdf", "docx", "doc"}


config_dict = {
    "development": MatchingConfig,
    "production": MatchingConfig,
    "testing": MatchingConfig,
    "default": MatchingConfig,
}
