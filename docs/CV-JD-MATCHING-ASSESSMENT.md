# Đánh giá CV–JD matching hiện tại

Đánh giá ngày 08-09-2026, đối chiếu mã nguồn `interview-prep-core` với [tài liệu khảo sát và Canonical Schema v2](../../DOC_AND_PLAN/tailieuthamkhao/CV-JD-MATCHING.md).

## Kết luận

Hệ thống đã có nền tảng vận hành tốt cho upload, OCR bất đồng bộ, lưu raw text và chạy matching ngoài event loop. Tuy vậy, phần matching hiện tại là **so khớp ngữ nghĩa toàn văn bằng embedding cosine**, chưa phải matching CV–JD theo requirement. Chưa nên dùng kết quả làm quyết định tự động `PASS`/`FAIL`, benchmark chính thức, hay tuyên bố độ chính xác.

| Khía cạnh | Mức hiện tại | Nhận định |
|---|---:|---|
| Ingest/OCR | 6/10 | Upload, checksum và Celery/MinerU đã có; kết quả là `raw_text`/Markdown. |
| CV canonical data | 2/10 | `user_cvs` chỉ lưu raw text; chưa có JSON CV có kiểu dữ liệu, version, evidence hoặc vùng PII tách biệt. |
| JD canonical data | 4/10 | Có `structured_data` JSONB và metadata, nhưng API cho phép `dict` bất kỳ; parser hiện chỉ ghi raw text, chưa tạo requirements nguyên tử. |
| Validation | 2/10 | `MatchRequest` kiểm tra text; không có validation schema CV/JD, date, enum, ID hay evidence offset. |
| Matching | 2/10 | `MatchingFacade` cố định external embedding + cosine; không đánh giá từng requirement hay trạng thái `met/not_met/unknown`. |
| Giải thích/audit | 2/10 | Trả metadata và score tổng, không có evidence từ CV/JD cho quyết định. |
| An toàn PII | 3/10 | Email/điện thoại không được trích xuất riêng, nhưng toàn bộ CV raw text (có PII) được gửi thẳng vào embedder. |
| Benchmark | 1/10 | Chưa có canonical frozen schema, nhãn requirement-level, split chống leakage hoặc metrics phù hợp. |

## Bằng chứng từ mã nguồn

- `src/workers/tasks/cv.py` và `src/workers/tasks/job_description.py` chỉ lưu OCR vào `raw_text`; không có bước canonicalization hay schema validation.
- `src/modules/matching/schemas.py` nhận `resumeText` và `jobDescription` là chuỗi. `src/modules/matching/facade.py` bỏ qua danh sách thuật toán cũ và luôn gọi semantic matcher.
- `src/modules/matching/semantic.py` nhúng trọn CV và JD rồi trả một cosine score duy nhất. Vì vậy một CV nhiều từ khóa vẫn có thể đạt điểm cao dù thiếu điều kiện bắt buộc; cũng không phân biệt thiếu thông tin với không đáp ứng.
- `src/modules/scoring/router.py` hiện chỉ tìm các khóa điểm ở cấp đầu của kết quả (`overall_score`, `score`, ...), trong khi semantic matcher đặt điểm tại `combined_results[0].combined_score`. Do đó endpoint `/ai/score-cv-jp` nhận `0.0`, lưu `0%`, và luôn trả `FAIL`. Đây là lỗi chức năng P0 độc lập với hạn chế mô hình.
- `structured_data` của JD có chỗ lưu trữ và patch, nhưng đang là `dict` tự do; đây là điểm bắt đầu phù hợp để chuyển sang JD canonical v2 sau khi có model validation.

## Khoảng cách với Canonical Schema v2

