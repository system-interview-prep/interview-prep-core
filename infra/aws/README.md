# Triển khai backend INTERVIA lên AWS (api.phongvan.app)

Template: `infra/aws/phongvan-backend.yaml` · Pipeline: `.github/workflows/deploy-aws.yml`

Stack này đi cùng hai stack khác: website `phongvan.app` (repo `interview-prep-frontend`, `infra/aws/phongvan-frontend.yaml`) và máy GPU `ai.phongvan.app` (VieNeu TTS + Qwen3-ASR).

## Kiến trúc

```
Trình duyệt (phongvan.app) ──HTTPS / Socket.IO──▶ api.phongvan.app (Elastic IP)
                                                    │  EC2 t3.medium, Ubuntu 24.04
                                                    ▼
                                             Caddy :80/443 (Let's Encrypt)
                                                    │
                    ┌───────────────────────────────┼──────────────────────────────┐
                    ▼                               ▼                              ▼
             api (uvicorn :5000)             worker (Celery)               voice-agent (LiveKit)
                    │                               │                              │
                    └──────────────┬────────────────┘                              │
                                   ▼                                               ▼
                     postgres (ParadeDB) · rabbitmq                 LiveKit Cloud, ai.phongvan.app
                     dữ liệu trên ổ /data (EBS riêng)                (TTS VieNeu, STT Qwen)

GitHub Actions (push main)
  migration + pytest ─▶ docker build ─▶ ECR (:<sha>, :latest) ─▶ SSM Run Command ─▶ intervia-core-deploy <sha>
                                          (OIDC, không có access key)       (migrate, đổi container, tự rollback)
```

- **Một image, ba tiến trình.** `api`, `worker` và `voice-agent` chạy cùng một image (target `api` của Dockerfile), chỉ khác lệnh khởi động.
- **PostgreSQL chạy trong container, không dùng RDS**, vì code cần extension `pg_search` của ParadeDB mà RDS không có. Image được ghim cùng digest với `docker-compose.yml`.
- **Dữ liệu nằm trên ổ EBS riêng gắn ở `/data`**: database, RabbitMQ, chứng chỉ HTTPS, cache embedding/TTS, bản dump hằng đêm. Xoá stack thì AWS tự chụp snapshot ổ này.
- **PostgreSQL và RabbitMQ không mở ra Internet.** Security group chỉ mở 80/443. Mật khẩu của hai dịch vụ này được sinh ngẫu nhiên ở lần khởi động đầu, lưu tại `/data/secrets/stack-secrets.env`.
- **Secret của ứng dụng nằm trong một tham số SSM SecureString** chứa nguyên file `.env` (giống stack GPU). Máy đọc lại tham số này ở mỗi lần deploy.
- CI không SSH vào máy. Role của GitHub chỉ được push vào đúng một repo ECR và chạy đúng một SSM document; document đó chỉ nhận một tag ảnh hợp lệ (`^[A-Za-z0-9._-]{1,128}$`).

## Chi phí ước tính (ap-southeast-1, On-Demand)

Giá tham khảo, nên kiểm tra lại trên trang giá AWS trước khi tạo stack.

| Thành phần | Mỗi tháng |
|---|---|
| EC2 t3.medium, 4 GiB (t3.small, 2 GiB) | ~38,5 (~19,3) USD |
| Public IPv4 / Elastic IP | ~3,7 USD |
| EBS gp3: 30 GiB root + 20 GiB data | ~4,8 USD |
| ECR (10 ảnh) | ~0,5 USD |
| Tham số SSM bậc Advanced | 0,05 USD |

Tổng khoảng **48 USD/tháng** với t3.medium. Máy này chạy 24/7. Cộng với máy website (~25 USD/tháng) và giờ chạy máy GPU, hãy tính vào budget 150 USD.

Chỉ nên chọn t3.small khi tắt voice agent (`EnableVoiceAgent=false`).

## Các bước triển khai (làm lần lượt)

### 1. Đưa file `.env` production lên SSM

Sao chép `.env` đang dùng thành `.env.production` (file này không commit; `.gitignore` đã chặn `.env.*`) rồi sửa:

