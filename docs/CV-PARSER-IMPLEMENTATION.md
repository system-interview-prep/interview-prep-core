# CV ingestion và parsing pipeline

Tài liệu này mô tả phần code đang thực thi từ lúc người dùng upload CV đến khi dữ liệu canonical được lưu vào PostgreSQL. Matching và scoring không thuộc pipeline này.

## Luồng thực thi

```text
POST /users/me/cvs
  -> CvFileValidator
  -> R2CvFileStorage
  -> SqlAlchemyCvUploadRepository (PENDING)
  -> CeleryCvParsePublisher
  -> CvParsingPipeline
       -> SqlAlchemyCvParseRepository (PARSING)
       -> R2ObjectStorage.read
       -> MinerUDocumentExtractor
       -> R2ObjectStorage.write_json (artifact bất biến theo checksum)
       -> build_source_document
       -> DeterministicResumeParser
            -> TaxonomySkillExtractor
            -> CefrLanguageExtractor
            -> RegexIdentityExtractor
       -> Pydantic CanonicalResume validation
       -> SqlAlchemyCvParseRepository.complete
            -> raw_text
            -> parsed_data JSONB
            -> parse_source
            -> DONE
```

## Ranh giới trách nhiệm

| Thành phần | Một trách nhiệm chính |
|---|---|
| `router.py` | Chuyển HTTP request/exception sang application call/HTTP response |
| `CvUploadService` | Điều phối upload, metadata và phát task |
| `CvFileValidator` | Kiểm tra kích thước, extension, declared MIME và magic bytes |
| `CvParsingPipeline` | Điều phối một parse attempt qua các port |
| `MinerUDocumentExtractor` | Chuyển MinerU API thành contract `DocumentArtifacts` trung lập |
| `build_source_document` | Tạo source text/block theo page, reading order và bounding box |
| `EvidenceMapper` | Ánh xạ offset/quote về bằng chứng nguồn |
| `TaxonomySkillExtractor` | Nhận diện skill alias chính xác |
| `CefrLanguageExtractor` | Nhận diện mức CEFR được ghi rõ |
| `RegexIdentityExtractor` | Tách email/phone khỏi canonical matching data |
| `SqlAlchemyCvParseRepository` | Quản lý SQL và transaction của trạng thái parse |

Các application service chỉ phụ thuộc `Protocol` nhỏ. R2, MinerU, Celery và SQLAlchemy là adapter được lắp ở HTTP router hoặc Celery task. Có thể thay từng adapter bằng fake trong test mà không sửa nghiệp vụ.

## Contract lưu trữ hiện tại

`user_cvs.parsed_data` chỉ lưu `CanonicalResume`, không lưu email/phone. Dữ liệu gồm version tài liệu/parser, claims, evidence, warnings và trạng thái canonical. Artifact MinerU được lưu theo key có checksum để phục vụ audit và re-parse.

`DONE` hiện chỉ cho biết attempt đã ghi thành công vào aggregate `user_cvs`. `parsedData.parsing.status` mới cho biết dữ liệu canonical là `ready` hay `review_required`. Baseline deterministic hiện luôn trả `review_required` vì chưa có extractor đáng tin cậy cho employment, education, projects và certifications.

## Phạm vi đã kiểm chứng

- Upload validation chạy trước storage và database.
- Upload trùng checksum không ghi file hoặc phát task lần nữa.
- Queue dispatch thất bại được ghi thành trạng thái `FAILED`.
- MinerU artifact giữ content list/layout và được lưu trước parsing.
- Mọi skill/language claim được liên kết tới evidence span hợp lệ.
- Pipeline thất bại không gọi persistence completion.
- Canonical output qua Pydantic validation trước khi repository nhận dữ liệu.

Chưa được tuyên bố production-ready cho extraction quality. Bước kế tiếp là thêm extractor theo section, `cv_parse_runs`, retry classification và benchmark bằng gold corpus.
