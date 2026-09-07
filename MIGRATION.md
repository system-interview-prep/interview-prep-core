# Migration plan: NestJS + matching service -> FastAPI modular monolith

## Nguyên tắc

1. Không đổi URL/JSON contract của frontend trong cùng bước đổi ngôn ngữ.
2. Dùng PostgreSQL cho toàn bộ dữ liệu nghiệp vụ và vector; giữ S3 object key và RabbitMQ queue name.
3. Chạy song song NestJS/FastAPI theo từng module; chuyển route qua reverse proxy.
4. Chỉ xóa NestJS sau khi contract test và frontend smoke test đạt.
5. Matching chạy nội bộ qua `MatchingFacade`; không tự gọi HTTP localhost.

## Module ownership

| Module | Sở hữu | Không sở hữu |
|---|---|---|
| auth | JWT, register/login/Google auth | user profile |
| users | profile, avatar metadata | authentication |
| user_cvs | upload metadata, S3 key, parse status | matching algorithms |
| job_profiles | JD metadata, parse status | CV ranking |
| matching | vectorize, score, evidence, RAG | user/session authorization |
| sessions | interview lifecycle | model implementation |
| interview_questions | question plan and active question | transport/WebSocket |
| chat | message history and orchestration | voice encoding |
| voice | STT/TTS adapters | interview state |
| signaling | Socket.IO/WebRTC signaling | video media |

## Route compatibility checklist

- `/auth/*`
- `/user/*`
- `/users/me/cvs/*`
- `/admin/job-categories/*`
- `/admin/job-profiles/*`
- `/ai/session`, `/ai/sessions`, `/ai/chat`, `/ai/history`
- `/ai/score-cv-jp`, `/ai/session/{id}/questions/generate`
- `/interview/video-calls/*`
- Socket.IO namespaces `/cv`, `/jp`, `/chat`, `/signaling`

## Thứ tự port đề xuất

1. Core config/security + auth/users.
2. `user_cvs` và `job_profiles`, giữ queue contracts cũ.
3. Worker parse CV/JD.
4. Matching facade + scoring endpoint.
5. Sessions/questions/chat.
6. Socket.IO signaling/status.
7. Voice/video-call.

## Definition of done cho mỗi module

- OpenAPI request/response tương thích frontend hiện tại.
- Unit test service/repository.
- Contract test so sánh NestJS và FastAPI.
- Unauthorized/cross-user access test.
- Timeout, retry và DLQ test cho tác vụ nền.

## Hiện đã chạy được và phần còn phải port

Đã có code thực thi cho health API, matching facade, ensemble algorithms, RAG,
Celery matching task, PostgreSQL schema/Alembic và các infrastructure adapter. Các package module còn lại mới
là boundary có chủ đích; chưa được xem là đã chuyển nghiệp vụ chỉ vì thư mục tồn tại.

DynamoDB đã bị loại khỏi FastAPI backend. Vì môi trường chưa có dữ liệu cần giữ,
migration đầu tiên tạo schema PostgreSQL mới thay vì thực hiện data backfill.

Hai task `cv.parse` và `job_profile.parse` hiện fail rõ ràng bằng
`NotImplementedError`. Đây là fail-fast để không trả trạng thái thành công giả trước
khi logic của worker TypeScript được port và có contract test.