| Biến | Giá trị production |
|---|---|
| `JWT_SECRET` | Chuỗi ngẫu nhiên mới, **ít nhất 32 ký tự**. Thiếu thì deploy dừng ngay. |
| `QUESTION_BANK_SEED_ENABLED` | `true`. Nếu không, ngân hàng câu hỏi rỗng và mọi buổi phỏng vấn trả 409. |
| `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` | Tài khoản admin đầu tiên. Đặt mật khẩu mạnh, khác mật khẩu lúc dev. |
| `OPENAI_API_KEY`, `R2_*`, `LIVEKIT_*`, `ELEVENLABS_*`, `MINERU_*`, `PADDLEOCR_*`, `TYPESAFE_*` | Giữ như đang dùng. |
| `VOICE_LAB_ENABLED` | `true` nếu bật phỏng vấn voice/video. |
| `VIENEU_TTS_BASE_URL`, `QWEN_STT_BASE_URL` | `https://ai.phongvan.app` (máy GPU phải đang bật khi phỏng vấn bằng giọng nói). |

Không cần sửa các biến sau vì stack tự đặt và ghi đè: `APP_ENV` (`production`), `DATABASE_URL`, `RABBITMQ_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `CORS_ORIGINS` (lấy từ tham số `FrontendOrigins`), `LIVEKIT_AGENT_AUTOSTART` (`false`), `VOICE_RUNTIME_API_BASE_URL`, `VOICE_LAB_TTS_CACHE_DIR`, `EMBEDDING_CACHE_DIR`.

Xoá những dòng có giá trị rỗng dạng `TEN_BIEN=`: giá trị rỗng không được coi là mặc định, với biến kiểu số thì API sẽ không khởi động.

```powershell
aws ssm put-parameter --region ap-southeast-1 `
  --name /intervia/core/app-env --type SecureString --tier Advanced `
  --value file://.env.production --overwrite
```

Cần `--tier Advanced` vì file `.env` dài hơn giới hạn 4 KB của bậc Standard (Advanced cho 8 KB). Nếu file vẫn quá 8 KB, bỏ bớt các dòng đang để giá trị mặc định.

Đổi cấu hình về sau: chạy lại lệnh trên rồi deploy lại (mục 5 hoặc 6). Không cần sửa stack.

### 2. Tạo stack

AWS Console → CloudFormation → *Create stack* → *Upload a template file* → chọn `phongvan-backend.yaml`. Chọn Region **ap-southeast-1 (Singapore)**, cùng Region với tham số SSM ở bước 1.

| Tham số | Giá trị |
|---|---|
| `ApiDomainName` | `api.phongvan.app` |
| `FrontendOrigins` | `https://phongvan.app,https://www.phongvan.app` |
| `HostedZoneId` | Để trống nếu DNS không ở Route 53 |
| `AppEnvParameter` | `/intervia/core/app-env` |
| `EnableVoiceAgent` | `true` (cần `LIVEKIT_*` trong `.env`); `false` nếu chỉ dùng phỏng vấn chat |
| `GitHubOwner` / `GitHubRepo` | `system-interview-prep` / `interview-prep-core` |
| `GitHubOidcProviderArn` | **Nếu đã tạo stack website trước thì dán ARN** của provider `token.actions.githubusercontent.com` (IAM → *Identity providers*). Mỗi tài khoản AWS chỉ có một provider này; để trống khi đã có thì tạo stack sẽ lỗi. |
| `KeyPairName` / `SshCidr` | Để trống (vào máy bằng Session Manager) |

Bước *Review* phải tick **I acknowledge that AWS CloudFormation might create IAM resources**.

CLI tương đương:

```bash
aws cloudformation deploy --region ap-southeast-1 \
  --stack-name intervia-backend \
  --template-file infra/aws/phongvan-backend.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides GitHubOidcProviderArn=arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com
```

Sau khi stack xong, máy đã chạy Caddy, PostgreSQL và RabbitMQ. API chưa có cho tới lần deploy đầu tiên.

### 3. Trỏ DNS

Mở tab **Outputs**, xem dòng `DnsRecordToCreate`. Ở nơi quản lý tên miền phongvan.app, tạo:

| Loại | Tên | Giá trị |
|---|---|---|
| A | `api` | `PublicIp` |

Nếu dùng Cloudflare, để **DNS only** (mây xám) để Caddy tự xin chứng chỉ. Kiểm tra: `nslookup api.phongvan.app` phải trả về đúng `PublicIp`.

### 4. Cấu hình GitHub (Settings → Environments → New environment → `production`)

1. **Deployment branches**: chọn *Selected branches* → thêm `main`.
2. **Environment variables** (không có biến nào là bí mật; secret nằm ở SSM):

