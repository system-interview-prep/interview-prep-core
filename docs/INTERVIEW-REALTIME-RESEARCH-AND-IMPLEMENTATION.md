# Thiết kế interview realtime có kiểm soát

**Trạng thái:** research/design baseline v1 — 2026-09-18  
**Phạm vi:** câu hỏi thích nghi, ngân sách thời gian, STT Việt/Anh, latency và chấm điểm.  
**Mục tiêu:** một phiên luyện phỏng vấn nhất quán, giải thích và đo lường được; không phải công cụ tự động ra quyết định tuyển dụng.

## 1. Kết luận kiến trúc

Không dùng một prompt hội thoại tự do để điều hành toàn bộ phiên. Backend phải là nguồn sự thật và điều phối bằng state machine. LLM chỉ làm các tác vụ hẹp: diễn đạt câu hỏi từ blueprint đã duyệt, phân tích câu trả lời theo rubric cố định và viết feedback từ các điểm đã tính.

```text
audio browser
  -> WebRTC/WebSocket STT streaming
  -> partial transcript (chỉ hiển thị)
  -> final utterance (lưu bất biến)
  -> fast controller: kết thúc / hỏi rõ / hỏi sâu / sang câu kế
  -> phát câu hỏi kế tiếp bằng TTS streaming

final utterance
  -> scoring worker song song
  -> criterion evidence + deterministic aggregation
  -> session report
```

Hai đường xử lý phải tách biệt:

- **Realtime lane:** VAD, STT, quyết định bước kế tiếp và TTS. Không chờ báo cáo chấm điểm dài.
- **Evaluation lane:** kiểm tra evidence, chấm từng criterion, hiệu chỉnh điểm và tạo feedback. Có thể hoàn tất trễ hơn một lượt hoặc sau phiên.

Legacy endpoint/module tạo danh sách câu hỏi tự do đã được gỡ bỏ để xây lại theo question-bank plan. Hiện chưa có runtime question controller thay thế. `voice/router.py` vẫn chờ tuần tự text generation rồi mới chờ toàn bộ MP3; frontend dùng Web Speech API nên provider và hành vi khác nhau theo browser; timer 45 giây chỉ ở client và có thể lệch/reset.

## 2. Hợp đồng phiên và state machine

Mỗi phiên đóng băng `InterviewPlanSnapshot`; không thay policy giữa phiên.

```json
{
  "planVersion": "interview-plan-v1",
  "rubricVersion": "technical-rubric-v1",
  "locale": "vi-VN",
  "secondaryLocale": "en-US",
  "durationSeconds": 1200,
  "answerReserveSeconds": 120,
  "competencies": [
    {"id": "backend_api", "weight": 0.35, "minQuestions": 2, "maxQuestions": 3},
    {"id": "database", "weight": 0.25, "minQuestions": 1, "maxQuestions": 2}
  ],
  "difficultyPolicy": "adaptive-3-band-v1"
}
```

```text
PLANNED -> INTRO -> ASKING -> LISTENING -> FINALIZING_UTTERANCE
                                  ^                 |
                                  |                 v
                              ASKING <- DECIDING <- SCORING_QUEUED
                                                   |
                                                   v
                                               WRAP_UP -> COMPLETED
```

Server phát `serverNow`, `sessionDeadlineAt`, `questionDeadlineAt` trong mọi event. Client chỉ render đồng hồ từ deadline; quyền timeout thuộc server. Mỗi lệnh có `eventId`, `turnId`, `sequence` và idempotency key để reconnect không tạo câu hỏi/điểm hai lần.

Các bất biến:

1. Chỉ một turn ở trạng thái `LISTENING`.
2. Một final transcript cho mỗi `utteranceId`; partial không làm evidence cuối.
3. Không sinh follow-up nếu thời gian còn lại nhỏ hơn `wrapUp + nextQuestionMinimum`.
4. Không quá hai follow-up liên tiếp cho một câu gốc.
5. Mỗi competency đạt `minQuestions` trước khi dùng thời gian cho câu bổ sung.
6. Câu hỏi và rubric được gắn ID trước khi hỏi; không tạo tiêu chí sau khi đã thấy câu trả lời.

## 3. Quy trình câu hỏi từ dễ đến khó

