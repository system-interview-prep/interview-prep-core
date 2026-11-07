# Kiểm thử STT bằng người đọc

## Luồng mic LiveKit (06/11/2026)

Nút test mic hiện dùng WebRTC/LiveKit như phỏng vấn. Hai luồng cùng worker,
`recognition.py`, cấu hình STT/VAD/BVC và `INTERVIEW_MICROPHONE_OPTIONS` trên frontend.
Màn test không chạy LLM, TTS, chấm điểm hoặc tạo interview session. Upload audio
vẫn là phép thử file riêng; không dùng kết quả file để khẳng định độ trễ mic LiveKit.

`POST /admin/ai/speech/test/livekit-token` yêu cầu quyền admin. Token phòng và
runtime credential có phạm vi riêng, không thể thay token runtime phỏng vấn.
Worker xác thực credential qua API trước khi mở ASR, nhận context nhưng không nhận
reference text. Reference chỉ lưu ở bản ghi `stt_test_issued` để đối chiếu sau.

Mic chờ worker ready rồi mới publish. Khi dừng, UI tắt mic, gửi stop và tiếp tục
nhận final tới khi worker xác nhận `stopped`; tối đa 30 giây. Khởi tạo có timeout
45 giây, mỗi lần test tối đa 20 phút. Rời phòng cũng kích hoạt drain/cleanup.
Nếu LiveKit không sẵn sàng thì báo lỗi, không tự đổi sang đường PCM WebSocket.
Endpoint PCM cũ được giữ để tương thích, không còn được nút mic của UI gọi.

Nếu sửa worker, chạy `docker compose restart voice-agent` sau khi kiểm tra code.
Backend dùng mount source và reload; nếu chạy backend không có reload thì restart
backend trước. Không cần migration hoặc đổi model.

### Các số đo mới của mic LiveKit

- `stt_test_started`: model/ngôn ngữ thực tế ở worker, context và snapshot tuning.
- `stt_test_asr_diagnostic`: phase `start/chunk/finish/error/aborted`, request ID,
  sequence của chunk, `processing_ms`, `chunk_audio_ms`, `submitted_audio_ms`.
- `audio_received_ms`: tổng PCM vào adapter; `audio_consumed_ms`: PCM adapter đã đọc;
  `backlog_ms`: chênh lệch hai mốc. PCM lúc idle có thể được đọc mà không gửi Qwen.
- `pcm_speech_end_to_final_ms`: từ lúc nhận frame có tiếng cuối theo RMS tới kết quả
  final, bao gồm chờ hàng đợi và HTTP. `speech_end_source=received_pcm_rms`.
  Đây là mốc ở worker, chưa phải thời gian âm thanh tuyệt đối ở mic hoặc nhãn VAD chuẩn.
- `stt_test_transport_stats`: các bộ đếm RTP ở trình duyệt khi SDK cung cấp, gồm
  packets/bytes đã gửi, packet loss, jitter và RTT. Không chuyển packet thành số mẫu
  PCM; im lặng/DTX và jitter buffer khiến hai đại lượng không tương đương.
- `stt_test_transcript`: raw, corrected, quyết định sửa/giữ và sequence. Qwen không có
  confidence đã hiệu chuẩn nên trace ghi null.
- `stt_test_completed`: trạng thái kết thúc, số frame nhận, số interim/final, raw và
  corrected tổng hợp. Trạng thái lỗi hoặc timeout không được coi là bài test thành công.

Worker gửi trace về backend theo hàng đợi riêng. UI báo lỗi nếu gửi không đủ;
`traceDelivered` xác nhận API nhận event, không chứng minh ổ đĩa đã ghi thành công.
Vẫn cần bật trace và đối chiếu file bằng test ID. Không lưu audio thô trong lượt này.

Phỏng vấn ghi cùng số đo adapter qua `voice_stt_diagnostic` trong trace voice.
Không lấy `elapsed_ms` của toàn bài đọc làm độ trễ nhận dạng. Không so số đo cũ
`speech_end_to_result_ms` với `pcm_speech_end_to_final_ms`: trước đây mốc end của
Qwen nằm sau HTTP finish, nên số cũ đánh giá thấp thời gian chờ.

### Giới hạn ngữ cảnh

AssemblyAI nhận context theo cấu hình steering dùng chung. Qwen demo API hiện chưa
nhận context; `context_applied_to_asr=false`. Context vẫn hỗ trợ bộ sửa từ bảo thủ
trong cả hai luồng. Không ép thuật ngữ theo reference và không sửa câu ứng viên nói
đúng thành đáp án mà hệ thống mong đợi.

