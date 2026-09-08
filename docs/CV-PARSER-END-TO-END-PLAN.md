# Kế hoạch CV parser end-to-end, evidence-first

Ngày lập: 08-09-2026. Trạng thái: kế hoạch triển khai và đánh giá; chưa phải tuyên bố chất lượng đã được chứng minh.

Tiến độ triển khai: WP0/WP1/WP2 đã có foundation gồm Resume schema v2.1, MinerU artifact reader, canonical source/evidence mapper và deterministic subset parser. Kết quả unit/contract được ghi trong `EXP-20260908-001`; chưa có gold-corpus quality evaluation.

## 1. Quyết định kiến trúc

Không dùng `LLM(raw CV) -> JSON -> READY` làm đường production duy nhất. Chọn pipeline hybrid, evidence-first:

```text
Upload
  -> kiểm tra file + checksum
  -> trích xuất text/layout bất biến
  -> phân đoạn section
  -> nhiều extractor tạo candidate
  -> gắn candidate vào exact source span
  -> normalize/taxonomy/linking bằng code
  -> strict schema + invariant validation
  -> confidence calibration
  -> READY hoặc REVIEW_REQUIRED
  -> redacted matching projection
  -> requirement-level matcher
```

LLM được phép tham gia candidate extraction và relation linking. LLM không được tự quyết định dữ liệu cuối, tự suy ra proficiency/số năm kinh nghiệm, hoặc tạo claim không có evidence. Structured output giải quyết tính hợp lệ của cấu trúc; tính đúng ngữ nghĩa phải được đo bằng gold data và kiểm tra lại với tài liệu nguồn.

## 2. Khoảng cách của code hiện tại

Luồng hiện tại trong `src/workers/tasks/cv.py` là:

```text
R2 file -> MinerU -> full.md -> user_cvs.raw_text -> DONE
```

Các khoảng trống chính:

- `is_ocr=True` cho mọi tài liệu, chưa phân biệt digital PDF/DOCX với scan.
- MinerU ZIP có structured layout artifacts nhưng `src/workers/mineru.py` chỉ đọc Markdown rồi bỏ phần còn lại.
- `user_cvs.parsed_data` đã có trong migration nhưng worker chưa ghi.
- Không có parse-run provenance, schema/prompt/model version, field confidence, evidence validation hoặc human review state.
- `DONE` hiện chỉ có nghĩa OCR trả text, dễ bị hiểu nhầm là canonical CV đã sẵn sàng matching.
- Raw CV chứa PII; scoring hiện vẫn đọc `raw_text` để embedding.

## 3. Contract dữ liệu cần chốt trước

Canonical Resume v2.1 phải là contract giữa parser, database và matcher. Ngoài các model đã có ở `src/modules/matching/schemas.py`, cần hoàn thiện:

- `DocumentRevision`: document ID, SHA-256, MIME đã phát hiện, số trang, extraction version.
- `PartialDate`: value và precision `year|month|day`; không ép dữ liệu chỉ có năm thành ngày giả.
- `ProjectEntry`, `EducationEntry`, `CertificationEntry`, `Achievement` và `Metric` có evidence.
- ID record (`claimId`) tách khỏi taxonomy ID (`conceptId`, `scheme`, `taxonomyVersion`).
- `EvidenceSpan`: artifact revision, page, block ID, exact quote, normalized-text offsets và bounding box khi có.
- Mỗi fact có `assertionSource=explicit|inferred`; fact inferred không được dùng làm hard requirement nếu chưa review.
- `ParserWarning`: code, field path, severity, message; không dùng chuỗi warning tự do.

Ba vùng dữ liệu phải tách biệt:

1. `CV identity`: PII mã hóa và giới hạn quyền truy cập.
2. `Source artifacts`: file gốc, raw/layout output, chỉ dùng audit/re-parse.
3. `Matching projection`: canonical claims đã redacted và validated; đây là payload duy nhất matcher/embedding được nhận.

## 4. Pipeline chi tiết

### Bước A — Upload và kiểm tra đầu vào

- Xác minh MIME bằng magic bytes, không chỉ tin extension/header.
- Giới hạn size/page, timeout và decompression ratio cho DOCX/ZIP.
- Tính checksum file trước khi lưu; idempotency key phải gồm `checksum + extractionConfigVersion` khi re-parse.
- Quét malware nếu môi trường production cho phép; file không hợp lệ chuyển `REJECTED`, không retry.
- Tạo record upload trước, nhưng không trả `READY` khi mới lưu thành công.

### Bước B — Trích xuất source artifacts

Đổi `extract_markdown()` thành interface `extract_document_artifacts()` trả:

```text
markdown
content_list
middle/layout JSON
page count
extractor name/version/config
artifact checksums
```