Không tăng độ khó cứng nhắc. Dùng ba band `FOUNDATION`, `APPLIED`, `DEEP_DIVE` và controller thích nghi có giới hạn.

### 3.1 Lập agenda

1. Tính usable time: `duration - intro - wrap_up - safety_buffer`.
2. Phân bổ theo trọng số competency, sau đó bảo đảm `minQuestions`.
3. Với mỗi competency, chuẩn bị blueprint: objective, prompt, rubric, expected duration, allowed follow-ups và difficulty.
4. Xếp agenda: một câu foundation để hiệu chỉnh, các câu applied phủ competency chính, deep-dive chỉ khi còn thời gian và evidence tốt.
5. Validate trước phiên: tổng `expectedDuration + transitionBudget` không vượt usable time và mọi criterion quan sát được từ câu hỏi.

### 3.2 Chuyển độ khó

Dùng điểm tạm thời theo criterion, không dùng “cảm giác” của LLM:

- Lên một band khi hai câu gần nhất có `provisionalScore >= 0.75`, không thiếu criterion critical và STT confidence đủ dùng.
- Giữ band khi điểm trong `[0.45, 0.75)` hoặc evidence chưa đủ.
- Hạ một band/hỏi làm rõ khi `< 0.45`; chỉ hỏi lại một lần, sau đó chuyển competency.
- Không nhảy quá một band mỗi turn.
- Nếu STT confidence thấp/code-switch bất thường, hỏi xác nhận transcript; không hạ điểm năng lực.

Controller chỉ chọn một action có schema:

- `CLARIFY`: câu trả lời mơ hồ hoặc transcript không chắc.
- `PROBE_EVIDENCE`: yêu cầu ví dụ, quyết định, trade-off hoặc kết quả đo được.
- `DEEPEN`: đã đạt core criteria và còn ngân sách.
- `NEXT_COMPETENCY`: đủ evidence hoặc hết ngân sách topic.
- `WRAP_UP`: chạm deadline/safety buffer.

LLM có thể viết câu chữ, nhưng action, target criterion và time cap do code quyết định.

## 4. Kiểm soát thời gian

Mặc định đề xuất cho phiên kỹ thuật 20 phút:

| Hạng mục | Ngân sách |
|---|---:|
| Intro + kiểm tra âm thanh | 45 s |
| 6 câu gốc, trung bình 120 s | 720 s |
| Tối đa 3 follow-up, trung bình 60 s | 180 s |
| Chuyển lượt/latency, 9 lần × 4 s | 36 s |
| Candidate question + wrap-up | 120 s |
| Safety buffer | 99 s |

Mỗi blueprint có `softAnswerSeconds` và `hardAnswerSeconds`. Soft limit chỉ cảnh báo; hard limit để server đóng utterance và chuyển bước. Chỉ cấp grace 10–15 giây một lần nếu ứng viên đang nói và session budget còn đủ.

Scheduler tính lại sau mỗi turn:

```text
remaining = sessionDeadline - now - wrapUpReserve
minimum_required = sum(min_duration of uncovered competencies)
follow_up_allowed = remaining >= minimum_required + follow_up_duration + safety_margin
```

Nếu thiếu thời gian, bỏ deep-dive trước rồi follow-up; không bỏ coverage bắt buộc. Pre-generate ít nhất hai câu kế tiếp và invalidate cache khi controller đổi nhánh.

## 5. Speech-to-text Việt/Anh

### 5.1 Khuyến nghị

Ưu tiên STT streaming server-managed qua WebRTC/WebSocket, với adapter provider-neutral. Web Speech API hiện tại chỉ là fallback vì không tạo pipeline đồng nhất để benchmark, quan sát và replay.

OpenAI Realtime transcription hỗ trợ WebRTC/WebSocket, incremental delta/completed events, language/prompt và VAD; hợp lý để prototype vì backend đã dùng OpenAI. Deepgram là candidate đối chứng đáng benchmark vì có interim/final, endpointing cấu hình được và key-term prompting. Không chọn provider từ tài liệu quảng bá: phải chạy corpus giọng Việt/Anh của dự án.

### 5.2 Language policy

