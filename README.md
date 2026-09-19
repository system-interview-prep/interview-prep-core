# Interview Prep FastAPI Backend

Đánh giá phạm vi và lộ trình nâng cấp CV–JD matching: [docs/CV-JD-MATCHING-ASSESSMENT.md](docs/CV-JD-MATCHING-ASSESSMENT.md).

Kế hoạch CV parser evidence-first từ upload đến matching: [docs/CV-PARSER-END-TO-END-PLAN.md](docs/CV-PARSER-END-TO-END-PLAN.md).

Luồng CV ingestion/parser đang triển khai: [docs/CV-PARSER-IMPLEMENTATION.md](docs/CV-PARSER-IMPLEMENTATION.md).

Thiết kế interview realtime có kiểm soát (câu hỏi thích nghi, thời gian, STT Việt/Anh, latency và rubric): [docs/INTERVIEW-REALTIME-RESEARCH-AND-IMPLEMENTATION.md](docs/INTERVIEW-REALTIME-RESEARCH-AND-IMPLEMENTATION.md).

Backend FastAPI hợp nhất từ:

- `interview-prep-backend`: API nghiệp vụ, S3, RabbitMQ, Socket.IO.
- `interview-resume-matching-service`: CV/JD matching, RAG, pgvector và Celery worker.

Đây là **modular monolith**: triển khai chung một codebase nhưng mỗi miền nghiệp vụ
sở hữu router, schema, service và repository riêng. Tác vụ CPU/GPU nặng vẫn chạy ở
worker process, không chạy trong event loop của API.

## Cấu trúc

```text
src/
  core/                 # config, security, logging, exception handling
  infrastructure/       # PostgreSQL, S3, RabbitMQ, Socket.IO
  modules/
    auth/
    users/
    user_cvs/
    job_categories/
    job_descriptions/
    matching/            # algorithms + semantic matching facade
    sessions/
    scoring/
    chat/
    voice/
    video_calls/
    signaling/
  workers/               # process entrypoints và Celery tasks
tests/
```

Quy tắc phụ thuộc:

```text
router -> service -> repository -> infrastructure
                   -> module facade
```

- Module không import router/service nội bộ của module khác.
- Giao tiếp liên-module thông qua `facade.py` hoặc event/task contract.
- `matching` không quản lý user, auth hay session.
- API không chạy OCR/model inference trực tiếp; API publish task và trả `202`.

## Chạy local

```bash
cp .env.example .env
docker compose up --build
```

- FastAPI/OpenAPI: `http://localhost:5000/docs`
- Health: `http://localhost:5000/health`
- RabbitMQ management: `http://localhost:15672`
- ParadeDB (`pg_search` + pgvector): `localhost:5432`

Alembic tự chạy `upgrade head` trước khi API khởi động. PostgreSQL là datastore
duy nhất cho dữ liệu nghiệp vụ và vector; dự án không còn phụ thuộc DynamoDB.
ParadeDB cung cấp BM25 cho lexical ranking, còn pgvector lưu embedding từ OpenAI
để semantic ranking. Image được cố định ở tag `0.25.3-pg16` và digest đã xác nhận
để local/CI/production sử dụng cùng một binary.

## Kiểm thử

```bash
# API contract + architecture tests (API image)
docker build --target test -t interview-prep-fastapi-tests .
docker run --rm interview-prep-fastapi-tests

# Matching engine integration (cần extra matching)
pip install -e ".[dev,matching]"
pytest tests/modules/matching/test_matching_facade.py -q

# Lint code mới; engine/RAG legacy tạm được exclude trong pyproject.toml
ruff check src tests
```

Test API bao phủ health, matching sync/async, default payload, schema validation,
unknown route/method và OpenAPI response contract.

## Trạng thái migration

Khung FastAPI, module registry, infrastructure, matching/RAG source và worker đã
được hợp nhất. Các module nghiệp vụ còn lại có package boundary sẵn để port lần lượt.
Xem `MIGRATION.md` để biết contract và thứ tự chuyển đổi.