Lưu ZIP hoặc từng artifact vào R2 bằng immutable key. `content_list.json` là input ưu tiên cho parser vì giữ reading order, page và bounding box; Markdown chỉ là view/fallback. Đối với DOCX/digital PDF, thử native-text path và OCR path như hai biến thể trong benchmark thay vì mặc định OCR mọi file.

### Bước C — Canonical source text

- Tạo text bất biến từ các content blocks theo reading order.
- Gán `blockId`, page và bbox cho từng block.
- Chuẩn hóa Unicode NFC, line endings và whitespace bằng hàm có version.
- Lưu bảng mapping từ normalized offsets về source block; không yêu cầu LLM tự tính offset.
- Loại header/footer lặp lại bằng rule có log, không xóa âm thầm.

### Bước D — PII isolation

- Email/phone dùng deterministic parser trước; name/address dùng NER hoặc LLM candidate nhưng phải có span.
- Tạo redacted source với placeholder ổn định như `[EMAIL_1]`, `[PHONE_1]`.
- Không gửi ảnh, contact section hoặc raw text chứa PII sang external LLM nếu chưa có phê duyệt data policy.
- Test leakage trên cả prompt, log, trace, embeddings và error payload.

### Bước E — Section segmentation

Baseline đầu tiên là heading dictionary Việt/Anh + layout feature (`text_level`, font/layout block, khoảng cách, reading order). Các section chuẩn: profile, skills, employment, projects, education, certifications, languages và other.

Sau khi có gold data mới so sánh thêm:

- Text classifier theo block/line.
- Layout-aware model như LayoutLMv3 nếu baseline thất bại rõ ở multi-column/layout mới.
- OCR-free model như Donut chỉ là nhánh thí nghiệm; không thay MinerU trước khi thắng benchmark CV Việt/Anh và đáp ứng chi phí vận hành.

### Bước F — Candidate extraction hybrid

Chạy extractor theo section, không gửi toàn CV vào một prompt lớn:

- Rule/parser: email, phone, URL, date, duration, percentage, common degree/certificate forms.
- Taxonomy matcher: exact/alias/fuzzy candidates cho skills; normalization luôn giữ raw label.
- NER baseline: skill, organization, title, institution, degree và certificate spans.
- LLM extractor: output strict schema, chia task theo label/section, bắt buộc trả `sourceQuote` và block/page reference.

Mỗi extractor chỉ tạo `CandidateClaim`; chưa được ghi vào canonical CV cuối.

### Bước G — Evidence grounding và deterministic validation

- Align `sourceQuote` về normalized source bằng exact match; nếu có nhiều vị trí thì dùng block/page constraint, nếu vẫn mơ hồ thì review.
- Offset do code tính sau alignment, không tin offset do model phát sinh.
- Không tìm thấy quote: reject candidate với `unsupported_claim`, tuyệt đối không auto-accept.
- Validate tham chiếu: skill claim phải trỏ tới employment/project claim hợp lệ khi khai báo kinh nghiệm.
- Date parser kiểm tra thứ tự, future date, `isCurrent/endDate`, overlap và precision.
- `durationMonths` và `totalExperienceMonths` là derived fields; service tính từ intervals đã validated và ghi algorithm version.
- Taxonomy resolver chỉ map khi đạt threshold đã calibration; không chắc thì giữ raw concept và route review.

### Bước H — Confidence và human review

Không sử dụng confidence tự khai báo của LLM làm xác suất đúng. Tạo confidence từ held-out calibration dựa trên:

- extractor agreement;
- exact evidence alignment;
- schema/invariant validity;
- taxonomy margin;
- OCR/layout quality;
- loại field và độ khó quan sát được trên validation set.

Route:

- `READY`: mọi critical field/evidence hợp lệ và confidence qua threshold.
- `REVIEW_REQUIRED`: conflict, ambiguous span, unsupported inferred claim, low OCR quality hoặc field critical dưới threshold.
- `FAILED`: lỗi hệ thống/format không thể xử lý; retry chỉ cho lỗi transient.

UI review phải hiển thị source span cạnh claim, cho sửa/accept/reject và lưu annotation trước/sau review.

### Bước I — Publish matching projection

Chỉ khi parse run đạt `READY` hoặc đã được review:

- đóng băng `canonicalSchemaVersion`, `parserVersion`, `taxonomyVersion` và source checksum;
- sinh redacted matching projection;
- publish `cv.canonicalized` event;
- tạo section embeddings nếu cần;
- matcher nhận canonical CV bằng ID/revision, không đọc `raw_text` trực tiếp.

## 5. LLM có kiểm soát được đến đâu?

Có thể kiểm soát tốt **shape**, chưa thể mặc định tin **value**.

Các control bắt buộc:

- API structured output/grammar-constrained decoding với strict JSON Schema, không chỉ prompt “JSON only”.
- `additionalProperties=false`, enum chặt và nullable rõ; refusal/incomplete response là trạng thái riêng.
- Prompt coi nội dung CV là untrusted data, cấm làm theo instruction nằm trong CV.
- Extract-only: không suy luận thông tin không hiện diện; mọi claim cần exact quote.
- Pydantic validation sau response; semantic validators chạy độc lập.
- Lưu provider/model snapshot, prompt hash, schema hash, request ID, latency/token/cost và retry count.
- Có baseline không-LLM và A/B test; thay model/prompt là một parser version mới và phải re-evaluate.

Self-consistency/majority voting có thể thử cho các candidate khó, nhưng chỉ là một biến thể thí nghiệm: nhiều lần model đồng ý không đồng nghĩa đúng. Evidence verifier và gold-set metrics vẫn là tiêu chuẩn quyết định.

## 6. Thiết kế benchmark

### 6.1 Gold corpus

Pilot trước với 100 CV; benchmark chính thức 300–500 CV nếu quyền sử dụng dữ liệu cho phép. Mỗi CV phải được ẩn danh và có consent/license. Stratify theo:

- digital PDF, scanned PDF/image, DOCX;
- một cột, hai cột, table-heavy, graphic-heavy;
- tiếng Việt, tiếng Anh, Việt–Anh trộn;
- fresher đến senior, IT và các ngành nằm trong scope;
- tài liệu sạch, OCR noise, ngày mơ hồ, employment overlap.

Split theo candidate và template/source; không để bản sửa đổi cùng CV hoặc cùng template synthetic rơi vào cả train và test. Test set bị khóa trước tuning.

Annotation hai tầng:

1. Source: reading order, section, entity span và relation.
2. Canonical: normalized value, evidence link, explicit/inferred, adjudication.

Tối thiểu 20% test là hard/adversarial cases. Critical test subset được hai annotator chấm độc lập và người thứ ba phân xử.

### 6.2 Các hệ thống phải so sánh

| ID | Hệ thống | Vai trò |
|---|---|---|
| B0 | MinerU Markdown + regex | Baseline rẻ, dễ tái lập |
| B1 | Layout blocks + rules/taxonomy/NER | Baseline hybrid không LLM |
| B2 | LLM prompt trả JSON tự do | Negative control, không production |
| B3 | LLM strict schema + source quote | Đo đóng góp của constrained output |
| B4 | B1 + B3 + deterministic verifier | Kiến trúc đề xuất |
| E1 | LayoutLMv3 hoặc tương đương | Chỉ thử khi layout là lỗi chính |
| E2 | Donut/OCR-free | Chỉ thử nếu OCR error propagation là lỗi chính |

Không chọn B4 trước khi có kết quả. Chọn pipeline theo metric ưu tiên precision/evidence, sau đó mới xét recall, latency và cost.

### 6.3 Metrics

- Extraction layer: CER/WER trên subset có transcription; reading-order/block accuracy; section span F1.
- Entity layer: strict và relaxed span precision/recall/F1 theo field và ngôn ngữ.
- Normalization: exact/normalized match cho date, degree, language, skill concept.
- Relation: employment–title–organization–date và skill–employment relation F1.
- Evidence: evidence coverage, exact span accuracy, token F1/IoU, unsupported-claim rate.
- Canonical: schema validity, document exact match, field completeness có phân biệt `missing` và `unknown`.
- Confidence: precision–coverage curve, Brier score/ECE; báo cáo riêng auto-accepted và reviewed samples.
- Stability: chạy lại cùng model/prompt trên một fixed subset; đo field disagreement và document consistency.
- Operations: p50/p95 latency, timeout/retry rate, token/cost mỗi CV và review minutes mỗi CV.
- Fairness/privacy: PII leakage rate và quality slices theo ngôn ngữ/layout; không dùng thuộc tính nhạy cảm để match.

### 6.4 Release gates đề xuất

Ngưỡng phải được khóa trước final test; các giá trị ban đầu sau dùng cho pilot:

- 100% output qua JSON Schema/Pydantic hoặc bị route khỏi `READY`.
- 100% evidence refs tồn tại và offsets hợp lệ.
- 0 unsupported critical claim trên test set; bất kỳ claim nào không ground được phải bị reject/review.
- Critical-field precision >= 0.98 và recall >= 0.90 trên test; báo cáo từng field, không chỉ micro-average.
- Evidence span F1 >= 0.95 cho auto-accepted critical claims.
- PII leakage = 0 trong matching payload, embedding input và application logs của test.
- Không slice chính nào thấp hơn aggregate F1 quá 10 điểm phần trăm mà không có mitigation/review route.