- Dùng BCP-47 `vi-VN`, `en-US`; không dùng chuỗi tự do `Vietnamese`/`English` trong contract nội bộ.
- `primaryLocale` do người dùng chọn; cho phép thuật ngữ tiếng Anh trong câu trả lời tiếng Việt.
- Prompt/context STT chỉ chứa 20–50 thuật ngữ liên quan JD/CV, không nhét toàn bộ tài liệu.
- Lưu transcript nguyên bản và normalized riêng. Scoring dùng nguyên bản làm evidence; normalized chỉ hỗ trợ search.
- Partial được phép sửa; final là append-only. Correction có provenance (`USER_CONFIRMED`, `PROVIDER_REVISION`, `NORMALIZER`).
- Nếu `noSpeech`, confidence thấp hoặc final quá ngắn: xác nhận lại, không suy ra câu trả lời sai.

Khởi điểm thử nghiệm: mono 16/24 kHz, chunk 20–40 ms, semantic/server VAD; silence 500–800 ms cho câu ngắn và 900–1,200 ms cho câu kỹ thuật dài. Đây là tham số cần tune. Luôn có nút “Tôi trả lời xong”, và barge-in phải dừng TTS khi phát hiện ứng viên nói.

### 5.3 Benchmark bắt buộc

Tạo bộ audio consented, tách speaker, không giữ PII không cần thiết; gồm:

- vi-VN thuần, English có accent Việt và code-switch Việt–Anh;
- mic laptop/headset/mobile;
- phòng yên, quạt/ồn đường, mạng có jitter;
- thuật ngữ backend/frontend/data/DevOps và tên công nghệ trong JD.

Đo `WER`, `CER`, entity/keyword recall, punctuation-insensitive WER, first-partial latency, finalization latency và false endpoint rate. Chỉ promote provider/config nếu không regress quality gate trên slice quan trọng.

## 6. Latency budget và giảm trễ

Mục tiêu từ lúc ứng viên ngừng nói đến lúc nghe âm đầu tiên của interviewer:

| Thành phần | p50 target | p95 gate |
|---|---:|---:|
| VAD/end-of-turn | 450 ms | 900 ms |
| STT final sau endpoint | 250 ms | 700 ms |
| Controller + DB/event | 50 ms | 150 ms |
| LLM time-to-first-token | 350 ms | 900 ms |
| TTS time-to-first-audio | 250 ms | 700 ms |
| **Tổng không chồng lấp** | **1.35 s** | **3.35 s** |

Đây là acceptance target, chưa phải số đo hiện tại. Ghi timestamp monotonic cho: `audio_first`, `audio_last`, `vad_stop`, `stt_partial_first`, `stt_final`, `decision_start/end`, `llm_first_token`, `tts_first_byte`, `playback_start`.

Ưu tiên:

1. Stream audio thay vì ghi xong rồi upload.
2. Dùng partial để prefetch; chỉ commit quyết định/scoring trên final.
3. Pre-generate hai câu kế, chọn cache trong phần lớn turn.
4. Stream TTS và phát frame đầu, không base64 toàn bộ MP3 trong JSON.
5. Giữ kết nối provider nóng, connection pool và region gần người dùng.
6. Context ngắn, structured output nhỏ; gửi summary + evidence IDs thay vì toàn transcript.
7. Chạy scoring chi tiết bất đồng bộ; realtime chỉ tính provisional coverage.
8. Cho phép barge-in/cancel.

Không hạ VAD silence quá thấp chỉ để đẹp latency: false endpoint sẽ cắt câu và làm xấu WER/scoring.

## 7. Rubric chấm điểm rõ ràng

Rubric được version và khóa trước khi hỏi:

```json
{
  "rubricId": "backend_api_applied_01",
  "version": "1.0.0",
  "criteria": [
    {"id": "diagnosis", "weight": 0.30, "critical": true, "anchors": {"0": "không xác định", "1": "nêu chung", "2": "có phương pháp", "3": "có phương pháp và evidence"}},
    {"id": "tradeoffs", "weight": 0.25, "critical": false, "anchors": {"0": "không có", "1": "liệt kê", "2": "so sánh", "3": "chọn và biện minh"}},
    {"id": "verification", "weight": 0.25, "critical": true, "anchors": {"0": "không kiểm chứng", "1": "metric mơ hồ", "2": "metric phù hợp", "3": "baseline, target và rollback"}},
    {"id": "communication", "weight": 0.20, "critical": false, "anchors": {"0": "không hiểu được", "1": "rời rạc", "2": "rõ", "3": "rõ và súc tích"}}
  ]
}
```

