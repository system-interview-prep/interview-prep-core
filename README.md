# interview-prep-core · Backend API

Backend của hệ thống **INTERVIA** — nền tảng luyện phỏng vấn dựa trên AI, hỗ trợ phân tích CV/JD và khớp hồ sơ ứng viên với yêu cầu công việc.

---

## Tính năng chính

| Module | Mô tả |
|---|---|
| **Auth** | JWT + Google OAuth2 |
| **User CVs** | Upload, parse và lưu trữ CV (PDF/DOCX) qua MinerU |
| **Job Descriptions** | Parse và phân tách JD thành atomic requirements |
| **Matching** | Khớp CV–JD 1-1 với BM25, semantic embedding, eligibility gate |
| **Interview Sessions** | Tạo và quản lý phiên phỏng vấn |
| **Interview Questions** | Sinh câu hỏi theo taxonomy/JD |
| **Chat** | Chat AI realtime qua Socket.IO |
| **Video Calls** | WebRTC signaling qua Socket.IO |
| **Voice** | STT/TTS trong phiên phỏng vấn |
| **Notifications** | Thông báo realtime |
| **Admin** | Quản trị người dùng và hệ thống |

---

## Kiến trúc hệ thống

```
┌──────────────────────────────┐
│     Next.js Frontend (3000)  │
└──────────┬───────────────────┘
           │ HTTP + Socket.IO
┌──────────▼───────────────────────────────────┐
│       FastAPI (ASGI) + python-socketio (5000) │
│  ┌──────┐ ┌─────────┐ ┌──────────┐           │
│  │ Auth │ │Matching │ │Sessions  │  ...       │
│  └──────┘ └─────────┘ └──────────┘           │
└───────┬──────────────┬───────────────────────┘
        │              │
┌───────▼──────┐ ┌─────▼──────────────────────┐
│  PostgreSQL  │ │  Celery Workers + RabbitMQ  │
│  (ParadeDB   │ │  cv-processing, jd-processing│
│  + pgvector) │ │  matching tasks              │
└──────────────┘ └─────────────────────────────┘
        │
┌───────▼────────────────────────────────────┐
│  External Services                         │
│  OpenAI API · MinerU PDF · Cloudflare R2   │
└────────────────────────────────────────────┘
```

**Kiến trúc:** Modular monolith — một codebase, mỗi miền nghiệp vụ sở hữu `router → service → repository`. Module không import nội bộ của nhau; giao tiếp qua `facade.py` hoặc Celery task contract.

**Luồng xử lý CV/JD:**
```
Upload file → publish task (RabbitMQ) → API trả 202
  → Celery worker: MinerU extract text → LLM parse → lưu canonical record
```

---

## Tech Stack

| Layer | Công nghệ |
|---|---|
| Language | Python 3.11 |
| Framework | FastAPI 0.115 + python-socketio 5 |
| Database | PostgreSQL 16 via ParadeDB 0.25.3 (BM25 + pgvector) |
| ORM | SQLAlchemy 2.0 (asyncio) + Alembic |
| Message Queue | RabbitMQ 3 + Celery 5 |
| LLM | OpenAI API (model configurable) |
| PDF Parse | MinerU API |
| Object Storage | Cloudflare R2 (boto3) |
| Auth | PyJWT + bcrypt + Google OAuth2 |
| Linting | Ruff |
| Testing | pytest + pytest-asyncio |
| Container | Docker + Docker Compose |

---

## Cấu trúc thư mục

```
interview-prep-core/
├── src/
│   ├── main.py              # Entry point, đăng ký tất cả module
│   ├── core/                # Config, settings (pydantic-settings)
│   ├── infrastructure/      # PostgreSQL, RabbitMQ, Socket.IO setup
│   ├── modules/             # Feature modules
│   │   ├── auth/
│   │   ├── users/
│   │   ├── user_cvs/
│   │   ├── job_descriptions/
│   │   ├── matching/        # BM25, semantic, RAG, evaluation runner
│   │   ├── sessions/
│   │   ├── chat/
│   │   ├── interview_questions/
│   │   ├── voice/
│   │   ├── video_calls/
│   │   ├── signaling/
│   │   ├── notifications/
│   │   ├── admin/
│   │   ├── taxonomy/
│   │   └── job_categories/
│   └── workers/             # Celery app + tasks (CV/JD/matching)
├── migrations/              # Alembic migration files
├── tests/                   # pytest test suite
├── docs/                    # Design docs
├── Dockerfile               # Multi-stage: api | worker | test
├── docker-compose.yml       # Dev stack
├── pyproject.toml
├── alembic.ini
└── .env.example
```

