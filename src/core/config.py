from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 5000
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    jwt_secret: str = "development-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 10080
    google_oauth_userinfo_url: str = "https://www.googleapis.com/oauth2/v3/userinfo"

    r2_endpoint_url: str | None = None
    r2_bucket_name: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_public_domain: str = ""
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    celery_broker_url: str = "amqp://guest:guest@localhost:5672/"
    celery_result_backend: str = "rpc://"
    database_url: str = "postgresql://user:password@localhost:5432/matching_db"
    openai_api_key: str | None = None
    llm_provider: str = "openai"
    # Pin the snapshot so an alias update cannot silently change eval behavior.
    llm_model: str = "gpt-5.4-mini-2026-03-17"
    # Shared self-hosted model-service settings.  Modules consume these through
    # src.modules.ai rather than coupling themselves to a tunnel/provider.
    ai_model_url: str | None = None
    ai_service_api_key: str | None = None
    ai_request_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    ai_max_input_chars: int = Field(default=12_000, ge=1, le=100_000)
    ai_max_output_tokens: int = Field(default=1_024, ge=1, le=8_192)
    # Output budget for evidence-grounded JD extraction.  The independent gold
    # set needs about 647 tokens at p95 and 816 at p99 for its canonical JSON;
    # 768 covers normal cases without the latency of the former 1,024 default.
    jd_parser_max_output_tokens: int = Field(default=4_096, ge=128, le=8_192)
    jd_parser_mode: str = "deterministic"
    cv_parser_max_output_tokens: int = Field(default=4_096, ge=128, le=8_192)
    cv_parser_mode: str = "deterministic"
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1024
    embedding_cache_enabled: bool = True
    embedding_cache_dir: str = "models_cache/embedding_cache"

    # MinerU Precision Extract API. Set MINERU_API_KEY to enable CV parsing.
    mineru_api_key: str | None = None
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_model_version: str = "vlm"
    mineru_language: str = "en"
    mineru_http_connect_timeout_seconds: float = Field(default=20.0, gt=0)
    mineru_http_read_timeout_seconds: float = Field(default=120.0, gt=0)
    mineru_http_write_timeout_seconds: float = Field(default=120.0, gt=0)
    mineru_http_pool_timeout_seconds: float = Field(default=20.0, gt=0)
    mineru_poll_interval_seconds: float = 2.0
    mineru_timeout_seconds: int = 300
    # API-side database observation interval for SSE status streams. The
    # browser holds one stream instead of independently polling resource APIs.
    sse_status_poll_interval_seconds: float = Field(default=1.0, ge=0.25, le=10.0)
    sse_heartbeat_seconds: float = Field(default=15.0, ge=1.0, le=60.0)

    rabbitmq_cv_queue: str = "cv-processing"
    rabbitmq_jd_queue: str = "jd-processing"
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
