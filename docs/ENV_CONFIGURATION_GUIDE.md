# Environment configuration and runtime settings

Scope: `interview-prep-core`. This guide classifies configuration; it does not
move values or secrets out of `.env`.

## Current behavior

The admin system configuration endpoint is read-only. Values come from `.env`
or code defaults, and changes generally require restarting the API, worker, and
voice agent that consume them. There is no general-purpose database-backed
settings table yet.

“Runtime candidate” below means a value that can be moved to an admin-managed
table after validation, audit logging, and cache/reload behavior are added. It
does not mean the current application can already change it live.

## Keep in deployment environment or a secret manager

These values describe a deployment, grant access to services, or affect
security. They should not be ordinary editable settings in an admin table.

| Category | Variables |
| --- | --- |
| Credentials and secrets | `JWT_SECRET`, `BOOTSTRAP_ADMIN_PASSWORD`, `WORKER_SECRET`, `DATABASE_URL`, `RABBITMQ_URL`, `CELERY_BROKER_URL`, `OPENAI_API_KEY`, `TYPESAFE_API_KEY`, `AI_SERVICE_API_KEY`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `MINERU_API_KEY`, `PADDLEOCR_ACCESS_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `ELEVENLABS_API_KEY`, `VIENEU_TTS_API_KEY` |
| Deployment topology and public URLs | `APP_ENV`, `APP_HOST`, `APP_PORT`, `CORS_ORIGINS`, `LIVEKIT_URL`, `VOICE_RUNTIME_API_BASE_URL`, `AI_MODEL_URL`, `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_PUBLIC_DOMAIN`, `MINERU_BASE_URL`, `PADDLEOCR_BASE_URL`, `VIENEU_TTS_BASE_URL`, `GOOGLE_OAUTH_USERINFO_URL` |
| Security and compatibility pins | `JWT_ALGORITHM`, `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`, `VECTOR_DIMENSION` (if it is used by an external deploy script), `LIVEKIT_AGENT_RUN_MODE`, `LIVEKIT_AGENT_AUTOSTART`, `LIVEKIT_AGENT_NAME` |
| Process and queue wiring | `RABBITMQ_CV_QUEUE`, `RABBITMQ_JD_QUEUE`, `RABBITMQ_MATCHING_QUEUE`, `RABBITMQ_PREFETCH`, `CELERY_RESULT_BACKEND` |

Provider API keys belong in a secret manager or deployment environment even
when the provider itself is selected dynamically. Bootstrap credentials should
be used only for initial setup and removed from the running deployment after
the administrator account exists.

## Good candidates for an admin-managed runtime settings table

Store only non-secret values here. Validate allowed values and ranges, record
who changed them, and apply them at a documented boundary (per request, new
job, or new interview session).

| Setting area | Environment variables / examples | Notes |
| --- | --- | --- |
| Feature switches | `QUESTION_GENERATION_ENABLED`, `QUESTION_PREGENERATION_ENABLED`, `LEGACY_INTERVIEW_API_ENABLED`, `VOICE_LAB_ENABLED`, `PARSER_SECTION_REGEX_V2_ENABLED`, `PDF_TEXT_LAYER_ENABLED` | Add audit history and define whether existing sessions/jobs keep their original value. |
| Parser and OCR choices | `JD_PARSER_MODE`, `CV_PARSER_MODE`, `JD_PARSER_MAX_OUTPUT_TOKENS`, `CV_PARSER_MAX_OUTPUT_TOKENS`, `OCR_PROVIDER`, `OCR_FALLBACK_PROVIDER`, `MINERU_MODEL_VERSION`, `MINERU_LANGUAGE`, `PADDLEOCR_MODEL` | Keep provider credentials and service URLs outside the table. |
| Interview policy | `INTERVIEW_MIN_MATCH_PERCENT`, `INTERVIEW_BLOCK_NOT_ELIGIBLE`, `INTERVIEW_REQUIRE_MATCH_SCORE`, `INTERVIEW_LOW_FIT_PERCENT`, `INTERVIEW_CHALLENGE_PERCENT`, `INTERVIEW_DYNAMIC_PLANNER_ENABLED`, `INTERVIEW_PLANNER_POLICY_JSON`, `QUESTION_GENERATION_ENABLED` | Validate planner JSON against `PlannerPolicyConfig`; keep approved question-bank content in its existing domain tables. |
| Matching and clarification policy | `JEV_CLARIFICATION_ENABLED`, `JEV_CLARIFICATION_CONFIDENCE_THRESHOLD`, `JEV_MAX_EVIDENCE_ITEMS`, `MATCHING_CLARIFICATION_QUESTIONS_ENABLED`, `MATCHING_CLARIFICATION_MODEL`, `MATCHING_CLARIFICATION_SEMANTIC_THRESHOLD`, `MATCHING_LLM_EVIDENCE_SEARCH_ENABLED`, `MATCHING_LLM_EVIDENCE_SEARCH_MAX_CHARS`, `MATCHING_LLM_EVIDENCE_SEARCH_MAX_REQUIREMENTS` | These change product behavior and are suitable for versioned, audited runtime policy. |
| Speech provider selection and presentation | `LIVEKIT_STT_PROVIDER`, `LIVEKIT_STT_MODEL`, `LIVEKIT_STT_LANGUAGE`, `QWEN_STT_MODEL`, `VOICE_LAB_TTS_PROVIDER`, `VOICE_LAB_FALLBACK_TTS_MODEL`, `VOICE_LAB_FALLBACK_TTS_VOICE`, `VOICE_LAB_FALLBACK_TTS_LANGUAGE`, `ELEVENLABS_MODEL_ID`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_VI_VOICE_ID`, `VIENEU_TTS_MODEL`, `VIENEU_TTS_VOICE`, `VOICE_LAB_VOICE`, `INTERVIEWER_VOICE_DIALECT`, `INTERVIEWER_SPEECH_V2` | Check that credentials exist for a selected provider before activating the change. Apply to new sessions; do not switch an active stream midway. |
| Speech behavior tuning | `QWEN_STT_CHUNK_SIZE_SECONDS`, `QWEN_STT_ENDPOINT_SILENCE_SECONDS`, `QWEN_STT_SILENCE_RMS_THRESHOLD`, `VOICE_LAB_VAD_*`, `VOICE_LAB_STT_*`, `VOICE_LAB_INTERRUPTION_*`, `VOICE_LAB_BACKCHANNEL_*`, `VOICE_LAB_ENDPOINT_MAX_DELAY`, `VOICE_LAB_NOISE_CANCELLATION` | Bound numeric values and roll out changes cautiously because they affect turn-taking and recognition quality. |
| Operational tuning | `INTERVIEW_AI_REQUESTS_PER_MINUTE`, `RABBITMQ_MAX_ATTEMPTS`, `RABBITMQ_RETRY_DELAY_MS`, `MINERU_*_TIMEOUT_SECONDS`, `PADDLEOCR_*_TIMEOUT_SECONDS`, `SSE_HEARTBEAT_SECONDS`, `SSE_STATUS_POLL_INTERVAL_SECONDS`, `PIPELINE_EVENTS_RETENTION_DAYS` | Prefer runtime settings only where components read settings dynamically; otherwise keep as deployment configuration. |
| Monitoring preferences | `LANGFUSE_ENABLED`, `LANGFUSE_ENVIRONMENT`, `LANGFUSE_TEXT_SAMPLE_RATE`, `LANGFUSE_REVIEW_RANDOM_RATE`, `LANGFUSE_REVIEW_QUEUE_ID`, `LANGFUSE_UI_URL`, `AI_CALL_LOG_CAPTURE_TEXT` | Treat text capture and sampling as privacy-sensitive; require elevated permission and audit. Keep Langfuse keys in the environment. |