> Pattern mỗi module: `router.py → service.py → models → schemas.py`. Xem [`docs/MODULE-MVC-ARCHITECTURE.md`](docs/MODULE-MVC-ARCHITECTURE.md).

---

## Yêu cầu môi trường

| Công cụ | Phiên bản |
|---|---|
| Docker Desktop | ≥ 4.x |
| Docker Compose | ≥ 2.x (bundled với Docker Desktop) |
| Python | 3.11–3.12 (chỉ cần khi chạy không có Docker) |

> **Khuyến nghị:** dùng Docker Compose, không cần cài PostgreSQL hay RabbitMQ trực tiếp.

---

## Cách cài đặt

```bash
# 1. Clone repo
git clone <repo-url> interview-prep-core
cd interview-prep-core

# 2. Tạo .env từ template
cp .env.example .env
# → Mở .env và điền các giá trị (xem phần Cấu hình .env bên dưới)

# 3. Khởi động toàn bộ stack
docker compose up --build
```

Docker Compose khởi động 4 services:

| Service | Port | Mô tả |
|---|---|---|
| `rabbitmq` | 5672, 15672 | Message broker + Management UI |
| `postgres-db` | 5433 | ParadeDB/PostgreSQL |
| `backend` | 5000 | FastAPI (tự chạy `alembic upgrade head`) |
| `worker` | — | Celery worker (CV/JD/matching queues) |

---

## Cấu hình .env

Sao chép từ `.env.example`. **Không commit file `.env` chứa secret thật.**

```dotenv
# App
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=5000
CORS_ORIGINS=http://localhost:3000

# Security
JWT_SECRET=<ít nhất 32 ký tự ngẫu nhiên>
JWT_ALGORITHM=HS256
JWT_EXPIRES_MINUTES=10080

# Google OAuth
GOOGLE_OAUTH_USERINFO_URL=https://www.googleapis.com/oauth2/v3/userinfo

# Database — tự điền khi chạy Docker Compose
DATABASE_URL=postgresql://user:password@postgres-db:5432/matching_db

# RabbitMQ — tự điền khi chạy Docker Compose
RABBITMQ_URL=amqp://interview:interview_password@rabbitmq:5672/
CELERY_BROKER_URL=amqp://interview:interview_password@rabbitmq:5672/
CELERY_RESULT_BACKEND=rpc://

# Cloudflare R2 Object Storage
R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET_NAME=<bucket-name>
R2_ACCESS_KEY_ID=<key-id>
R2_SECRET_ACCESS_KEY=<secret>
R2_PUBLIC_DOMAIN=https://<custom-domain>

# OpenAI / LLM
OPENAI_API_KEY=<sk-...>
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini

# MinerU PDF Parser
MINERU_API_KEY=<key>
MINERU_BASE_URL=<url>

# Parser mode: "deterministic" (rule-based, an toàn cho dev/test) hoặc "llm"
JD_PARSER_MODE=deterministic
CV_PARSER_MODE=deterministic

# Worker auth
WORKER_SECRET=<secret>
API_BASE_URL=http://backend:5000
```

Xem đầy đủ tất cả biến tại [`.env.example`](.env.example).

---

## Cách chạy project

### Docker (khuyến nghị)

```bash
# Khởi động (có hot-reload src/)
docker compose up --build

# Restart một service cụ thể
docker compose restart backend

# Xem log
docker compose logs -f backend
docker compose logs -f worker

# Dừng và xóa container
docker compose down
```

### Local (không Docker)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -e ".[dev,matching]"

# Đảm bảo PostgreSQL và RabbitMQ đang chạy, cập nhật DATABASE_URL + RABBITMQ_URL trong .env
python -m alembic upgrade head
uvicorn src.main:asgi_app --host 0.0.0.0 --port 5000 --reload --reload-dir src
```

**Chạy Celery worker (terminal riêng):**

```bash
celery -A src.workers.celery_app:celery_app worker \
  --loglevel=info --concurrency=1 \
  --queues=celery,cv-processing,jd-processing,matching
```

---

## API / Swagger

| URL | Mô tả |
|---|---|
| `http://localhost:5000/docs` | Swagger UI (interactive) |
| `http://localhost:5000/redoc` | ReDoc |
| `http://localhost:5000/health` | Health check |
| `http://localhost:15672` | RabbitMQ Management UI |

---

## AI Pipeline