Mỗi criterion chấm ordinal `0..3`, kèm `evidenceSpans`, `reasonCode` (`SUPPORTED`, `PARTIAL`, `CONTRADICTED`, `NOT_MENTIONED`, `UNSCORABLE_STT`), `confidence`, `reviewRequired`, model/prompt/rubric version.

Không cho LLM trả một điểm tổng tự do. Code validate schema và tính:

```text
criterion_normalized = level / 3
question_score = sum(weight * criterion_normalized) / sum(scored weights)
coverage = sum(scored weights) / sum(all weights)
```

`UNSCORABLE_STT` là unknown và giảm coverage, không phải 0. Criterion critical mức 0 có thể cap điểm câu, nhưng policy phải versioned. Điểm phiên chỉ hiển thị khi coverage vượt threshold; nếu không trả `INSUFFICIENT_EVIDENCE`.

Không trừ điểm kỹ thuật vì accent, tốc độ nói hoặc lỗi ngữ pháp. `communication` chỉ đo cấu trúc/độ rõ nội dung và báo riêng. Fluency/language chỉ là rubric riêng nếu người dùng chủ động chọn luyện ngôn ngữ.

Kiểm định:

- Hai người chấm độc lập trên golden set; đo weighted Cohen's kappa theo criterion và ICC/MAE tổng điểm.
- Báo agreement theo `vi`, `en`, code-switch, difficulty và competency.
- LLM grader không thấy identity/protected attributes; audit chênh lệch với human label.
- Gate đề xuất: weighted kappa >= 0.70 cho critical criteria, MAE <= 0.50 trên thang 0–3, evidence precision >= 0.90, coverage >= 0.85.
- Không dùng điểm để xếp hạng tuyển dụng; đây là feedback luyện tập và luôn hiển thị evidence/cách tính.

## 8. Data model và API

Không tiếp tục dồn transcript vào `video_calls.metadata`. Bổ sung:

- `interview_plan_snapshots`: policy/rubric/language/time snapshot.
- `interview_turns`: blueprint, action, difficulty, deadlines và state.
- `transcript_segments`: partial/final, locale, timestamps, confidence, provider metadata.
- `answer_evaluations`: criterion levels, evidence spans, unknown/review flags.
- `interview_events`: append-only event log phục vụ replay/audit.
- `interview_latency_spans`: milestone timestamps và trace ID.

```text
POST /interview/sessions                  tạo plan snapshot
POST /interview/sessions/{id}/start       deadline + turn đầu
POST /interview/sessions/{id}/stt-token   token ngắn hạn, scope một session
WS   /interview/sessions/{id}/events      state/transcript/turn events
POST /interview/turns/{id}/complete       manual end-of-answer, idempotent
POST /interview/sessions/{id}/finish      đóng phiên và queue report
GET  /interview/sessions/{id}/report      rubric + evidence + coverage
```

Nếu browser kết nối thẳng provider, backend chỉ cấp ephemeral token; không đưa API key dài hạn xuống client. Audio retention mặc định tắt/ngắn hạn; transcript có consent, encryption, delete/export và retention policy.

## 9. Kế hoạch triển khai

### P0 — contract và phép đo

- Schema plan/turn/rubric/event và server-authoritative deadline.
- Instrument latency; dashboard p50/p95 theo locale/browser/provider.
- Bộ 100–200 utterance Việt/Anh/code-switch và human rubric seed set.
- Giữ UI hiện tại nhưng không coi Web Speech transcript là ground truth đánh giá.

### P1 — vertical slice realtime

- Phiên 3 câu, STT streaming, final append-only, manual end-turn.
- Question blueprint cố định; chưa adaptive bằng LLM.
- TTS streaming, barge-in, reconnect/idempotency.
- Criterion aggregation deterministic với evidence.

### P2 — controlled adaptation

- Controller rule-based cho `CLARIFY/PROBE/DEEPEN/NEXT/WRAP_UP`.
- Pre-generation/cache hai câu tiếp.
- Async grader; uncertainty/coverage và review state.

### P3 — benchmark và rollout