Các mô tả bên dưới về file và PCM WebSocket là hợp đồng tương thích cũ; với nút mic
LiveKit hãy dùng các trường mới ở trên và lọc theo `mode=livekit`.

Vào trang quản trị đang có bảng kiểm thử STT/TTS. Không cần tạo buổi phỏng vấn.

1. Nhập **Ngữ cảnh hoặc câu hỏi** nếu muốn kiểm thử bộ sửa thuật ngữ theo ngữ cảnh, ví dụ câu hỏi về `unknown` và `any`.
2. Dán kịch bản vào **Bài đọc mẫu** rồi đọc theo khi bật micro; cũng có thể chọn tệp audio và phiên âm.
3. Nhấn dừng và đợi transcript cuối. Ghi lại **Mã lượt kiểm thử** để lọc log đúng lần đọc.
4. So sánh `reference_text`, `raw_text` và `text` trong trace để phân biệt lỗi STT với lỗi do bộ sửa từ.

**Bài đọc mẫu chỉ là dữ liệu đối chiếu**: không được chuyển đến provider và không dùng làm ngữ cảnh sửa từ. Ô ngữ cảnh hỗ trợ sửa từ; chỉ provider có hỗ trợ mới dùng thêm cho nhận dạng.

## Vị trí log

Workflow `test_stt` ghi vào `<TRACE_LOGS_DIR>/test-stt/trace.jsonl`. Với cấu hình phát triển hiện tại, đường dẫn trên máy là:

```text
D:\NCKH\interview-prep-core\trace-logs\test-stt\trace.jsonl
```

Compose đã mount `./trace-logs:/app/trace-logs`, nên backend trong Docker ghi được vào cùng thư mục trên máy. Tính năng dùng công tắc trace hiện có: `TRACE_LOGS_ENABLED=true` hoặc mặc định môi trường local/development. UI hiển thị cảnh báo khi trace đang tắt. Không lưu tệp audio trong thư mục trace.

## Các bản ghi

- `stt_test_started`: `test_id`, cách test bằng file hay stream, provider, model, locale.
- `stt_test_configured`: ngữ cảnh và bài đọc mẫu, ghi một lần cho mỗi test.
- `stt_test_audio_started`: bắt đầu nhận PCM mono 16 kHz.
- `stt_test_speech_state`: mốc provider báo bắt đầu/kết thúc lời nói, nếu provider hỗ trợ.
- `stt_test_transcript`: từng interim/final, nguyên văn ASR, bản sửa, các quyết định sửa/giữ nguyên và confidence nếu có.
- `stt_test_input_ended`: lúc hết tệp hoặc người dùng dừng/mất kết nối.
- `stt_test_error`: loại lỗi; không ghi token xác thực hoặc thông tin credential.
- `stt_test_completed`: tổng hợp toàn bộ final, số interim/final, thời lượng audio đã nhận, kết quả hoàn tất/dừng/mất kết nối/lỗi/timeout.

Mọi bản ghi cùng lượt có chung `test_id`; các lượt chạy đồng thời không dùng chung mã.

## Đọc số đo độ trễ

- `elapsed_ms`: thời gian kể từ khi test bắt đầu trên server.
- `audio_received_ms`: thời lượng audio PCM thật đã nhận, không phải độ trễ.
- `after_last_audio_ms`: thời gian từ gói PCM gần nhất tới kết quả; khi mic vẫn gửi khoảng lặng, số này không thể coi là độ trễ sau khi người dùng nói xong.
- `speech_end_to_result_ms`: khoảng cách từ sự kiện kết thúc lời nói do provider báo tới final. Không có mốc thì ghi `null`.
- `input_end_to_result_ms`: khoảng cách từ lúc đóng đầu vào tới final còn lại. Hữu ích khi dừng micro hoặc xử lý file; khác với độ trễ nhận dạng khi đang nói.

Test offline giả lập provider kiểm tra việc ghi đúng file, ID riêng, bảo toàn transcript gốc, các final đến sau thao tác dừng, lỗi/mất kết nối và việc bài đọc mẫu không ảnh hưởng nhận dạng. Muốn đánh giá chính xác STT trên giọng thật, cần người đọc đúng kịch bản và đối chiếu trace của lượt đó.
