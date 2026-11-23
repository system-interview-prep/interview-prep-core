# scripts/eval — công cụ đo chất lượng, xếp theo module

Mỗi thư mục con mang đúng tên module trong `src/modules/`, nên nhìn đường dẫn là
biết script đang đo module nào. Thư mục nào không có module tương ứng thì không
đặt ở đây.

| Thư mục | Module được đo | Script |
|---|---|---|
| `matching/` | `src/modules/matching` | `metamorphic_corpus_v1.py` (bộ test metamorphic qua API thật — harness chính), `evaluate_matching_cv_corpus.py` (chấm offline toàn corpus), `bm25_provider_ab.py` (ParadeDB BM25 vs in-memory), `api_results_to_eval_csv.py`, `api_run_diff.py`, `probe_pair.py` (dump requirement 2 phía của 1 cặp), `probe_index_diacritics_coverage.py` (độ phủ từ có dấu) |
| `interviews/` | `src/modules/interviews` | `eval_interviewer_naturalness.py` (A/B prompt cũ–mới), `eval_session.py` (chấm 1 buổi thật), `interview_question_quality_report.py`, `progress_calibration.py` (độ nhiễu điểm cho trang Tiến độ), `watch_interviewer.py` (xem trực tiếp), `dump_interviewer_script.py` (in kịch bản để duyệt) |
| `interviews/voice/` | `src/modules/interviews/adapters/voice` | `collect_vi_stt_corpus.py`, `generate_vi_stt_collection_plan.py`, `import_google_sheet_audio_corpus.py`, `transcribe_vi_stt_corpus.py`, `evaluate_vi_stt.py` (WER), `tts_roundtrip.py`, `soak_voice_transport.py` |
| `question_bank/` | `src/modules/question_bank` | `question_coverage_report.py` |

## Không nằm ở đây

- `scripts/langfuse/` đã bị xoá ngày 23/11/2026: `langfuse_setup.py` (cấp phát
  score config, hàng đợi soát và giám khảo AI trên Langfuse), `langfuse_judges.py`
  (prompt 4 giám khảo) và `langfuse_experiment.py` (bộ đề hồi quy + dữ liệu).
  Lớp giám sát trong `src/` và giao diện quản trị vẫn nguyên; chỉ mất phần dựng
  tự động, nên muốn tạo lại các tài nguyên đó thì phải làm tay trong giao diện
  Langfuse. Khôi phục ba file: `git checkout 52af303 -- scripts/langfuse/`.
- `scripts/ingest_greenhouse_jobs.py`, `scripts/reparse_job_descriptions.py`
  — nạp và parse lại dữ liệu việc làm, là thao tác dữ liệu.

## Chạy

Hầu hết script cần database và biến môi trường của backend, nên chạy trong container:

    docker compose exec backend python scripts/eval/<module>/<script>.py

Script tự thêm gốc repo vào `sys.path` bằng `Path(__file__).resolve().parents[N]`
— **N phụ thuộc độ sâu thư mục**, nên nếu di chuyển script sang cấp khác thì phải
sửa lại con số đó, nếu không `import src...` sẽ gãy ngay khi chạy.
