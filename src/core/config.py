from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 5000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    jwt_secret: str = "development-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 1440
    google_oauth_userinfo_url: str = "https://www.googleapis.com/oauth2/v3/userinfo"

    aws_region: str = "us-east-1"
    s3_endpoint: str | None = None
    s3_bucket: str = "interview-prep"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    celery_broker_url: str = "amqp://guest:guest@localhost:5672/"
    celery_result_backend: str = "rpc://"
    database_url: str = "postgresql://user:password@localhost:5432/matching_db"
    vector_dimension: int = 1024
    bm25_weight: float = 0.4
    semantic_weight: float = 0.6
    rrf_k: int = 60
    use_gpu: bool = False
    model_cache_dir: str = "models_cache"
    openai_api_key: str | None = None
    llm_provider: str = "openai"
    llm_model: str = "gpt-5.4-mini"
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1024

    # MinerU Precision Extract API. Set MINERU_API_KEY to enable CV parsing.
    mineru_api_key: str | None = None
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_model_version: str = "vlm"
    mineru_language: str = "en"
    mineru_poll_interval_seconds: float = 2.0
    mineru_timeout_seconds: int = 300

    rabbitmq_cv_queue: str = "cv-processing"
    rabbitmq_jp_queue: str = "jp-processing"
    rabbitmq_matching_queue: str = "matching"
    rabbitmq_max_attempts: int = 3

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def validate_production(self) -> None:
        if self.app_env.casefold() == "production" and len(self.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must contain at least 32 characters in production")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_production()
    return settings