| Biến | Lấy từ Output |
|---|---|
| `AWS_REGION` | `DeploymentRegion` |
| `AWS_STACK_NAME` | `StackName` |
| `AWS_DEPLOY_ROLE_ARN` | `GitHubDeployRoleArn` |

Output `GitHubVariablesCommand` có sẵn lệnh `gh` để đặt cả ba biến.

### 5. Deploy

Workflow chạy khi có push lên `main`. Hiện `main` của repo này mới chỉ có commit đầu tiên, còn code nằm ở các nhánh feat, nên lần deploy đầu là lần merge code vào `main`. Tab **Actions** chạy *Deploy backend to AWS (api.phongvan.app)* với 3 job:

1. **Migrations and tests**: `alembic upgrade head` trên một ParadeDB trống, rồi `pytest`. Job này phải xanh thì mới build. Tại thời điểm viết (24/11/2026, nhánh `feat/practice-garden`), `pytest` đang đỏ: 7 file test không import được và 72 test fail, so với 2.419 test pass. Cần sửa các test này trước lần deploy đầu.
2. **Build and push image**: build target `api`, push `:<commit sha>` và `:latest` lên ECR.
3. **Deploy to EC2**: chạy SSM document. Trên máy, `intervia-core-deploy` làm lần lượt:
   1. đọc lại `.env` từ SSM và kiểm tra `JWT_SECRET`;
   2. kéo ảnh mới;
   3. chạy `alembic upgrade head` bằng ảnh mới. Nếu migration lỗi thì dừng, bản đang chạy không bị đụng tới;
   4. đổi container, chờ API `healthy` và kiểm tra `worker`, `voice-agent` còn chạy;
   5. nếu không đạt thì **tự quay về ảnh trước**.

   Cuối cùng workflow gọi `/health` và `/health/interview` qua HTTPS.

Trong lúc đổi container (dưới một phút), API trả 502 và các kết nối Socket.IO bị ngắt rồi tự nối lại. Buổi phỏng vấn voice đang diễn ra sẽ bị gián đoạn, nên tránh deploy khi có người đang phỏng vấn.

### 6. Nối frontend

Trong repo `interview-prep-frontend`, environment `production`: đặt `NEXT_PUBLIC_API_URL=https://api.phongvan.app` rồi chạy lại workflow deploy của frontend (biến `NEXT_PUBLIC_*` được gắn vào bundle lúc build).

### 7. Rollback

Actions → *Deploy backend to AWS* → **Run workflow** → nhập `image_tag` là SHA của commit muốn quay về (xem trong ECR). Job build được bỏ qua, chỉ chạy deploy.

- Nút *Run workflow* chỉ hiện khi file workflow đã có trên nhánh mặc định (`main`).
- **Rollback không hạ schema database.** Ảnh cũ sẽ chạy trên schema mới hơn. Nếu migration của bản lỗi không tương thích ngược, hãy khôi phục từ bản dump (mục Vận hành) hoặc viết migration sửa tiếp.

## Vận hành

```bash
# Vào máy (không cần SSH). Cài Session Manager plugin cho AWS CLI trước.
aws ssm start-session --target <InstanceId> --region ap-southeast-1

sudo tail -n 100 /var/log/intervia-core-bootstrap.log     # log lần khởi động đầu
sudo intervia-core-compose ps                             # trạng thái các container
sudo docker logs --tail 100 intervia-core-api             # log API
sudo docker logs --tail 100 intervia-core-worker          # log Celery
sudo docker logs --tail 100 intervia-core-voice-agent     # log LiveKit agent
sudo docker logs --tail 100 intervia-core-caddy           # log HTTPS / chứng chỉ
sudo cat /opt/intervia-core/release.env                   # ảnh đang chạy
sudo intervia-core-deploy <tag>                           # deploy tay (đọc lại .env từ SSM)

# Database
sudo intervia-core-backup                                 # dump ngay vào /data/backups
sudo docker exec -it intervia-core-postgres psql -U intervia -d intervia
# Khôi phục một bản dump (ghi đè dữ liệu hiện tại):
sudo docker exec -i intervia-core-postgres pg_restore -U intervia -d intervia --clean --if-exists < /data/backups/<file>.dump
```