```
Upload CV/JD
    │
    ▼
Publish task → RabbitMQ → Celery Worker
    │
    ├── MinerU API ──► Full-text extraction (PDF/DOCX → Markdown)
    │
    ▼
LLM (OpenAI hoặc self-hosted endpoint)
    ├── CV Parser  → skills, experience, education → canonical record
    └── JD Parser  → atomic requirements + priority/operator/evidence

Matching Engine (per-request, sync):
    ├── Eligibility gate   (must-have hard constraints — không thể bị override)
    ├── BM25 lexical scorer  (ParadeDB pg_search)
    ├── Semantic embedding   (pgvector cosine similarity)
    └── Reliability-aware fusion → fit_band + gaps + evidence refs
```

**Parser modes** (biến `*_PARSER_MODE`):
- `deterministic` — rule-based, không gọi AI (dùng cho dev/test)
- `llm` — gọi OpenAI API hoặc self-hosted model

---

## Testing / Evaluation

### Chạy test

```bash
# Local
pytest -q

# Qua Docker (build test target)
docker build --target test -t intervia-tests .
docker run --rm intervia-tests

# Với matching integration tests
pip install -e ".[dev,matching]"
pytest tests/modules/matching/ -q

# Lint
ruff check src tests
```

### Evaluation matching

Evaluation runner: `src/modules/matching/evaluation/runner.py`

**Metrics theo dõi:**

| Tầng | Metrics |
|---|---|
| CV Parser | evidence validity, field P/R/F1, fallback rate, review rate |
| JD Parser | requirement P/R/F1, priority/operator accuracy, evidence validity |
| Matching | weighted kappa, ordinal MAE, macro-F1, hard-violation rate |
| End-to-end | predicted-canonical vs oracle-canonical delta |
| Safety | Brier/ECE, score counterfactual stability |

> Chi tiết quality gates và evaluation plan: [`docs/CV-JD-MATCHING-ASSESSMENT.md`](docs/CV-JD-MATCHING-ASSESSMENT.md)

---

## Database Migrations

```bash
# Tạo migration mới
python -m alembic revision --autogenerate -m "mô tả thay đổi"

# Áp dụng migration
python -m alembic upgrade head

# Rollback 1 bước
python -m alembic downgrade -1
```

> Xem thêm: [`MIGRATION.md`](MIGRATION.md)

---

## Git Workflow & Conventional Commits

```
main  ← production-ready
  └── dev  ← integration
        └── feat/*, fix/*, chore/*  ← feature branches
```

**Format commit message:**

```
feat(matching): thêm BM25 scorer cho lexical baseline
fix(cv-parser): sửa lỗi extract khi CV thiếu section Education
chore(deps): nâng openai lên 1.50
docs(readme): cập nhật hướng dẫn cài đặt
test(matching): thêm test eligibility gate với must-have missing
```

Types: `feat` · `fix` · `chore` · `docs` · `test` · `refactor` · `perf`

---

## Deployment

```bash
# Build image API production
docker build --target api -t intervia-backend:latest .

# Build image Worker production
docker build --target worker -t intervia-worker:latest .
```

Đặt đầy đủ các biến từ `.env.example` vào environment của hosting. `DATABASE_URL` và `RABBITMQ_URL` trỏ đến production services.

---

## Security

- JWT tokens có expiry (mặc định 7 ngày, cấu hình qua `JWT_EXPIRES_MINUTES`)
- Password hash bằng `bcrypt`
- CORS giới hạn qua `CORS_ORIGINS`
- File upload chỉ nhận PDF/DOCX, lưu qua Cloudflare R2 (không lưu local)
- Secret/API key không được commit vào repo — dùng `.env` local hoặc secret manager khi deploy

---

## Documentation liên quan

| Tài liệu | Mô tả |
|---|---|
| [`docs/MODULE-MVC-ARCHITECTURE.md`](docs/MODULE-MVC-ARCHITECTURE.md) | Kiến trúc module MVC |
| [`docs/CV-JD-MATCHING-ASSESSMENT.md`](docs/CV-JD-MATCHING-ASSESSMENT.md) | Assessment plan CV-JD matching |
| [`docs/CV-PARSER-END-TO-END-PLAN.md`](docs/CV-PARSER-END-TO-END-PLAN.md) | CV parser end-to-end plan |
| [`docs/CV-PARSER-IMPLEMENTATION.md`](docs/CV-PARSER-IMPLEMENTATION.md) | CV parser implementation notes |
| [`MIGRATION.md`](MIGRATION.md) | Hướng dẫn DB migration |
| [DOC_AND_PLAN repo](https://github.com) | Roadmap, research, evaluation data plans |

---

## License

Dự án luận văn tốt nghiệp — nội bộ nhóm nghiên cứu.