- A/B provider/config trên corpus consented.
- Shadow scoring so với human, calibration và slice analysis.
- Canary theo locale; rollback khi WER, false endpoint hoặc p95 latency vượt gate.

## 10. Definition of Done

- 100% phiên tôn trọng server deadline trong sai số 1 giây và dành đủ wrap-up reserve.
- 100% câu có objective, difficulty, rubric version trước khi hỏi.
- Không duplicate turn/evaluation sau reconnect.
- p95 stop-speech → first AI audio <= 3.35 giây trên mạng thử nghiệm đã định nghĩa.
- STT đạt gate WER/CER/keyword recall riêng cho vi/en/code-switch; threshold chốt sau baseline.
- Điểm tái tính deterministic từ criterion, có evidence và coverage.
- Human agreement đạt gate và không regress trên slice ngôn ngữ/mic/noise.
- Người dùng xem/sửa transcript trước feedback cuối và xóa được dữ liệu phiên.

## 11. Nên dùng RAG hay chỉ database cho câu hỏi?

### 11.1 Kết luận

Chọn **hybrid có kiểm soát**, nhưng database question bank là nguồn sự thật:

```text
PostgreSQL question bank + deterministic constraints
                |
                v
       tập ứng viên hợp lệ (20–100 item)
                |
                +--> SQL ranking đủ tốt: chọn trực tiếp
                |
                +--> cần hiểu ngữ nghĩa CV/JD/answer: hybrid retrieval/rerank
                                  |
                                  v
                         chọn question_id đã duyệt
                                  |
                                  v
                     LLM chỉ diễn đạt/follow-up có ràng buộc
```

Không dùng “RAG sinh câu hỏi tự do” làm đường mặc định. RAG vốn kết hợp retrieval với generation để đưa tri thức ngoài tham số mô hình vào đầu ra và tăng provenance/specificity; lợi ích này phù hợp với cá nhân hóa theo JD/CV hoặc tài liệu kỹ thuật. Nhưng yêu cầu cốt lõi của interview là content coverage, difficulty, thời gian, rubric và item exposure — đây là bài toán chọn item có ràng buộc, không phải chỉ là semantic similarity. Nghiên cứu computerized adaptive testing cũng tách rõ content balancing, item-selection criterion và exposure control.

### 11.2 Dùng database thuần khi nào?

PostgreSQL có cấu trúc là lựa chọn mặc định khi:

- đã biết competency/topic/difficulty/job family;
- câu hỏi và rubric đã được chuyên gia duyệt;
- pool sau filter còn nhỏ (ví dụ vài chục hoặc vài trăm item);
- cần latency ổn định, reproducibility và audit;
- cần bảo đảm quota competency, không hỏi trùng, exposure cap và time budget.

Query nên lọc cứng trước:

```sql
SELECT q.id
FROM interview_question_blueprints q
WHERE q.status = 'APPROVED'
  AND q.locale = :locale
  AND q.competency_id = :competency_id
  AND q.difficulty_band = :difficulty
  AND q.expected_seconds <= :remaining_topic_seconds
  AND NOT EXISTS (
      SELECT 1 FROM interview_turns t
      WHERE t.session_id = :session_id AND t.question_id = q.id
  )
  AND q.exposure_rate_30d < q.max_exposure_rate
ORDER BY q.calibration_quality DESC,
         q.last_used_at ASC NULLS FIRST
LIMIT 20;
```

Sau đó controller chọn có seed và lưu `selectionPolicyVersion`, candidate IDs, feature values và reason codes. Cùng snapshot + seed phải replay được cùng kết quả.

Với MVP, database thuần tốt hơn RAG vì chưa có đủ dữ liệu để chứng minh embedding similarity tương ứng với “câu hỏi tốt”. Vector similarity cao không bảo đảm đúng difficulty, rubric rõ, thời gian phù hợp hoặc coverage cân bằng.

### 11.3 RAG có giá trị ở đâu?

Chỉ bật semantic/hybrid retrieval cho các trường hợp mà exact metadata không đủ:

1. **JD → competency mapping:** một requirement diễn đạt tự do cần tìm blueprint gần nghĩa.
2. **CV/JD personalization:** tìm câu đã duyệt liên quan dự án/công nghệ cụ thể, nhưng không chép PII hoặc khẳng định điều CV không chứng minh.
3. **Follow-up:** tìm các probe đã duyệt tương ứng với criterion còn thiếu trong câu trả lời.
4. **Scoring support:** lấy rubric, reference facts hoặc exemplar đúng `question_id`; đây chủ yếu là keyed lookup, vector retrieval chỉ bổ sung kiến thức liên quan.
5. **Large bank:** khi pool lên hàng chục nghìn item và taxonomy không đủ chi tiết, semantic ranking giúp thu hẹp ứng viên.

Ngay cả các trường hợp trên, output retrieval chỉ là **candidate question IDs**. Controller vẫn áp hard constraints và lấy prompt/rubric canonical từ bảng quan hệ. Không đưa một chunk text bất kỳ từ vector store thẳng vào phiên rồi chấm theo rubric được sinh sau.

### 11.4 RAG không nên dùng ở đâu?

- Không dùng để quyết định thời gian, difficulty transition hoặc coverage bắt buộc.
- Không dùng top-1 vector similarity làm câu hỏi cuối cùng.
- Không dùng retrieval score như evidence rằng câu hỏi đúng mức độ khó.
- Không dùng LLM tạo cả câu hỏi và rubric trong cùng lượt rồi tự chấm — đó là circular evaluation.
- Không truy hồi toàn bộ transcript/CV khi keyed lookup theo `question_id`, `rubric_id` đã đủ.
- Không gọi embedding/vector search trên critical path nếu hai câu kế tiếp đã được prefetch.

### 11.5 Thiết kế question bank đề xuất

```text
interview_question_blueprints
  id, version, status, locale
  job_family_id, competency_id, skill_ids[]
  difficulty_band, objective, canonical_prompt
  expected_seconds, min_seconds, max_seconds
  rubric_id, follow_up_policy_id
  source_ids[], author, reviewer, approved_at
  calibration_quality, empirical_difficulty, discrimination
  exposure_count_30d, max_exposure_rate, last_used_at

interview_question_variants
  id, question_id, locale, wording, status, semantic_hash

interview_rubrics
  id, version, criteria_json, scoring_policy_version

interview_question_embeddings
  question_id, embedding, embedding_model, content_hash
```

Tách canonical data và embedding projection. Khi blueprint/rubric đổi, tăng version và rebuild embedding theo `content_hash`; lịch sử phiên vẫn tham chiếu version cũ. Vector row không được là bản duy nhất chứa question/rubric.

### 11.6 Retrieval pipeline đúng

```text
1. Parse intent: job family + competency + target difficulty + locale
2. SQL hard filter: approved, locale, time, coverage, exposure, not-seen
3. Nếu candidates <= N hoặc query không có free text: deterministic rank
4. Nếu cần semantic match:
   - BM25 trên skill/title/requirement terms
   - dense similarity trên objective + competency summary
   - fusion/rerank trong candidate set đã hợp lệ
5. diversity/MMR hoặc semantic_hash để loại câu gần trùng
6. controller chọn question_id; fetch canonical prompt + rubric bằng key
7. prefetch 2 nhánh kế và ghi retrieval trace
```

Gợi ý scoring retrieval, chỉ dùng để xếp hạng trong tập hợp lệ:

```text
rank_score = 0.35 semantic_relevance
           + 0.25 lexical_skill_overlap
           + 0.20 calibration_quality
           + 0.10 novelty
           + 0.10 underexposure_bonus
```

Các trọng số chỉ là baseline để thí nghiệm, không phải policy đã được xác nhận. Hard constraints không được biến thành trọng số mềm.

### 11.7 Đánh giá code RAG hiện tại

`src/modules/matching/rag` có thể tái sử dụng ý tưởng embedding/vector store, nhưng chưa nên nối trực tiếp vào realtime interview:

