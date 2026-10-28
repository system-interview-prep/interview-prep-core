from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 5000
    # None enables traces only in local/development environments.
    trace_logs_enabled: bool | None = None
    trace_logs_dir: str = "trace-logs"
    # Admin pipeline view (interview_pipeline_events, ai_call_logs). Unlike the
    # JSONL traces these are kept in every environment. None: on, except
    # under pytest.
    pipeline_events_enabled: bool | None = None
    pipeline_events_retention_days: int = Field(default=90, ge=1, le=3650)
    # Store truncated model inputs/outputs in ai_call_logs. They contain CV
    # and answer text, so this is off unless an operator turns it on.
    ai_call_log_capture_text: bool = False
    # Langfuse (src/core/llm_tracing.py): one trace per interview turn and per
    # grading run, with every model call, the engine's decision, guard results
    # and rule scores. Off unless the keys are set (and the `langfuse` package
    # is installed). Self-hosted: LANGFUSE_BASE_URL=http://host.docker.internal:3000.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_enabled: bool | None = None
    langfuse_environment: str = ""
    # Share of sessions whose prompts and answers are sent as text (masked);
    # flagged turns are always sent. The LLM judge in Langfuse runs on these.
    langfuse_text_sample_rate: float = Field(default=0.2, ge=0.0, le=1.0)
    # Annotation queue that receives flagged turns plus a random share of the
    # text-sampled ones (scripts/langfuse_setup.py prints the id).
    langfuse_review_queue_id: str = ""
    langfuse_review_random_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    # Address of the Langfuse UI for links on the admin pages, when the browser
    # reaches it differently from the backend (http://localhost:3000 vs host.docker.internal).
    langfuse_ui_url: str = ""
    # Seeds the curated Question Bank (approved questions, rubrics and taxonomy
    # mappings). Interviews fail closed with question_bank_insufficient on an
    # empty bank, so a demo deployment must turn this on explicitly.
    # None seeds only in development/test environments.
    question_bank_seed_enabled: bool | None = None
    # When a JD skill has no approved question (nor a broader-skill or role
    # question), ask the LLM for one, use it in that interview, and file it as
    # an IN_REVIEW draft. Off -> the skill is dropped and reported uncovered.
    question_generation_enabled: bool = True
    # When a job is published, a background task generates gated drafts so each
    # must-have skill has enough questions to rotate (question_coverage).
    question_pregeneration_enabled: bool = True
    # Old unstructured interview endpoints bypass the structured turn state
    # machine. They remain opt-in only for a controlled migration window.
    legacy_interview_api_enabled: bool = False
    interview_ai_requests_per_minute: int = Field(default=20, ge=1, le=600)
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    jwt_secret: str = "development-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 10080
    google_oauth_userinfo_url: str = "https://www.googleapis.com/oauth2/v3/userinfo"
    # Local/dev bootstrap only. Both values must be supplied to create the
    # initial administrator; credentials are never hard-coded in application code.
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

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
    typesafe_api_key: str | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_default_model: str = "jev-latest"
    jev_clarification_enabled: bool = False
    jev_timeout_seconds: float = Field(default=5.0, gt=0)
    jev_clarification_confidence_threshold: float = Field(default=0.75, ge=0, le=1)
    jev_max_evidence_items: int = Field(default=5, ge=1, le=20)
    matching_clarification_questions_enabled: bool = False
    matching_clarification_model: str = "gpt-5.4-mini"
    matching_llm_evidence_search_enabled: bool = True
    matching_llm_evidence_search_max_chars: int = Field(default=12_000, ge=1_000, le=100_000)
    matching_llm_evidence_search_max_requirements: int = Field(default=12, ge=1, le=30)
    # Kept as text so a deliberately blank .env.example value stays valid.
    matching_clarification_semantic_threshold: str | None = None
    voice_lab_enabled: bool = False
    voice_lab_realtime_model: str = "gpt-realtime-2.1"
    voice_lab_transcription_model: str = "gpt-4o-mini-transcribe"
    voice_lab_tts_model: str = "gpt-4o-mini-tts"
    voice_lab_voice: str = "marin"
    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None
    livekit_agent_name: str = "intervia-voice"
    # Start the LiveKit worker from the API process during local development.
    # Keep this opt-in for deployments that run the worker as its own service.
    livekit_agent_autostart: bool = False
    livekit_agent_run_mode: str = "dev"
    llm_provider: str = "openai"
    # Pin the snapshot so an alias update cannot silently change eval behavior.
    llm_model: str = "gpt-5.4-mini-2026-03-17"
    # Dynamic interview planning is opt-in because its time-allocation values
    # are Product policy. When enabled, the JSON must satisfy PlannerPolicyConfig.
    interview_dynamic_planner_enabled: bool = False
    interview_planner_policy_json: str | None = None
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
    parser_section_regex_v2_enabled: bool = True
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1024
    embedding_cache_enabled: bool = True
    embedding_cache_dir: str = "models_cache/embedding_cache"

    # Document extraction provider. Select the adapter used by CV and JD jobs.
    # Supported values: mineru (default) and paddleocr.
    ocr_provider: str = "mineru"
    # Provider tried when OCR_PROVIDER fails (outage, timeout, API error, missing
    # key, empty output). "auto" = the other supported provider; "none" = no
    # fallback; or name one explicitly.
    ocr_fallback_provider: str = "auto"
    # Read born-digital PDFs and .docx files locally before any OCR upload;
    # scans, image-only PDFs and images still go to the OCR providers.
    pdf_text_layer_enabled: bool = True

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

    # PaddleOCR Official API (AI Studio hosted service). The adapter uploads
    # the document, polls the asynchronous job, and normalizes its result to
    # DocumentArtifacts so the existing parser pipeline remains provider-neutral.
    paddleocr_access_token: str | None = None
    paddleocr_base_url: str = "https://paddleocr.aistudio-app.com"
    paddleocr_model: str = "PaddleOCR-VL-1.6"
    paddleocr_request_timeout_seconds: float = Field(default=120.0, gt=0)
    paddleocr_poll_interval_seconds: float = Field(default=2.0, ge=0)
    paddleocr_timeout_seconds: int = Field(default=600, gt=0)
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