| Cần có | Hiện trạng | Hướng tích hợp |
|---|---|---|
| CV/JD versioned, typed | raw text và JSON tùy ý | Pydantic models v2, `schemaVersion`, strict enums và migrations JSONB. |
| Requirement nguyên tử | cosine toàn văn | Parser JD sinh `Requirement[]` gồm type, priority, operator, value/unit, source span. |
| Evidence truy vết | không có | Lưu evidence refs/spans xác thực theo raw text; mọi `met` cần evidence. |
| PII isolation | raw CV đi vào embedding | Tách resume identity, redaction trước embedding/matching, kiểm thử không rò PII. |
| Kết quả theo requirement | score đơn | `requirementResults` với `met`, `not_met`, `unknown`, `not_applicable`; `unknown` không tự suy thành `not_met`. |
| Đánh giá đáng tin cậy | chưa có | Pilot người chấm theo kế hoạch: parser F1, requirement macro-F1/must-have recall, evidence metrics, nDCG/Recall@K và agreement. |

## Review Canonical Schema v2: cần hoàn thiện trước khi code

Schema v2 đi đúng hướng, nhưng ví dụ JSON hiện là **logical model**, chưa đủ chặt để làm API/database contract. Nên chốt các điểm sau trong bản v2.1 trước khi tạo migration hay parser.

| Hạng mục | Điểm chưa rõ trong v2 | Cải tiến cần chốt |
|---|---|---|
| ID và taxonomy | `skills[].id` đang vừa giống ID của claim vừa giống taxonomy ID; `scheme: internal` không có version. | Tách `claimId`/`employmentId` (ID record) khỏi `conceptId`, `scheme`, `taxonomyVersion` (ID chuẩn hóa). Lưu `rawLabel`, `normalizedLabel` và aliases đã áp dụng. |
| Requirement typed union | Một object chung có các field chỉ áp dụng cho `skill` hoặc `language`; `type=education`, `location`, `certification` chưa có payload cụ thể. | Dùng discriminated union theo `type`: `SkillRequirement`, `ExperienceRequirement`, `EducationRequirement`, `LanguageRequirement`... Mỗi type chỉ cho phép field hợp lệ qua `extra="forbid"`. |
| Giá trị so sánh | `gte/lte/equal` được liệt kê nhưng không có một value/unit/precision thống nhất; `minimumExperienceMonths` chỉ phù hợp một phần skill requirement. | Thống nhất `constraint: {operator, value, unit}`; đặt ràng buộc type-specific, ví dụ tháng là integer không âm, level là enum, degree là danh sách có thứ bậc. |
| Logic nhóm | Các requirements trong mảng mặc định là AND; schema chưa biểu đạt “Java **hoặc** Kotlin”, “AWS **và** Azure certificate”, hay điều kiện thay thế. | Thêm `requirementGroupId` và node logic `all_of/any_of`, hoặc tree `RequirementExpression`; không dùng `operator=any_of` mà không có operands rõ. |
| Thời gian | Ví dụ dùng `"2019"` và `"2023-01"` dù validation nói ISO-8601; chưa có precision, timezone hoặc quy tắc khoảng thời gian hiện tại. `durationMonths` có thể lệch/nhập sai. | Dùng `PartialDate {value, precision: year|month|day}`; `endDate: null` chỉ khi `isCurrent=true`; tính `durationMonths` ở service, không nhận như nguồn sự thật. Quy định rõ có/không khử overlap khi tính tổng kinh nghiệm. |
| Claim skill và kinh nghiệm | `skills[].experienceMonths` gộp mọi lần dùng skill, nhưng không cho biết từng khoảng thời gian/evidence; `proficiency=advanced` có thể là suy đoán của parser. | Skill là claim có `usageRefs` tới employment/project, `assertionSource` (`explicit`/`inferred`) và confidence từng claim. Chỉ tính experience theo các khoảng đã xác minh. |
| Evidence | `charStart/charEnd` dễ hỏng nếu OCR/normalization thay đổi; `sourceSpan` của JD khác cấu trúc `evidence` của CV; không có raw-document revision. | Một model `EvidenceSpan` dùng chung: `documentId`, `documentSha256`, `parserVersion`, `section`, offsets theo **raw text bất biến**, page và tùy chọn bounding box. Mọi fact/requirement/recommendation chỉ reference `evidenceId`. |
| Độ bất định | Chỉ có confidence tổng cho parsing; `null`, missing và extraction failure có thể bị lẫn. | Thêm `extractionStatus` và confidence theo field/claim; warnings có `code`, `path`, `message`. `unknown` chỉ là kết quả matching, không thay thế trạng thái parser. |
| CV sections chưa định nghĩa | `projects`, `certifications` là mảng nhưng không có item schema; education không có evidence refs; achievements/metrics không có evidence riêng. | Định nghĩa đầy đủ `ProjectEntry`, `CertificationEntry`, `EducationEntry`; các fact dùng cho matching phải có evidence refs hoặc được đánh dấu `inferred`. |
| Ngôn ngữ và chứng chỉ | CEFR tốt nhưng CV thường ghi IELTS/TOEIC hoặc “giao tiếp tốt”; chưa có conversion/policy. | Lưu credential gốc và `levelEvidence`; mapping sang CEFR phải mang `mappingPolicyVersion`, không tự coi mô tả tự do là B2. |
| PII và provenance | Có object PII riêng nhưng chưa định nghĩa redacted projection, quyền truy cập hay khả năng evidence vô tình chứa PII. | Tạo ba payload riêng: encrypted identity, raw document (restricted), matching projection (redacted). Matcher/embedding chỉ nhận projection; audit evidence kiểm tra quyền trước khi hiển thị. |
| Matching response | `overallScore` và factor scores chưa quy định thang điểm, trọng số, threshold hoặc policy version; `reason` là text tự do. | Thêm `matchingPolicyVersion`, `scoreScale`, weighting/threshold snapshot và reason codes. `recommendation` phải được suy ra từ policy có version, không từ LLM tự do. |