| Đường dẫn trên máy | Nội dung |
|---|---|
| `/opt/intervia-core/docker-compose.yml` | Các service `api`, `worker`, `voice-agent`, `postgres`, `rabbitmq`, `caddy` |
| `/opt/intervia-core/.env` | Bản sao tham số SSM (quyền 0600) |
| `/opt/intervia-core/release.env` | `CORE_IMAGE=` ảnh đang chạy |
| `/etc/intervia-core/stack.env` | Region, ECR, domain, CORS, có bật voice agent hay không |
| `/data/postgres`, `/data/rabbitmq` | Dữ liệu database và hàng đợi |
| `/data/backups` | Dump hằng đêm lúc 02:00 giờ Việt Nam, giữ 7 ngày |
| `/data/secrets/stack-secrets.env` | Mật khẩu PostgreSQL và RabbitMQ |
| `/usr/local/sbin/intervia-core-*` | `compose`, `deploy`, `load-env`, `start`, `backup`, `ecr-login` |
| `intervia-core.service` (systemd) | Bật lại stack sau khi khởi động máy |

## Lưu ý

- **UserData chỉ chạy ở lần khởi động đầu.** Sửa phần bootstrap trong template rồi update stack sẽ không cấu hình lại máy đang chạy; hãy sửa file trên máy qua Session Manager.
- **Thay máy (đổi `AmiId`, đổi UserData) cần làm bằng tay.** CloudFormation tạo máy mới trước khi xoá máy cũ, mà ổ `/data` chỉ gắn được vào một máy, nên bước update sẽ lỗi. Cách làm: Stop máy cũ, gỡ (detach) ổ data trong EC2 Console, rồi mới update stack. Máy mới nhận lại ổ data cũ và không format lại.
- **Dump hằng đêm nằm cùng ổ với database.** Chúng cứu được lỗi thao tác (xoá nhầm, migration hỏng), không cứu được khi mất ổ. Muốn an toàn hơn, bật snapshot định kỳ cho ổ `DataVolumeId` bằng Data Lifecycle Manager.
- **Xoá stack** sẽ xoá ECR repo cùng các ảnh và trả lại Elastic IP; ổ data được chụp snapshot (tính phí lưu trữ cho tới khi bạn xoá snapshot). Bản ghi DNS tạo tay phải tự xoá.
- **Chỉ chạy một tiến trình API.** Phòng Socket.IO và bộ dọn phiên nằm trong bộ nhớ của tiến trình, nên không tăng số worker của uvicorn và không chạy hai máy song song khi chưa có Redis.
- Backend phải cho phép origin của website: đổi domain website thì sửa tham số `FrontendOrigins`, rồi sửa `CORS_ORIGINS` trong `/etc/intervia-core/stack.env` trên máy và deploy lại (vì UserData không chạy lại).

## Kiểm thử đã chạy trước khi bàn giao (24/11/2026)

- Build image target `api` và `test` từ Dockerfile đã sửa (thêm `alembic.ini` và `migrations/` vào image).
- `alembic upgrade head` trên ParadeDB trống (cùng digest): chạy hết tới bản mới nhất.
- Dựng UserData với giá trị thử rồi chạy file `docker-compose.yml` sinh ra trên máy dev (thay ổ `/data` bằng volume, Caddy chạy HTTP): migrate bằng image, API `healthy`, `/health` và `/health/interview` trả 200 qua Caddy, preflight CORS từ `https://phongvan.app` được chấp nhận còn origin khác thì không, Socket.IO bắt tay được, worker nối RabbitMQ, admin đầu tiên được tạo. Các biến stack đặt (`APP_ENV`, `DATABASE_URL`, `CORS_ORIGINS`…) thắng giá trị sai cố ý đặt trong `.env`.
- Chạy các script trên máy với `docker`/`aws` giả: tag lạ (`x;rm -rf /`) bị từ chối; `JWT_SECRET` ngắn bị từ chối và `.env` cũ được giữ; migration lỗi thì không đổi container; bản không healthy hoặc worker chết thì quay về ảnh trước.
- Kiểm tra mọi `!Ref`, `!GetAtt`, `!Sub`, `Condition` trong template đều trỏ tới thứ có thật; UserData dài 14,3 KB (giới hạn 16 KB); `caddy validate` cho Caddyfile; `docker compose config` cho file compose.

Chưa chạy: `cfn-lint`, `actionlint` (không cài được trong phiên làm việc), voice agent với LiveKit thật, và chưa tạo stack thật trên AWS. Trước khi tạo stack nên chạy `cfn-lint infra/aws/phongvan-backend.yaml`.