- `Retriever.retrieve()` embed rồi vector-search trước, sau đó mới loại history; không có content coverage, time budget, exposure hoặc rubric version.
- Topic/difficulty vừa được đưa vào metadata filter, vừa bị `QualityLayer` so khớp exact lần nữa. Cách này dễ trả rỗng khi taxonomy/chuẩn hóa chuỗi không đồng nhất.
- `search_limit = max(..., 50)` tạo chi phí không cần thiết cho pool nhỏ.
- đường PostgreSQL dùng `psycopg2` đồng bộ và tạo/check extension/table trong search path; nếu gọi từ FastAPI event loop sẽ tăng tail latency.
- fallback embedding dimension trong retriever là `384`, trong khi vector store/project config hiện dùng `1024`; nhánh zero vector có nguy cơ dimension mismatch và bản thân zero-vector retrieval không có ý nghĩa.
- metadata JSONB text filters và dynamic SQL hiện chưa có allowlist key rõ ràng; schema question bank nên dùng typed columns cho các hard constraints.
- quality score mặc định `0.8` khi thiếu dữ liệu làm item chưa được đánh giá trông có vẻ tốt; thiếu quality phải là `UNKNOWN` hoặc không đủ điều kiện production.

Do đó cần tạo module question-bank/retrieval riêng, async, typed và có policy trace; interview runtime không được phụ thuộc ngược vào matching/RAG legacy. Chỉ trích embedding adapter dùng chung qua infrastructure sau khi chốt lifecycle và dimension contract.

### 11.8 Thí nghiệm quyết định có cần RAG

So sánh ba cấu hình trên cùng session plans:

- **B0 — SQL:** hard filters + deterministic rank.
- **B1 — Hybrid retrieval:** hard filters + BM25/dense fusion, không LLM rewrite.
- **B2 — Hybrid + constrained wording:** như B1, LLM chỉ viết biến thể từ blueprint/rubric khóa sẵn.

Golden set cần các JD/CV Việt, Anh và code-mixed; chuyên gia gán danh sách question IDs chấp nhận được thay vì chỉ một đáp án. Metrics:

- `AcceptableQuestionRecall@k`, `nDCG@k`, competency coverage và difficulty accuracy;
- duplicate/near-duplicate rate, exposure distribution và rubric alignment;
- hallucinated-premise rate (câu hỏi khẳng định sai về CV/JD);
- p50/p95 selection latency và chi phí mỗi turn;
- human preference chỉ dùng cùng các metric kiểm soát, không thay thế chúng.

Quy tắc promote: chỉ dùng B1/B2 nếu tăng đáng kể acceptable recall/coverage trên các slice khó mà vẫn giữ `rubricAlignment = 100%`, `hallucinatedPremiseRate` dưới gate và không làm tổng latency vượt budget. Nếu B0 ngang bằng, giữ B0 vì đơn giản và dễ audit hơn.

### 11.9 Quyết định cho roadmap hiện tại

- **P0/P1:** dùng PostgreSQL question bank, SQL hard filter và deterministic scheduler. Đây là đủ để xây luồng dễ → khó, timer và rubric đúng.
- **P2:** thêm hybrid retrieval sau hard filter cho personalization theo JD/CV và follow-up; chạy shadow trước, chưa điều khiển phiên production.
- **P3:** chỉ bật RAG theo competency/slice mà benchmark chứng minh có lợi. Không cần một vector database riêng: PostgreSQL + pgvector hiện có đủ cho thí nghiệm; “RAG” là pipeline retrieval + grounding, không đồng nghĩa phải thêm datastore mới.

## Nguồn kỹ thuật chính

- OpenAI, [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription): transcription session, WebRTC/WebSocket, incremental events, VAD và language/prompt.
- OpenAI, [Realtime WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc): kết nối browser và ephemeral client secrets.
- OpenAI, [Latency optimization](https://developers.openai.com/api/docs/guides/latency-optimization): streaming, giảm token/context và critical path.
- Deepgram, [Endpointing and interim results](https://developers.deepgram.com/docs/understand-endpointing-interim-results): `is_final`, `speech_final` và endpointing.
- Deepgram, [Live Audio API](https://developers.deepgram.com/reference/speech-to-text/listen-streaming): streaming options, interim results và key-term prompting.
- Lewis et al., [Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks](https://papers.neurips.cc/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html): nền tảng RAG và external non-parametric memory/provenance.
- Shin et al., [Components of the item selection algorithm in computerized adaptive testing](https://pmc.ncbi.nlm.nih.gov/articles/PMC5968224/): content balancing, item selection và exposure control là các phần riêng của adaptive assessment.
- pgvector, [official repository and documentation](https://github.com/pgvector/pgvector): exact/approximate nearest-neighbor search, filtering và iterative scans trong PostgreSQL.