### Quyết định thiết kế nên giữ

- Tách PII khỏi representation dùng để match.
- Requirement nguyên tử, có priority, operator và source evidence.
- Bốn trạng thái `met`, `not_met`, `unknown`, `not_applicable`; không phạt `unknown` như một fact không đạt.
- Raw document được giữ để audit/re-parse, còn canonical schema có version riêng.

### Thứ tự chốt v2.1

1. Chốt envelope chung: document identity/revision, taxonomy references, partial date, evidence và parser warnings.
2. Chốt discriminated union cho tất cả loại requirement cùng logic AND/OR.
3. Chốt CV sub-models cho skill claim, employment, project, education, certification và language.
4. Chốt matching request/response cùng policy-version, reason code và score semantics.
5. Viết JSON Schema/Pydantic models và bộ valid/invalid fixtures trước khi đổi database hoặc prompt parser.

Việc này giúp parser, database và matcher cùng dựa trên một contract testable; nếu bắt đầu từ ví dụ JSON hiện tại, mỗi layer rất dễ tự diễn giải khác nhau.

## Ưu tiên thực hiện

1. **P0 — sửa scoring contract:** lấy `combined_results[0].combined_score` (hoặc thống nhất semantic matcher trả `overallScore`), bổ sung test cho score và nhánh PASS/FAIL. Không công bố endpoint này là kết quả đánh giá ứng viên trước khi sửa.
2. **P0 — thiết kế contract v2:** thêm Pydantic request/response models cho CV, JD, `Requirement`, evidence và matching result; dùng `extra="forbid"`, versioning và validation offset/date.
3. **P1 — thay parser text-only:** giữ raw OCR để audit nhưng xây task canonicalization có strict validation; lưu PII tách khỏi payload đưa vào matcher.
4. **P1 — matcher requirement-level:** thực hiện luật cho skill, thời lượng kinh nghiệm, ngôn ngữ, degree và must-have trước; embedding/BM25 chỉ là tín hiệu bổ sung cho responsibility/semantic similarity.
5. **P2 — calibration và benchmark:** triển khai pilot 300–600 pairs, hai annotator độc lập, hard negatives 20–30%, split theo candidate/job và khóa test set trước khi tuning.

## Tiêu chí hoàn thành giai đoạn v2

Chỉ coi matching sẵn sàng benchmark khi payload CV/JD v2 validate nghiêm ngặt, mỗi requirement có trạng thái và evidence, PII không đi vào embedding, endpoint scoring đã có contract-test, và pilot được báo cáo theo các metrics trong tài liệu nghiên cứu. Điểm tổng khi đó là công cụ hỗ trợ review, không phải quyết định tuyển dụng tự động.