Đây là quality gates của dự án, không phải con số được các bài báo bảo đảm. Nếu pilot cho thấy không khả thi, thay đổi threshold phải được ghi trước khi mở final test.

## 7. Test pyramid

### Unit/property tests

- Strict schema, enum, extra fields, partial dates và ID uniqueness.
- Unicode Việt, normalization và deterministic offset mapping.
- Date overlap/duration với leap year, current role, missing precision.
- Evidence exact/multiple/no-match; property test mọi accepted span round-trip về source quote.
- Taxonomy alias collisions (`C`, `R`, `Go`, `React Native`) và bilingual aliases.

### Golden/contract tests

- Versioned fixtures: input artifact + expected canonical JSON + expected warnings.
- Provider contract: valid output, refusal, truncated response, invalid schema, timeout/rate limit.
- MinerU archive: Markdown-only, content list, middle JSON, corrupt/missing entries và incompatible version.
- Migration/backfill: old `raw_text` records không bị tự đánh dấu `READY`.

### Integration/E2E tests

```text
upload -> R2 -> Celery -> MinerU fake -> parser fake/live
       -> DB parse run -> READY/REVIEW_REQUIRED
       -> matching projection -> structured matcher
```

Kiểm tra retry/idempotency, worker crash giữa transaction, duplicate task, stale parser version, deletion/retention và quyền truy cập PII.

### Metamorphic/adversarial tests

- Đổi thứ tự section nhưng không đổi facts.
- Thêm header/footer, prompt injection, invisible text và irrelevant skills.
- Xóa dòng evidence phải làm claim biến mất hoặc thành review.
- Đổi `3 năm` thành `1 năm` phải đổi duration, không chỉ giữ skill.
- Hai cột đảo reading order; bảng employment; date Việt/Anh trộn.

### Live evaluation

- Live provider tests tách khỏi CI thường, dùng fixed model snapshot và ngân sách giới hạn.
- Shadow-run parser mới trên dữ liệu có consent; không thay canonical production record.
- Báo cáo bootstrap confidence intervals và error taxonomy, không chỉ một F1 tổng.

## 8. Persistence và state machine

Giữ `user_cvs` là aggregate, thêm `cv_parse_runs` để không ghi đè lịch sử:

```text
cv_parse_runs:
  id, cv_id, source_checksum
  extraction_version, parser_version, schema_version, taxonomy_version
  provider, model_snapshot, prompt_hash, schema_hash
  status, canonical_data, metrics, warnings
  source_artifact_key, redacted_artifact_key
  started_at, completed_at, reviewed_at, reviewer_id
```

Status parse run:

```text
QUEUED -> EXTRACTING -> SEGMENTING -> EXTRACTING_CLAIMS
       -> VALIDATING -> READY
                    -> REVIEW_REQUIRED -> READY | REJECTED
       -> FAILED_TRANSIENT | FAILED_PERMANENT
```

`user_cvs.status` chỉ phản ánh aggregate/upload lifecycle; không dùng một `DONE` cho cả OCR và canonical readiness. Final canonical record trỏ tới một immutable `parse_run_id`.

## 9. Work packages và thứ tự thực hiện

1. **WP0 — Contract/fixtures:** hoàn thiện Resume v2.1, evidence model, JSON Schema, 20 valid và 30 invalid fixtures.
2. **WP1 — MinerU artifacts:** trả/lưu `content_list` và `middle`; version adapter; benchmark native/OCR extraction.
3. **WP2 — Source/evidence:** normalized source, block mapping, deterministic extractors, PII redaction.
4. **WP3 — LLM candidate adapter:** strict output, prompt isolation, provenance, refusal/retry contract; chưa publish canonical.
5. **WP4 — Verifier/normalizer:** quote alignment, dates, relations, taxonomy, derived durations và conflict detection.
6. **WP5 — Persistence/orchestration:** migration `cv_parse_runs`, state machine, idempotent Celery chain và review event.
7. **WP6 — API/review:** parse-status endpoint, canonical view, evidence viewer, correction/adjudication audit.
8. **WP7 — Eval harness:** gold format, scorers, slices, confidence intervals, comparison B0–B4.
9. **WP8 — Rollout:** offline benchmark -> shadow -> review-only -> gated auto-READY; rollback bằng parser version.

Mỗi WP chỉ được merge khi unit/contract tests tương ứng đạt. Không nối parser mới vào quyết định matching production trước WP7.

## 10. Hồ sơ minh chứng

Nguồn, kỹ thuật áp dụng và trạng thái bằng chứng được ghi tại `DOC_AND_PLAN/tailieuthamkhao/cv-parser-evidence/`. Một kỹ thuật chỉ chuyển từ `proposed` sang `accepted` sau khi có experiment ID, dataset version, metric và kết quả tái lập trong repository.