## Keep fixed in code unless there is a clear product requirement

Values that define invariants or have no environment override should remain
code constants. The current admin configuration view already reports several
of these without an `env` name, including interview question/word budgets and
speech response limits. If product owners need to tune them, first give them a
named policy, validation bounds, and tests before exposing them as runtime
settings.

## Variables in the current core `.env` with no usage found in this repository

The following names were present in `interview-prep-core/.env` but no
references were found outside the env file in this repository scan:

`AWS_ACCESS_KEY_ID`, `AWS_REGION`, `AWS_SECRET_ACCESS_KEY`, `API_CORS_ORIGINS`,
`BM25_WEIGHT`, `MODEL_CACHE_DIR`, `PADDLE_IMAGE_TARGET`, `PADDLEOCR_DEVICE`,
`RRF_K`, `SEMANTIC_WEIGHT`, `SIMLI_API_KEY`, `SIMLI_FACE_ID`, `USE_GPU`,
`VECTOR_DB_PROVIDER`, `VECTOR_DIMENSION`.

Treat these as cleanup candidates, not confirmed safe deletions: an external
deployment script or another service may still consume them. `VECTOR_DIMENSION`
is especially worth checking against the database/vector index before removal.

## Suggested implementation order

1. Keep secrets and deployment wiring in `.env`/secret management.
2. Add a typed runtime settings model and database table for the candidate
   product policies above; start with interview thresholds, parser/OCR choice,
   and voice provider/model/voice selection.
3. Add an admin editor with validation, permission checks, audit history, and
   explicit apply timing. Keep the existing read-only config view for effective
   values and secret presence.
4. Move additional tuning only after confirming each consumer reads the
   runtime value instead of an import-time environment variable.
