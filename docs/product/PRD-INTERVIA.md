# INTERVIA — Product Requirements Document

**Phiên bản 1.1 · 09/10/2026**

| Hạng mục | Nội dung |
|---|---|
| Sản phẩm | INTERVIA — nền tảng luyện phỏng vấn bằng AI, bám CV và JD |
| Chủ sở hữu tài liệu | Nhóm KLTN INTERVIA — PM / PO (Chờ điền tên) |
| Hợp đồng nguồn | INTERVIA Product Contract — INTV-PC-001 Rev 1.1 |
| Nguồn bổ sung | Mã nguồn `interview-prep-core` nhánh `feat/interview-langgraph` và `interview-prep-frontend` (09/10/2026); thư mục `docs/` của repo; khảo sát thị trường 09/10/2026; `MARKET-READINESS-AUDIT.md` |
| Trạng thái | Bản thảo, chờ review |

---

## Record of Changes

A = Thêm · M = Sửa · D = Xoá

| Ngày | A/M/D | Phiên bản | Nội dung thay đổi | Người thực hiện |
|---|---|---|---|---|
| 09/10/2026 | A | 1.0 | Bản thảo đầu tiên. Mục 1–14 rút từ hợp đồng INTV-PC-001 Rev 1.0, mã nguồn hiện tại và khảo sát thị trường ngày 09/10. Thêm 7 tính năng đề xuất F-01 đến F-07 ở mục 7.3. Ghi nhận 3 khoảng trống giữa hợp đồng và code ở mục 13 (RK-1 đến RK-3); RK-2 đã xác nhận là vi phạm đang xảy ra. | Nhóm INTERVIA |
| 09/10/2026 | A / M | 1.1 | **Sửa những chỗ v1.0 nói quá so với code** (sau khi rà toàn bộ backend và frontend): R-05 → Một phần (130 câu seed được chèn thẳng APPROVED, bỏ qua quy trình duyệt, dùng chung một rubric); R-10 → Một phần (khi chấm chỉ gửi tên và mô tả tiêu chí, bỏ qua mốc 0–3, trọng số, ý bắt buộc; `what_good_looks_like` được sinh rồi bỏ); R-12 → Đang vi phạm (endpoint đối chiếu không xác thực, đọc CV không lọc người dùng); giai đoạn VALIDATE chỉ là một câu mẫu, không kiểm tính trung thực. **Thêm** mục 7.5 Luận điểm khác biệt so với ChatGPT, 7.6 Hồ sơ tiến bộ (P-01 đến P-13), 7.7 Dữ liệu và API, 7.8 Màn hình; F-08 Dán JD của bạn; NFR-08, NFR-09; GC-SEC-02, GC-PROG-01 đến GC-PROG-08; RK-10 đến RK-14. Luật trích dẫn chuyển thành mục 7.9. | Nhóm INTERVIA |

---

## Cách đọc tài liệu này

Mọi phát biểu có tính ràng buộc đều mang một nhãn nguồn.

| Nhãn | Ý nghĩa |
|---|---|
| [Hợp đồng] | Do INTV-PC-001 quy định. PRD này không được sửa hay mở rộng. |
| [Code] | Đã có trong mã nguồn; đường dẫn ghi ngay tại chỗ. |
| [Code — tắt mặc định] | Có trong code nhưng nằm sau feature flag tắt mặc định. Không được trình bày như tính năng đã phát hành. |
| [Khảo sát] | Tìm được trong khảo sát thị trường ngày 09/10/2026; nguồn ghi ngay tại chỗ. |
| [Đề xuất] | Tính năng hoặc lựa chọn thiết kế mới, chưa được nhóm chốt, chưa có trong code. |
| [Giả định] | Một cách diễn giải, không phải sự thật đã kiểm chứng. |

**Nguyên tắc.** Ở đâu nguồn tài liệu không đỡ được phát biểu, tài liệu này ghi *Không quy định* hoặc *Chờ xác định*, thay vì tự lấp chỗ trống.

---

## 1. Sản phẩm

Nền tảng luyện phỏng vấn tiếng Việt, đặt câu hỏi dựa trên chính CV của ứng viên và JD họ nhắm tới, rồi chấm từng câu trả lời theo rubric có trích dẫn bằng chứng.

Ứng viên tải CV, chọn một JD đã xuất bản, hệ thống đối chiếu CV với JD, lập kế hoạch phỏng vấn theo năng lực, chạy buổi phỏng vấn qua chat, giọng nói hoặc video, rồi trả báo cáo theo rubric và khung STAR. INTERVIA là công cụ luyện tập, không phải công cụ ra quyết định tuyển dụng. [Hợp đồng §1.2.1] [Code — `docs/INTERVIEW-REALTIME-RESEARCH-AND-IMPLEMENTATION.md`]

## 2. Vấn đề

### Đang giải quyết cái gì

- **Thiếu kinh nghiệm phỏng vấn là rào cản lớn nhất của sinh viên mới ra trường.** Trong một khảo sát của Đại học Sư phạm TP.HCM, 87,78% sinh viên mới tốt nghiệp nêu "chưa có kinh nghiệm phỏng vấn" là khó khăn, cao hơn cả kỹ năng viết CV (81,11%). Mẫu khoảng 90 người, không đại diện toàn quốc, năm khảo sát chưa rõ. [Khảo sát — journal.hcmue.edu.vn]
- **Người tìm việc lo nhiều hơn chuẩn bị.** Khảo sát của Adobe (hơn 1.000 người, 756 người đang tìm việc) cho thấy họ dành gần 5 giờ lo lắng nhưng chỉ khoảng 3 giờ chuẩn bị cho một buổi phỏng vấn; Gen Z lo nhiều nhất (6,1 giờ). [Khảo sát — adobe.com]
- **Phỏng vấn thật hầu như không cho phản hồi.** Khảo sát của Greenhouse ghi nhận khoảng 75% ứng viên từng bị "bỏ rơi" sau phỏng vấn và hơn 70% muốn được nhận phản hồi. Các con số đến từ nhiều năm khảo sát khác nhau, không cộng gộp được. [Khảo sát — Greenhouse, qua HR Dive / Axios]
- **Công cụ luyện phỏng vấn AI hiện có chấm cách nói nhiều hơn chấm nội dung.** Các bài so sánh năm 2026 nhận xét phản hồi của nhóm công cụ này mạnh ở cách trình bày (từ đệm, tốc độ) và yếu ở nội dung câu trả lời; một người dùng Yoodli nói công cụ "tập trung vào chỉ số kỹ thuật hơn là lý do". Phần lớn nguồn là bài của nhà cung cấp hoặc affiliate. [Khảo sát — finalroundai.com, favtutor.com, mocky.pro]
- **Câu hỏi chung chung, không bám CV.** Các công cụ phổ biến sinh câu hỏi từ JD hoặc từ ngân hàng câu hỏi chung; câu hỏi xác minh những gì ứng viên viết trong CV là điểm khác biệt hiếm. [Giả định — chưa khảo sát có hệ thống]
- **ChatGPT đã làm được phần lớn "luyện phỏng vấn cơ bản".** Người dùng dán CV và JD, yêu cầu hỏi từng câu, hỏi đào sâu, dùng giọng nói và xin chấm điểm. Hạn chế được nêu: phản hồi có xu hướng dễ dãi nếu không yêu cầu nghiêm khắc, không có điểm số chuẩn hóa, không theo dõi tiến bộ có cấu trúc qua các buổi. [Khảo sát — mockif.com, makeuseof.com, mocky.pro] Vì vậy khác biệt của INTERVIA phải nằm ở chỗ một cuộc chat về cấu trúc không làm được (mục 7.5), không ở việc "hỏi theo CV".
- **LLM chấm cùng một câu trả lời có thể cho điểm khác nhau.** Thay đổi thứ tự tiêu chí, cách đánh nhãn điểm hay nhiệt độ đều làm điểm dao động; nhiệt độ 0 không bảo đảm điểm giống nhau. [Khảo sát — arXiv 2506.22316, arXiv 2603.04417] Mọi biểu đồ "tiến bộ" vẽ từ điểm LLM mà không đo sai số có thể chỉ là vẽ nhiễu.
- **Không tìm thấy nền tảng luyện phỏng vấn AI tiếng Việt nổi bật.** Tìm kiếm không thấy TopCV hay ITviec công bố tính năng phỏng vấn thử bằng AI. [Khảo sát — tìm kiếm ngày 09/10/2026; chưa xác nhận trực tiếp trên hai nền tảng]

### Ai chịu ảnh hưởng

- Sinh viên năm cuối và người mới đi làm, phải vào phỏng vấn thật mà chưa từng được luyện có phản hồi.
- Người chuyển ngành, có CV nhưng chưa biết kể kinh nghiệm cũ theo ngôn ngữ của ngành mới.
- Trung tâm hỗ trợ việc làm của các trường, không đủ người để phỏng vấn thử 1–1 cho từng sinh viên. [Giả định]

### Vì sao đáng làm

- Số người dùng tiềm năng và mức sẵn lòng chi trả: *Không quy định* trong nguồn tài liệu. Đây là con số thiếu quan trọng nhất.
- Greenhouse (2026, 2.950 người tìm việc ở 5 nước) ghi nhận 63% ứng viên Mỹ đã từng gặp phỏng vấn AI; 39% muốn biết rõ AI đang đo cái gì. Ứng viên cần luyện trước với chính dạng phỏng vấn này. [Khảo sát — greenhouse.com]

## 3. Mục tiêu

Các mục tiêu dưới đây do nhóm đặt cho phạm vi khóa luận. Mục tiêu kinh doanh: *Không quy định*.

| ID | Mục tiêu | Nguồn |
|---|---|---|
| G-1 | Mọi phiên phỏng vấn kết thúc ở đúng một trạng thái cuối hợp lệ, đọc từ bản ghi phiên sau khi phiên đóng. | Hợp đồng §2.1 |
| G-2 | Không có severe failure nào trên bộ ca kiểm thử chuẩn. | Hợp đồng §3 |
| G-3 | Mọi điểm số trong báo cáo truy ngược được về câu trả lời của ứng viên và tiêu chí rubric. | Hợp đồng §1.1.11, SF-01 |
| G-4 | Chạy trọn một buổi phỏng vấn giọng nói thật trong buổi bảo vệ. | Hợp đồng §1.3.1 |
| G-5 | Ứng viên thấy được mình tiến bộ ở kỹ năng nào, có khoảng tin cậy, và chỉ được báo "tiến bộ" khi thay đổi vượt sai số chấm đã đo. | Hợp đồng §1.4, SF-07 |

## 4. Không thuộc phạm vi

- Đưa ra quyết định tuyển hay loại, hoặc bất kỳ nhãn nào mang nghĩa đó, cho ứng viên. [Hợp đồng §1.2.1]
- Trợ lý nhắc bài trong lúc phỏng vấn thật ("interview copilot"). [Hợp đồng §1.2.2]
- Phân tích khuôn mặt, cảm xúc hay ngoại hình để chấm điểm. [Hợp đồng §1.2.3]
- Ứng viên tự tải JD lên hệ thống; JD do quản trị viên đăng và xuất bản. [Code — `src/modules/job_descriptions/router.py`]
- Thanh toán trực tuyến. Hệ thống có số dư credit nhưng chưa có cổng thanh toán. [Code — `src/modules/users/schemas.py`]

## 5. Nhu cầu người dùng

| ID | Nhu cầu | Nguồn |
|---|---|---|
| N-1 | Được hỏi những câu sát với CV và JD của chính mình, không phải câu hỏi chung chung. | Hợp đồng §1.1.3; Giả định |
| N-2 | Biết vì sao mình được điểm đó và câu trả lời tốt hơn trông như thế nào, viết từ chính kinh nghiệm của mình. | Khảo sát — Greenhouse (39% muốn biết AI đo gì); finalroundai.com |
| N-3 | Luyện được lúc nào cũng được, buổi ngắn cũng được, không bị phán xét. | Khảo sát — Adobe (5 giờ lo / 3 giờ chuẩn bị) |
| N-4 | Thấy mình tiến bộ qua nhiều buổi, biết nên luyện gì tiếp. | Khảo sát — interviewdrills.com (điểm dùng để theo dõi tiến bộ của chính mình) |
| N-5 | Tin rằng CV và câu trả lời của mình không bị lộ hay bị dùng để đánh giá mình với nhà tuyển dụng. | Hợp đồng SF-03; Giả định |
| N-6 | Buổi luyện không hỏng giữa chừng và không mất công chuẩn bị. | Khảo sát — Huru (người dùng báo công cụ "không hỏi câu nào" sau khi nạp JD) |
| N-7 | Biết tiến bộ của mình là thật hay chỉ do AI chấm lúc dễ lúc khó. | Khảo sát — arXiv 2506.22316; Giả định |
| N-8 | Biết còn bao xa mới sẵn sàng cho đúng JD mình sắp phỏng vấn, và nên luyện gì tiếp. | Khảo sát — Interview Prep Academy (Readiness Dashboard); Code — frontend đang hiển thị "—" |
| N-9 | Luyện được với JD của chính mình, không chỉ JD có sẵn trong hệ thống. | Code — ứng viên chỉ chọn được JD do quản trị viên đăng |

## 6. User Stories

| ID | Story | Dẫn tới |
|---|---|---|
| US-01 | Là ứng viên, tôi muốn tải CV lên và sửa được những chỗ hệ thống đọc sai, để buổi phỏng vấn dựa trên đúng thông tin của tôi. | N-1 · R-01 |
| US-02 | Là ứng viên, tôi muốn thấy CV của mình khớp và chưa khớp yêu cầu nào của JD, kèm bằng chứng, trước khi phỏng vấn. | N-1 · R-02 |
| US-03 | Là ứng viên, tôi muốn được hỏi về chính các dự án trong CV, để luyện đúng những câu nhà tuyển dụng sẽ hỏi. | N-1 · R-03, R-04 |
| US-04 | Là ứng viên, tôi muốn biết trước buổi phỏng vấn có bao nhiêu câu và kéo dài bao lâu, và được dừng sớm khi cần. | N-3 · R-06, R-07 |
| US-05 | Là ứng viên, tôi muốn mỗi điểm số đi kèm câu nói của chính tôi làm bằng chứng và tiêu chí rubric tương ứng. | N-2 · R-09, R-10 |
| US-06 | Là ứng viên, tôi muốn nhận câu trả lời mẫu viết lại từ chính kinh nghiệm trong CV của tôi. | N-2 · F-01 |
| US-07 | Là ứng viên, tôi muốn luyện lại đúng câu tôi làm kém nhất và thấy điểm thay đổi. | N-4 · F-02 |
| US-08 | Là người soạn câu hỏi, tôi muốn câu hỏi do AI sinh ra phải qua người duyệt trước khi vào ngân hàng chính thức. | N-1 · R-05 |
| US-09 | Là quản trị viên, tôi muốn biết trước JD nào thiếu câu hỏi trong ngân hàng, để ứng viên không vào một buổi phỏng vấn rỗng. | N-6 · R-05, F-06 |
| US-10 | Là người vận hành, tôi muốn trace từng lượt của mọi phiên, để phân biệt lỗi do thay đổi code với dao động ngẫu nhiên của mô hình. | NFR-06 |
| US-11 | Là ứng viên đã luyện nhiều buổi, tôi muốn xem từng kỹ năng của mình thay đổi ra sao theo thời gian, kể cả khi các buổi dùng JD khác nhau. | N-4, N-7 · P-01, P-04 |
| US-12 | Là ứng viên, tôi muốn trả lời lại đúng câu tôi từng trả lời và xem hai câu trả lời cạnh nhau, tiêu chí nào đã lên mức. | N-4 · P-05, P-06 |
| US-13 | Là ứng viên sắp phỏng vấn thật, tôi muốn nhập ngày phỏng vấn và JD mục tiêu, để nhận kế hoạch luyện đến ngày đó và biết mình đang ở mức sẵn sàng nào. | N-8 · P-08, P-09, P-10 |
| US-14 | Là ứng viên, tôi muốn biết những điều tôi ghi trong CV mà tôi chưa bảo vệ được khi bị hỏi đào sâu. | N-1 · P-07 |
| US-15 | Là ứng viên, tôi muốn xóa CV cũ mà không mất lịch sử luyện tập. | N-4, N-5 · P-12 |

---

## 7. Yêu cầu

### 7.1 Rút từ hợp đồng — Bắt buộc

| ID | Yêu cầu | Nguồn |
|---|---|---|
| R-01 | CV phải được xử lý bất đồng bộ, báo tiến độ cho ứng viên, và cho phép ứng viên xem và sửa dữ liệu đã trích xuất trước khi dùng. Trích xuất phải có đường dự phòng khi dịch vụ OCR chính lỗi. | Hợp đồng §1.1.1 · Code — `src/modules/user_cvs/` |
| R-02 | Đối chiếu CV–JD phải trả kết quả theo từng yêu cầu của JD, mỗi kết luận kèm bằng chứng trích từ CV. Không được dùng điểm đối chiếu để kết luận đạt hay trượt. | Hợp đồng §1.1.2, §1.2.1 · Code — `src/modules/matching/` |
| R-03 | Kế hoạch phỏng vấn phải được lập từ CV, JD và kết quả đối chiếu đã chuẩn hóa; mỗi mục trong kế hoạch phải gắn với một năng lực và, khi có, một dự án trong CV. | Hợp đồng §1.1.3 · Code — `planning/planner.py`, `planning/project_evidence.py`. Lưu ý: giai đoạn VALIDATE hiện là **một câu hỏi mẫu** dựng từ vai trò, công nghệ, số liệu của dự án liên quan JD nhất; không có rubric, không đối chiếu câu trả lời với CV; `FAST_FAIL_VALIDATION` được khai báo nhưng không bao giờ phát ra. |
| R-04 | Câu hỏi phải được chốt (freeze) trước khi phiên mở. Sau khi chốt, bộ câu hỏi của phiên không đổi. | Hợp đồng §1.1.4, SF-02 · Code — `planning/question_selector.py` |
| R-05 | Câu hỏi chỉ đến từ ngân hàng đã duyệt. Câu do AI sinh để lấp chỗ thiếu phải mang trạng thái IN_REVIEW và người duyệt phải khác người soạn. Khi ngân hàng không phủ được mục nào trong kế hoạch thì trả 409 `question_bank_insufficient` và không mở phiên; thiếu một phần thì phiên vẫn mở với các mục đã phủ. | Hợp đồng §1.1.5 · Code — `planning/question_generation.py`, `src/modules/question_bank/` · **Một phần:** quy trình duyệt có thật trong `question_bank/service.py`, nhưng 130 câu seed được chèn thẳng ở trạng thái APPROVED (`src/seeds/question_bank_seed.py`) và dùng chung một rubric Relevance/Depth/Clarity không có ý bắt buộc. |
| R-06 | Phiên chạy theo máy trạng thái WARM_UP → VALIDATE → DEEP_DIVE → CHALLENGE → BEHAVIORAL → CLOSING → CLOSED. Mô hình ngôn ngữ không được tự chuyển giai đoạn. | Hợp đồng §1.1.6 · Code — `core/interview_engine.py` |
| R-07 | Mỗi câu hỏi được đào sâu (PROBE) tối đa một lần. Khi ứng viên hỏi lại hoặc trả lời mơ hồ, hệ thống làm rõ (CLARIFY) thay vì chấm. | Hợp đồng §1.1.7 · Code — `core/interview_types.py` |
| R-08 | Ứng viên muốn dừng sớm thì hệ thống phải hỏi xác nhận (CONFIRM_ABORT) trước khi đóng phiên. | Hợp đồng §1.1.8 · Code — `application/chat_runtime.py` |
| R-09 | Một phiên chỉ được đóng với lý do COMPLETED khi đã phủ đủ agenda. Endpoint đóng phiên chung không nhận COMPLETED. Lý do do hệ thống quyết định (HARD_TIMEOUT, FAST_FAIL_TECH) không được nhận từ client. | Hợp đồng §2.2, SF-06 · Code — `api/router.py` (`CloseInterviewSession`, `ClientEndReason`) |
| R-10 | Mỗi lượt trả lời được chấm trên thang 0–10, kèm phân tích STAR, trích dẫn bằng chứng, điểm mạnh, điểm yếu và câu trả lời mẫu. Lượt mô hình không trả điểm hợp lệ phải đánh dấu `graded = false`, không được tính vào điểm tổng. | Hợp đồng §1.1.10 · Code — `evaluation/evaluation_types.py`. **Một phần:** khi chấm, `evaluation_service.py:199-213` chỉ gửi "tên: mô tả" của tiêu chí; mốc 0–3, trọng số và ý bắt buộc trong snapshot rubric bị bỏ qua. `what_good_looks_like` được sinh nhưng không có cột lưu. `GET /evaluation` không trả `graded`. |
| R-11 | Mọi `evidence_quotes` phải là trích nguyên văn từ câu trả lời của ứng viên trong chính lượt đó. | Hợp đồng §1.1.11, SF-01 · **Chưa có trong code — xem RK-1, mục 7.9** |
| R-12 | Ứng viên chỉ đọc và ghi được phiên, CV và báo cáo của chính mình. Mọi endpoint nhận id CV, JD hay phiên phải yêu cầu đăng nhập và lọc theo người dùng. | Hợp đồng SF-03 · Code — đạt ở `interviews/api/router.py` (`_owned_session`). **Đang vi phạm** ở `matching/api/router.py`: `/match`, `/match-ids`, `/ai/score-cv-jp`, `/clarifications*` không xác thực, và `matching/application/input_resolver.py:25` đọc `user_cvs` theo id không lọc `user_id`. |
| R-13 | Ba kênh chat, giọng nói và video phải đi qua cùng một lõi phỏng vấn; kênh chỉ chuyển lời nói thành văn bản cuối và ngược lại. | Hợp đồng §1.3.2 · Code — `agent/graph.py`, `adapters/` · **Một phần — xem RK-5** |
| R-14 | Mọi lệnh gửi tin nhắn mang `clientMessageId`; gửi lại cùng một id không được tạo lượt mới. | Hợp đồng §4.2.3 · Code — `api/router.py` (`SendChatMessage`) |
| R-15 | Báo cáo hiển thị cho ứng viên không được chứa nhãn tuyển hay loại (PASS, REJECT, STRONG_PASS, CONSIDER). | Hợp đồng SF-04 · **Đang vi phạm trong code — xem RK-2** |
| R-16 | Hệ thống không được hỏi về tuổi, tình trạng hôn nhân, kế hoạch sinh con, tôn giáo, dân tộc, quê quán hay sức khỏe. | Hợp đồng SF-05 · Chưa có bộ lọc — xem RK-3 |

### 7.2 Rút từ hợp đồng — Nên có

| ID | Yêu cầu | Nguồn |
|---|---|---|
| R-17 | Thời lượng phiên do ứng viên chọn trong khoảng 2–120 phút; số câu hỏi co giãn theo ngân sách thời gian. | Hợp đồng §1.1.9 · Code — `CreateInterviewSession.duration_minutes`, `docs/INTERVIEW_DYNAMIC_QUESTION_BUDGET_PROPOSAL.md` |
| R-18 | Câu hỏi về CV ở giai đoạn VALIDATE nên hỏi mở về một chi tiết trong CV, không đọc chi tiết đó ra rồi hỏi "đúng không". Việc theo dõi ứng viên bảo vệ được những tuyên bố nào trong CV thuộc P-07. | Hợp đồng §1.1.3 · Đề xuất |
| R-19 | Báo cáo nên có hai phần tách bạch: phần phản hồi cho ứng viên và phần ghi chú kỹ thuật; phần ghi chú kỹ thuật không hiển thị cho ứng viên. | Hợp đồng SF-04 · Đề xuất |

### 7.3 Tính năng mới đề xuất

Mỗi tính năng ghi rõ điểm đau và nguồn, "đúng" nghĩa là gì theo dạng §1.1 của hợp đồng, và nhóm phải cắt gì để có chỗ làm. Tính năng không có đánh đổi không được duyệt. [Hợp đồng §7.3]

| ID | Tính năng | Điểm đau và nguồn | "Đúng" nghĩa là | Đánh đổi |
|---|---|---|---|---|
| F-01 | **Viết lại câu trả lời từ chính CV của bạn.** Với mỗi lượt điểm dưới 7, hệ thống viết lại câu trả lời theo STAR chỉ dùng dữ kiện có trong CV và câu trả lời gốc, đánh dấu từng dữ kiện lấy từ đâu. | Công cụ hiện có chấm cách nói, không chấm nội dung. [Khảo sát — finalroundai.com, mocky.pro] | Mọi con số, tên dự án, công nghệ trong câu viết lại phải có trong CV hoặc câu trả lời gốc. Không có dữ kiện thì để ô trống có nhãn "bạn cần bổ sung", không tự bịa. | Dùng lại lời gọi đang sinh `what_good_looks_like` (hiện bị bỏ, không lưu), thêm cột lưu và bước kiểm dữ kiện. Hoãn phần phân tích video. |
| F-02 | **Vòng luyện lại câu yếu nhất.** Sau báo cáo, ứng viên trả lời lại 1–3 câu điểm thấp nhất trong một phiên ngắn, nhận điểm mới và độ chênh so với lần đầu. | Người dùng muốn theo dõi tiến bộ của chính mình. [Khảo sát — interviewdrills.com] | Phiên luyện lại dùng đúng phiên bản câu hỏi và rubric đã chốt của lần đầu; độ chênh chỉ tính giữa hai lần chấm cùng rubric. | Dùng lại `experience_type = question_practice`. Cắt màn hình lịch sử chi tiết. |
| F-03 | **Kế hoạch lấp khoảng trống so với JD.** Từ các yêu cầu JD chưa khớp trong kết quả đối chiếu, hệ thống đề xuất kế hoạch luyện 7 ngày và một phiên phỏng vấn tập trung vào các năng lực đó. | Ứng viên không biết nên luyện gì tiếp. [Giả định] | Mỗi mục trong kế hoạch trỏ về một yêu cầu JD cụ thể có kết quả "chưa khớp" hoặc "thiếu bằng chứng". Không tạo mục cho yêu cầu đã khớp. | Phụ thuộc chất lượng đối chiếu, hiện đánh giá là thấp. [Code — `docs/CV-JD-MATCHING-ASSESSMENT.md`] Phải làm bộ gold corpus trước. |
| F-04 | **"AI chấm gì" — rubric minh bạch.** Trước phiên hiển thị các năng lực sẽ được chấm; sau phiên, mỗi điểm gắn với tiêu chí rubric đã đạt và chưa đạt. | 39% ứng viên muốn biết AI đang đo gì; 44% muốn được báo trước. [Khảo sát — greenhouse.com 2026] | Mỗi điểm trên báo cáo liên kết được tới đúng phiên bản rubric đã chốt cho câu đó. | Không đổi mô hình chấm. Chi phí nằm ở giao diện. |
| F-05 | **Phong cách người phỏng vấn và phỏng vấn tiếng Anh.** Chọn phong cách (thân thiện / áp lực / kỹ thuật sâu) và ngôn ngữ (vi-VN / en-US) khi tạo phiên. | Ứng viên Việt ứng tuyển công ty nước ngoài phải phỏng vấn bằng tiếng Anh. [Giả định] | Phong cách chỉ đổi câu chữ và nhịp hỏi; không đổi bộ câu hỏi đã chốt, rubric hay máy trạng thái. | Cần ngân hàng câu hỏi tiếng Anh. Hoãn kênh video cho tiếng Anh. |
| F-06 | **Kiểm tra trước khi vào phòng.** Trước khi ứng viên bấm bắt đầu, hệ thống chạy thử lập kế hoạch và chốt câu hỏi, báo trước nếu JD thiếu câu hỏi, thay vì để ứng viên vào một phiên hỏng. | Người dùng Huru báo công cụ không hỏi câu nào sau khi nạp JD. [Khảo sát — finalroundai.com/blog/huru-review-pros-cons] | Không phiên nào được mở mà chưa chốt xong câu hỏi. Lỗi kỹ thuật đóng phiên với TECHNICAL_FAILURE, không quy cho ứng viên. | Dùng lại báo cáo độ phủ ngân hàng câu hỏi (`planning/question_coverage.py`). Thêm khoảng 1–2 giây trước khi vào phòng. |
| F-08 | **Dán JD của bạn.** Ứng viên dán hoặc tải JD của công ty mình sắp phỏng vấn; hệ thống bóc tách bằng bộ phân tích JD đã có, gắn vào taxonomy, rồi chọn câu từ ngân hàng theo `concept_id`. | ChatGPT nhận bất kỳ JD nào; INTERVIA hiện chỉ cho chọn JD do quản trị viên đăng. [Code — frontend `UserJobsBoard.tsx` gọi `/admin/job-descriptions`] | JD của ứng viên là riêng tư, không xuất bản cho người khác. Câu hỏi vẫn chỉ lấy từ ngân hàng đã duyệt theo khái niệm; khái niệm không có câu thì báo trước (F-06), không tự sinh câu chưa duyệt cho phiên của ứng viên. | Bộ phân tích JD trên văn bản thật hiện đạt khoảng 0,53 [Code — `reports/`]; phải cho ứng viên xem và sửa kỹ năng trích ra, giống luồng sửa CV. Hoãn F-05. |
| F-07 | **Khởi động 3 phút.** Phiên rất ngắn, 2–3 câu, không chấm điểm số, chỉ trả một nhận xét, để giảm lo âu trước buổi phỏng vấn thật. | Người tìm việc dành gần 5 giờ lo lắng, 3 giờ chuẩn bị. [Khảo sát — adobe.com] | Phiên khởi động không ghi điểm vào lịch sử tiến bộ và không sinh báo cáo đầy đủ. | Dùng lại chế độ demo 2–3 phút. [Code — `core/demo_mode.py`] Không làm bản giọng nói ở giai đoạn đầu. |

### 7.5 Luận điểm khác biệt so với ChatGPT

ChatGPT nhận CV và JD được dán vào, hỏi từng câu, hỏi đào sâu, chạy bằng giọng nói và chấm điểm khi được yêu cầu. Những thứ đó **không** phải khác biệt của INTERVIA. Bảng điều khiển tiến bộ nói chung cũng không phải: Exponent, Yoodli (bản doanh nghiệp) và Interview Prep Academy đã có. [Khảo sát — tryexponent.com, yoodli.ai, producthunt.com]

Khác biệt chỉ đứng được ở những chỗ một cuộc chat không trạng thái về cấu trúc không làm được:

| ID | Khác biệt | Vì sao chat chung không làm được | Hiện tại |
|---|---|---|---|
| D-1 | **Điểm có sai số được công bố.** Cùng câu trả lời, cùng rubric phiên bản, điểm nằm trong biên sai số đã đo. | Mỗi cuộc chat chấm theo một tiêu chuẩn ngầm khác nhau; không có rubric cố định để đo sai số. | Chưa |
| D-2 | **Tiến bộ đo được.** Sổ kỹ năng theo `concept_id`, khoảng tin cậy, chỉ báo "tiến bộ thật" chỉ khi vượt ngưỡng nhiễu. | Trí nhớ của chat là văn bản tự do, không phải chuỗi đo theo kỹ năng. | Chưa |
| D-3 | **Câu neo do người duyệt.** Mỗi kỹ năng có câu đã duyệt được hỏi lại định kỳ để so sánh cùng điều kiện. | Mô hình nghĩ câu mới mỗi lần; hai lần đo không cùng đề. | Một phần (có xoay vòng câu, chưa có câu neo) |
| D-4 | **Phòng thủ CV.** Mỗi dự án, con số, công nghệ trong CV là một mục cần bảo vệ; theo dõi mục nào đã được trả lời vững khi bị hỏi đào sâu. | Chat không giữ danh sách tuyên bố của CV và trạng thái từng tuyên bố qua các buổi. | Chưa |
| D-5 | **Vòng khép kín.** Khoảng trống JD → bài luyện → đo lại → cập nhật sổ kỹ năng → mức sẵn sàng theo JD. | Người dùng phải tự nhớ và tự lên kế hoạch. | Chưa |
| D-6 | **Bảo đảm cứng bằng code.** Không bịa trích dẫn, không hỏi câu phân biệt đối xử, không ghi hoàn thành sai. | Chỉ dựa vào lời dặn trong prompt. | Một phần |

**Luận điểm:** *Tiến bộ đo được, không phải cảm giác.* INTERVIA đo sai số chấm của chính mình và chỉ nói "bạn đã tiến bộ" khi thay đổi lớn hơn sai số đó. Trong phạm vi khảo sát, chưa thấy sản phẩm luyện phỏng vấn nào công bố sai số chấm cho người dùng. [Khảo sát — tìm kiếm ngày 09/10/2026; chưa phải khảo sát toàn diện]

### 7.6 Hồ sơ tiến bộ

P-01, P-03, P-04 và P-12 hiện thực hóa điều khoản MUST của Hợp đồng §1.4 nên mang nhãn [Hợp đồng]; các P còn lại là [Đề xuất]. Tất cả chịu ràng buộc của SF-07. Thứ tự P0 → P2 là thứ tự phải làm: các màn hình ở P2 vô nghĩa nếu nền đo ở P0 sai.

| ID | Ưu tiên | Yêu cầu | Nguồn |
|---|---|---|---|
| P-12 | P0 | **Không bao giờ mất lịch sử.** Xóa CV không được ẩn hay xóa phiên và báo cáo cũ. Bỏ điều kiện `resume_id IS NOT NULL` khi liệt kê và đọc phiên; phiên giữ snapshot đủ để hiển thị báo cáo khi CV đã xóa. | Hợp đồng §1.4.6 · Code — `migrations/versions/20260925_0013_interview_runtime_foundation.py:30` (`ON DELETE SET NULL`), `api/router.py:182, 332` |
| P-01 | P0 | **Sổ kỹ năng theo `concept_id`.** Mỗi lượt đã chấm sinh một quan sát gắn `concept_id`, `taxonomy_version`, `question_version_id`, `rubric_version_id`, `scorer_version`. Điểm năng lực không được tổng hợp theo nhãn chữ. Lượt thay thế (câu kỹ năng rộng hơn hoặc câu theo vai trò) ghi khái niệm thực sự được hỏi, không phải khái niệm mục tiêu. Loại khỏi sổ: lượt `graded = false`, các id giả `warmup` và `cv-validation`, phiên `TECHNICAL_FAILURE`, phiên khởi động F-07. | Hợp đồng §1.4.1 · Code — `competency_scores` hiện lưu theo nhãn tự do; `concept_id` có trong `question_snapshot.taxonomyTarget` |
| P-02 | P0 | **Chấm theo mốc rubric.** Mô hình chấm chọn mức 0–3 cho từng tiêu chí, kèm trích dẫn cho mức đó; điểm lượt được tính bằng code từ mức × trọng số, không để mô hình tự cho một số 0–10. | Hợp đồng §1.1.10 · Code — mốc và trọng số đã có trong snapshot nhưng không được dùng |
| P-03 | P0 | **Ngưỡng nhiễu.** Bộ hiệu chuẩn gồm các câu trả lời cố định, chấm lại k lần (k = 5) mỗi khi đổi mô hình, prompt hoặc rubric. Tính sai số chuẩn đo lường (SEM) theo khái niệm và ngưỡng thay đổi tối thiểu MDC95 = 1,96 × √2 × SEM. Khoảng tin cậy 95% quanh **một** điểm là ±1,96 × SEM (= MDC95 / √2); MDC95 là ngưỡng cho **hiệu** hai điểm. SEM đo bằng cách chấm lại cùng câu trả lời chỉ phản ánh dao động của bộ chấm, nên là **cận dưới**: giữa hai buổi còn có độ khó câu hỏi và phong độ của ứng viên. Giá trị: *Chờ đo*. | Hợp đồng §1.4.2 · Khảo sát — arXiv 2506.22316, 2603.04417 |
| P-13 | P0 | **Trạng thái rỗng trung thực.** Người mới chưa có buổi nào thấy cách hệ thống đo (sai số, câu neo) và nút bắt đầu buổi đầu tiên; không hiển thị số giả hay "—" vĩnh viễn. "Thời gian luyện tập" tính từ `startedAt` và `endedAt` đã có. | Code — frontend `UserDashboardHome.tsx`; `ProgressPreview.tsx` có sẵn nhưng chưa dùng |
| P-04 | P1 | **Chỉ báo tiến bộ ba mức** cho mỗi khái niệm: **TIẾN BỘ THẬT**, **TRONG BIÊN NHIỄU**, **GIẢM**. Chỉ được kết luận theo một trong hai cách: (a) **cặp câu neo** — hai lần trả lời cùng một câu neo, cùng `scorer_version`, ngưỡng MDC95; (b) **cửa sổ** — trung bình k quan sát gần nhất so với k quan sát trước đó, cùng `scorer_version`, ngưỡng MDC95 / √k. Hai lượt đơn lẻ trên hai câu khác nhau **không** được gắn TIẾN BỘ THẬT hay GIẢM. Khái niệm chưa đủ dữ liệu hiển thị "cần thêm lần đo". Không dùng mũi tên xanh hay chữ "tiến bộ" cho thay đổi trong biên nhiễu. | Hợp đồng §1.4.3, SF-07 |
| P-05 | P1 | **Câu neo.** Mỗi khái niệm trong taxonomy đang dùng có ít nhất một câu đã duyệt được đánh dấu neo. Câu neo được hỏi lại sau tối thiểu 7 ngày; bộ chọn câu không dùng câu neo cho mục đích khác. So sánh trước/sau ưu tiên cặp quan sát trên cùng câu neo. | Hợp đồng §1.4.4 · Code — có xoay vòng câu theo người dùng (`question_selector.py`, `_question_exposure`) |
| P-06 | P1 | **So sánh trước/sau.** Với câu đã trả lời từ hai lần, hiển thị hai câu trả lời cạnh nhau, mức từng tiêu chí và tiêu chí nào đã đổi mức. | Đề xuất |
| P-07 | P1 | **Bản đồ phòng thủ CV.** Từ CV đã bóc tách, liệt kê các tuyên bố có thể bị hỏi (dự án, vai trò, con số, công nghệ), mỗi mục kèm đoạn bằng chứng trong CV. Mỗi mục có trạng thái **CHƯA HỎI / VỮNG / LUNG LAY**. Phiên sau ưu tiên hỏi mục CHƯA HỎI và LUNG LAY. Thay câu VALIDATE mẫu hiện tại. | Hợp đồng §1.4.5 · Code — bộ bóc tách CV evidence-first, `project_evidence.py` |
| P-08 | P1 | **Mức sẵn sàng theo JD mục tiêu.** Với JD ứng viên chọn làm mục tiêu: mỗi khái niệm bắt buộc hiển thị mức hiện tại, khoảng tin cậy và mức JD cần. Tổng quan là **số khái niệm bắt buộc đã đạt / tổng số**, không phải một phần trăm duy nhất. Thay ô "Readiness" đang hiển thị "—". | Đề xuất · Code — frontend `UserDashboardHome.tsx` |
| P-09 | P2 | **Buổi luyện kế tiếp.** Gợi ý khái niệm có (độ quan trọng trong JD mục tiêu × khoảng cách tới mức cần × độ bất định) lớn nhất, kèm một câu giải thích vì sao. | Đề xuất |
| P-10 | P2 | **Ngày phỏng vấn thật.** Ứng viên nhập ngày; hệ thống chia các buổi ngắn rải đều đến ngày đó thay vì một buổi dài sát ngày. | Khảo sát — iatrox.com, andymatuschak.org (bằng chứng riêng cho phỏng vấn còn mỏng) |
| P-11 | P2 | **Tóm tắt tuần** trong ứng dụng: số buổi, khái niệm TIẾN BỘ THẬT, khái niệm cần đo lại, buổi gợi ý. | Đề xuất |

### 7.7 Dữ liệu và API đề xuất

| Bảng | Cột chính | Ghi chú |
|---|---|---|
| `skill_observations` | `user_id`, `session_id`, `turn_id`, `concept_id`, `taxonomy_version`, `question_version_id`, `rubric_version_id`, `scorer_version`, `is_anchor`, `criterion_levels` (JSONB), `score`, `observed_at` | Một dòng cho mỗi lượt đã chấm hợp lệ. Không lưu nội dung câu trả lời. |
| `scorer_calibrations` | `scorer_version`, `concept_id`, `k`, `sem`, `mdc95`, `measured_at` | Mỗi lần đổi mô hình, prompt hoặc rubric là một `scorer_version` mới. |
| `cv_claims` | `resume_id`, `claim_id`, `kind`, `text`, `evidence_span`, `status`, `last_probed_session_id` | Phục vụ P-07. |
| `practice_goals` | `user_id`, `target_job_id`, `interview_date`, `created_at` | Phục vụ P-08 đến P-10. |

Bảng `scoring_history` (migration 0001) hiện không có ai dùng; thay bằng `skill_observations` hoặc xóa.

| Endpoint | Trả về |
|---|---|
| `GET /api/v1/progress/overview` | Số buổi, thời gian luyện, khái niệm TIẾN BỘ THẬT gần đây, buổi gợi ý |
| `GET /api/v1/progress/skills` | Mỗi khái niệm: ước lượng hiện tại, khoảng tin cậy, số quan sát, chỉ báo P-04 |
| `GET /api/v1/progress/skills/{conceptId}` | Chuỗi quan sát theo thời gian, đánh dấu chỗ đổi `scorer_version` |
| `GET /api/v1/progress/readiness?jobId=` | P-08 cho một JD |
| `GET /api/v1/progress/cv-defense?resumeId=` | P-07 |
| `PUT /api/v1/progress/goal` | Đặt JD mục tiêu và ngày phỏng vấn |

### 7.8 Màn hình

| Màn hình | Nội dung | Thay cho |
|---|---|---|
| Bảng điều khiển | Mức sẵn sàng theo JD mục tiêu (x/y khái niệm đạt), buổi gợi ý kế tiếp, thời gian luyện tuần này | Ô "Readiness" và "Practice time" đang hiện "—" |
| Hành trình | Dòng thời gian các buổi; mỗi buổi ghi khái niệm nào TIẾN BỘ THẬT, khái niệm nào GIẢM | Danh sách 5 buổi gần nhất không có điểm |
| Sổ kỹ năng | Mỗi khái niệm một hàng: điểm hiện tại, dải tin cậy, đường xu hướng nhỏ, nhãn P-04 | Biểu đồ radar của một buổi |
| Chi tiết kỹ năng | Biểu đồ điểm theo thời gian có dải nhiễu; đánh dấu câu neo và chỗ đổi `scorer_version`; so sánh trước/sau | — |
| Phòng thủ CV | Từng tuyên bố trong CV với trạng thái CHƯA HỎI / VỮNG / LUNG LAY và đoạn trả lời gần nhất | Câu VALIDATE mẫu |
| Báo cáo buổi | Thêm khối "So với lần trước" theo khái niệm; tab mặc định là phản hồi cho ứng viên | Tab nhà tuyển dụng đang mặc định |

### 7.9 Luật trích dẫn bằng chứng cho R-11

Trích dẫn được coi là hợp lệ khi, sau khi chuẩn hóa khoảng trắng, dấu câu và chữ hoa, nó là chuỗi con liên tục của câu trả lời trong cùng lượt. Kết quả có ba mức:

| Kết quả | Điều kiện | Xử lý |
|---|---|---|
| KHỚP | Là chuỗi con sau chuẩn hóa. | Giữ nguyên. |
| GẦN KHỚP | Khác tối đa N ký tự do lỗi nhận dạng giọng nói. N: *Chờ đo*. | Giữ, thay bằng đoạn gốc trong câu trả lời. |
| KHÔNG KHỚP | Không thuộc hai trường hợp trên. | Loại trích dẫn. Lượt không còn trích dẫn nào thì `graded = false`. |

---

## 8. Yêu cầu phi chức năng

| ID | Yêu cầu | Nguồn |
|---|---|---|
| NFR-01 | Phản hồi của người phỏng vấn ở kênh chat có p95 dưới 8 giây mỗi lượt; kênh giọng nói p95 dưới 1,5 giây từ lúc ứng viên ngừng nói tới tiếng đầu tiên. Cả hai con số: *Chờ đo*. | Đề xuất · trục chỉ số Hợp đồng §5 |
| NFR-02 | Trạng thái phiên chỉ nằm trong PostgreSQL và checkpoint của LangGraph; không giữ trạng thái phiên trong bộ nhớ tiến trình. | Hợp đồng §4.2.2 · Code — `agent/checkpoints.py` |
| NFR-03 | Tác vụ xử lý CV, JD và đối chiếu chạy qua hàng đợi, có timeout, retry và giải phóng bản ghi bị treo. | Code — `src/workers/` |
| NFR-04 | Dữ liệu gửi ra mô hình bên thứ ba chỉ chứa những gì cần cho lượt đó; không gửi họ tên, số điện thoại, email từ CV. | Đề xuất · Luật Bảo vệ dữ liệu cá nhân 2025 [Giả định — cần đối chiếu nguyên văn] |
| NFR-05 | Có chính sách lưu giữ cho bản ghi phiên, checkpoint và audio. Thời hạn: *Chờ xác định*. | Code — `docs/INTERVIEW_AGENT_MODULE_MAP.md` (mục việc còn lại) |
| NFR-06 | Mỗi lượt ghi một sự kiện trace (thời gian, hành động, loại tin nhắn, độ dài nội dung), không ghi nội dung câu trả lời vào log. | Code — `trace_event` trong `api/router.py` |
| NFR-08 | Mọi điểm lưu kèm `scorer_version`. Chuỗi tiến bộ được ngắt và đánh dấu tại chỗ đổi `scorer_version`; điểm trước và sau không được nối thành một đường liên tục khi chưa hiệu chuẩn lại. | Hợp đồng §1.4.2 · Đề xuất |
| NFR-09 | Bộ hiệu chuẩn chấm (P-03) chạy trong CI mỗi khi prompt chấm, mô hình chấm hoặc rubric thay đổi; MDC95 tăng quá ngưỡng thì chặn phát hành. Ngưỡng: *Chờ xác định*. | Đề xuất |
| NFR-07 | Có thể thay nhà cung cấp mô hình mà không sửa lõi phỏng vấn. | Code — `docs/INTERVIEW_AGENT_MODULE_MAP.md` (chưa có LLM port) · Đề xuất |

---

## 9. Tiêu chí nghiệm thu

Mỗi ca chuẩn (golden case) chạy qua HTTP và được chấm bằng bản ghi phiên và bản ghi đánh giá sau khi phiên đóng, không chấm bằng transcript.

| Ca | Thiết lập | Kỳ vọng |
|---|---|---|
| GC-PLAN-01 | JD có 3 năng lực, ngân hàng đủ câu cho cả 3. | `POST /plan` rồi `/questions/select` thành công; số lượt chốt khớp kế hoạch. |
| GC-PLAN-02 | Không năng lực nào của JD có câu trong ngân hàng, `QUESTION_GENERATION_ENABLED=false`. | 409, `detail.errorCode = question_bank_insufficient`; phiên không mở. |
| GC-PLAN-02b | JD có 3 năng lực, chỉ 2 có câu, tắt sinh câu hỏi. | Chốt thành công với 2 mục; không trả 409. |
| GC-PLAN-03 | Như GC-PLAN-02 nhưng bật sinh câu hỏi. | Câu sinh ra có trạng thái IN_REVIEW; phiên mở được. |
| GC-RUN-01 | Ứng viên trả lời mơ hồ "ý anh là sao?". | Hành động CLARIFY; lượt chưa hoàn thành; không chấm. |
| GC-RUN-02 | Ứng viên gõ "tôi muốn dừng". | Tin nhắn CONFIRM_ABORT; phiên vẫn OPEN đến khi ứng viên xác nhận. |
| GC-RUN-03 | Gửi hai lần cùng `clientMessageId`. | Chỉ một lượt trả lời được ghi. |
| GC-END-01 | Client gọi `/close` với `reason = COMPLETED`. | 422; phiên không chuyển sang COMPLETED. |
| GC-END-02 | Client gọi `/chat/complete` khi mới trả lời 1/5 câu. | 409; phiên không chuyển sang COMPLETED. |
| GC-EVAL-01 | Mô hình chấm trả một trích dẫn không có trong câu trả lời. | Trích dẫn bị loại (R-11). |
| GC-EVAL-02 | Mô hình chấm trả JSON hỏng cho một lượt. | Lượt đó `graded = false`, không tính vào điểm tổng. |
| GC-SEC-01 | Ứng viên A gọi `GET /sessions/{id}` của ứng viên B. | 404, không lộ sự tồn tại của phiên. |
| GC-SEC-02 | Gọi `POST /api/v1/matching/match-ids` không có token, và có token của A với id CV của B. | 401 cho trường hợp đầu; 404 cho trường hợp sau. **Hiện trượt.** |
| GC-PROG-01 | Ứng viên xóa CV đã dùng cho 3 phiên. | Cả 3 phiên và báo cáo vẫn xem được (P-12). **Hiện trượt.** |
| GC-PROG-02 | Hai phiên với hai JD khác nhau cùng có câu thuộc khái niệm `caching`. | `GET /progress/skills` trả một dòng `caching` với 2 quan sát (P-01). |
| GC-PROG-03 | Hai lần trả lời cùng câu neo `caching`, điểm tăng 0,4, MDC95 = 0,9. | Nhãn TRONG BIÊN NHIỄU; không có chữ "tiến bộ" (SF-07). |
| GC-PROG-04 | Hai lần trả lời cùng câu neo, điểm tăng 1,5, MDC95 = 0,9. | Nhãn TIẾN BỘ THẬT. |
| GC-PROG-04b | Hai lượt liền nhau trên **hai câu khác nhau**, điểm tăng 2,0. | Không gắn TIẾN BỘ THẬT; chỉ được dùng trong ước lượng cửa sổ (P-04b). |
| GC-PROG-05 | Giữa hai quan sát có đổi `scorer_version`. | Chuỗi bị ngắt và đánh dấu; không tính chỉ báo P-04 qua chỗ ngắt (NFR-08). |
| GC-PROG-06 | Phiên đóng với `TECHNICAL_FAILURE`, và một phiên khởi động F-07. | Không sinh quan sát nào trong sổ kỹ năng. |
| GC-PROG-07 | Lượt `graded = false`, và lượt `warmup`. | Không sinh quan sát nào. |
| GC-PROG-08 | Người dùng mới, chưa có phiên. | Bảng điều khiển không hiện số giả hay "—"; có giải thích cách đo và nút bắt đầu (P-13). |
| GC-REP-01 | Ứng viên gọi `GET /sessions/{id}/evaluation`. | Không có trường `decision_recommendation` / `decisionRecommendation` và không có chuỗi PASS, REJECT, STRONG_PASS, CONSIDER (R-15). **Hiện trượt.** |

---

## 10. Chỉ số thành công

Trọng số dưới đây là đề xuất của nhóm cho bộ ca kiểm thử nội bộ, chưa được hội đồng chốt. [Đề xuất · Hợp đồng §5]

| Chỉ số | Trọng số |
|---|---|
| Ca kết thúc ở đúng trạng thái phiên và có bản ghi đánh giá hợp lệ | 35% |
| Số severe failure (SF-01 đến SF-07), cộng tỉ lệ phục hồi sau một lần hiểu nhầm | 25% |
| p95 độ trễ mỗi lượt và chi phí mô hình mỗi phiên | 15% |
| Chất lượng test, vận hành và tài liệu bàn giao | 25% |

**Cổng, không phải trọng số.** Một buổi phỏng vấn giọng nói thật chạy trọn vẹn trong buổi bảo vệ. [Hợp đồng §5.2]

**Chỉ số kết quả** (tỉ lệ người dùng quay lại luyện lần hai, điểm cải thiện sau F-02, tỉ lệ phiên hoàn thành): *Không quy định*. Chưa có người dùng thật để có số nền.

**Chỉ số cho Hồ sơ tiến bộ** [Đề xuất], tất cả *Chờ đo*:

- MDC95 trung vị theo khái niệm trên bộ hiệu chuẩn — càng nhỏ, hệ thống càng phát hiện được tiến bộ nhỏ.
- Tỉ lệ người dùng có ít nhất 2 quan sát trên cùng một khái niệm trong 14 ngày — điều kiện để hồ sơ tiến bộ có nghĩa.
- Tỉ lệ khái niệm đạt TIẾN BỘ THẬT sau 3 buổi luyện có mục tiêu.
- Tỉ lệ quay lại sau 7 ngày và 30 ngày.

Những chỉ số đáng định nghĩa nhưng hiện chưa đo:

- Tỉ lệ phiên đóng với TECHNICAL_FAILURE — *Chờ xác định*
- Tỉ lệ lượt `graded = false` — *Chờ xác định*
- Độ lệch điểm giữa hai lần chấm cùng một câu trả lời — *Chờ xác định*

---

## 11. Ràng buộc và phụ thuộc

### Ràng buộc hợp đồng

- Các mục §1 đến §4 của INTV-PC-001 bị đóng băng sau khi chốt Rev 1.0; chỉ được làm rõ, không được mở rộng. [Hợp đồng §8]
- Ứng viên không được thấy nhãn tuyển hay loại. [Hợp đồng SF-04]

### Ràng buộc nhân sự và tiến độ

- Quy mô nhóm và mốc bảo vệ: *Chờ điền*.

### Ràng buộc kỹ thuật

- Toàn bộ mô hình (ngôn ngữ, embedding, nhận dạng và tổng hợp tiếng nói) đang dùng một nhà cung cấp là OpenAI. [Code — `src/core/config.py`]
- Kênh giọng nói và video phụ thuộc LiveKit. [Code — `adapters/voice/livekit_agent.py`]
- Schema ngân hàng câu hỏi được tạo bằng `create_all` ngoài Alembic. [Code — `README.md`]
- Bộ lập kế hoạch động, câu hỏi làm rõ khi đối chiếu và Voice Lab đang tắt mặc định. [Code — tắt mặc định]

### Ràng buộc pháp lý [Khảo sát / Giả định]

- Luật Bảo vệ dữ liệu cá nhân 2025 thay Nghị định 13/2023 từ 01/01/2026. CV chứa dữ liệu cá nhân; cần đồng ý rõ ràng trước khi xử lý.
- Bộ luật Lao động 2019 cấm phân biệt đối xử trong tuyển dụng; các câu hỏi ở R-16 không được xuất hiện dù chỉ là luyện tập. [Giả định — cần đối chiếu điều khoản cụ thể]

### Phụ thuộc

- Bộ JD mẫu (Greenhouse và demo) do quản trị viên nạp. [Code — `scripts/ingest_greenhouse_jobs.py`, `scripts/demo_jobs/`]
- Ngân hàng câu hỏi đã duyệt đủ độ phủ cho các JD được xuất bản. [Code — `scripts/question_coverage_report.py`]

## 12. Giả định

| ID | Giả định |
|---|---|
| A-1 | Sinh viên Việt Nam sẵn sàng luyện phỏng vấn với AI bằng tiếng Việt. Chưa kiểm chứng bằng khảo sát người dùng. |
| A-2 | Câu hỏi bám CV có giá trị cao hơn câu hỏi chung theo JD. Chưa có thử nghiệm A/B. |
| A-3 | Điểm 0–10 từ mô hình đủ ổn định để so sánh tiến bộ của cùng một người qua các buổi, không đủ để so sánh giữa nhiều người. [Khảo sát — interviewdrills.com nêu nhận định tương tự cho công cụ khác] |
| A-4 | Thời lượng 15–25 phút phù hợp với một buổi luyện đầy đủ. |

## 13. Rủi ro

### Rủi ro đến từ khoảng trống giữa hợp đồng và code

| ID | Rủi ro |
|---|---|
| RK-1 | **Trích dẫn chưa được kiểm.** `evidence_quotes` hiện lấy nguyên từ đầu ra mô hình (`evaluation_engine.py`) mà không đối chiếu với câu trả lời. Một trích dẫn bịa là SF-01. Giảm rủi ro bằng luật ở mục 7.9. |
| RK-2 | **Nhãn tuyển/loại đang được trả cho ứng viên.** `get_session_evaluation` (`evaluation/evaluation_service.py`) trả `decision_recommendation` và `decisionRecommendation` (STRONG_PASS, PASS, CONSIDER, REJECT) qua `GET /sessions/{id}/evaluation` và `/report`, là endpoint của chính ứng viên. Đây là SF-04 đang xảy ra. Cần bỏ trường này khỏi API phía ứng viên hoặc đổi thành mức sẵn sàng ("cần luyện thêm / khá / sẵn sàng"). |
| RK-3 | **Chưa có bộ lọc câu hỏi phân biệt đối xử.** Câu hỏi do AI sinh và câu PROBE chưa qua bộ lọc thuộc tính được bảo vệ. |
| RK-10 | **Endpoint đối chiếu không xác thực, đọc CV không lọc người dùng.** Ai biết id CV đều đọc được dữ liệu CV của người khác và có thể đốt chi phí mô hình. [Code — `matching/api/router.py`, `matching/application/input_resolver.py:25`] |
| RK-11 | **Xóa CV làm mất lịch sử.** Phá hỏng mọi tính năng tiến bộ từ gốc. Xem P-12. |
| RK-12 | **Marketing tuyên bố không có căn cứ.** Frontend hiển thị "98% Accuracy", "Sentiment Analysis Engine" và lời chứng thực của nhân vật hư cấu; thanh toán VietQR là giả; credit không bao giờ bị trừ. [Code — frontend `i18n.ts`, `VietQrModal.tsx`] |
| RK-13 | **Biểu đồ tiến bộ vẽ nhiễu.** Nếu làm màn hình trước khi có P-01 đến P-03, người dùng sẽ thấy "tiến bộ" và "thụt lùi" do mô hình chấm dao động, và mất niềm tin khi nhận ra. |
| RK-14 | **Ngân hàng câu hỏi mỏng.** 130 câu seed dùng chung một rubric chung chung, không có ý bắt buộc; P-02 chỉ có giá trị khi rubric từng câu đủ cụ thể. |
| RK-15 | **Đường tiến bộ phẳng làm người dùng bỏ đi.** Nếu MDC95 thật vào khoảng 1 điểm trên thang 10, phần lớn thay đổi giữa hai buổi sẽ là TRONG BIÊN NHIỄU; người dùng thấy đường phẳng và nghĩ luyện không có tác dụng. Giảm rủi ro bằng: nhảy mức 0–3 ở từng tiêu chí (rời rạc, dễ thấy hơn điểm tổng); ước lượng cửa sổ có sai số giảm dần khi tích lũy quan sát; tiến bộ không dựa vào điểm (tuyên bố CV chuyển sang VỮNG, số khái niệm đã đo, câu neo đã làm lại). |

### Rủi ro đến từ các quyết định chưa chốt

| ID | Rủi ro |
|---|---|
| RK-4 | Chất lượng đối chiếu CV–JD được đánh giá là thấp và chưa có gold corpus. F-03 xây trên nền này sẽ khuếch đại sai số. [Code — `docs/CV-JD-MATCHING-ASSESSMENT.md`] |
| RK-5 | Lõi phỏng vấn chưa chuyển hết sang LangGraph, nên R-13 mới đạt một phần; `text_runtime.py` và endpoint video legacy chưa đi qua graph. Hai đường chạy có thể cho kết quả khác nhau. [Code — `docs/INTERVIEW_AGENT_MODULE_MAP.md`] |
| RK-6 | Sự cố giữa lúc commit DB và lúc ghi checkpoint chưa khôi phục trọn vẹn. [Code — `docs/INTERVIEW_AGENT_MODULE_MAP.md`] |

### Rủi ro nhận ra từ khảo sát

| ID | Rủi ro |
|---|---|
| RK-7 | **Độ tin cậy là điểm đau số một của nhóm sản phẩm này.** Người dùng Huru báo công cụ không hỏi câu nào; Final Round AI có tỉ lệ đánh giá 1 sao đáng kể, tập trung vào thanh toán. [Khảo sát — finalroundai.com, hiredkit.ai] |
| RK-8 | **Ứng viên ngờ vực phỏng vấn AI.** 38% người được Greenhouse hỏi từng rời quy trình tuyển dụng vì có phỏng vấn AI. Sản phẩm luyện tập phải nói rõ mình không chấm thay nhà tuyển dụng. [Khảo sát — greenhouse.com 2026] |
| RK-9 | **Phụ thuộc một nhà cung cấp mô hình.** Đổi giá hoặc ngừng mô hình ảnh hưởng toàn bộ hệ thống. |

## 14. Những gì tài liệu này không bao gồm

- Kiến trúc chi tiết và lựa chọn mô hình. Ghi trong `docs/MODULE-MVC-ARCHITECTURE.md` và `docs/INTERVIEW_AGENT_MODULE_MAP.md`.
- Câu chữ hội thoại ở mức từng câu.
- Kế hoạch sprint và chia nhỏ công việc.

---

**Ghi chú về truy vết.** Không một yêu cầu, chỉ số, ràng buộc hay rủi ro nào trong tài liệu này được đưa vào mà không có nguồn trong hợp đồng, mã nguồn, khảo sát, hoặc được đánh dấu rõ là Đề xuất hay Giả định.

### Nguồn khảo sát

- HCMUE — khó khăn khi xin việc của sinh viên: https://journal.hcmue.edu.vn/index.php/hcmuejos/article/download/2041/2025
- Adobe — nghiên cứu chuẩn bị phỏng vấn: https://www.adobe.com/acrobat/resources/resume-preparation-research.html
- Greenhouse 2026 — ứng viên và phỏng vấn AI: https://www.greenhouse.com/uk/newsroom/63-of-job-seekers-have-faced-an-ai-interview-most-havent-had-a-good-one-yet
- Axios — Greenhouse và tình trạng ghosting: https://www.axios.com/2024/09/24/greenhouse-job-ghosting-hiring-rejection-email
- HR Dive — khảo sát Greenhouse: https://www.hrdive.com/news/job-applications-longer-than-15-minutes-lose-candidate-interest/618169/
- So sánh công cụ luyện phỏng vấn AI 2026: https://mocky.pro/en/blog/ai-mock-interview-tools-compared · https://favtutor.com/best-ai-mock-interview-tools-2026/ · https://interviewdrills.com/blog/best-ai-mock-interview-tools · https://www.hiredkit.ai/blog/best-final-round-ai-alternatives-2026
- Đánh giá Huru (do đối thủ viết): https://www.finalroundai.com/blog/huru-review-pros-cons
- ChatGPT cho luyện phỏng vấn: https://mockif.com/chatgpt-mock-interview · https://www.makeuseof.com/how-use-chatgpt-voice-mode-interview-preparation/ · https://mocky.pro/en/blog/chatgpt-vs-gemini-interview-prep
- Bảng điều khiển tiến bộ của đối thủ: https://www.tryexponent.com/blog/new-my-progress-dashboard · https://yoodli.ai/blog/introducing-analytics-centralized-insights-within-yoodli · https://www.producthunt.com/posts/1239902
- Độ không ổn định của LLM khi chấm: https://arxiv.org/abs/2506.22316 · https://www.alphaxiv.org/abs/2603.04417 · https://arxiv.org/abs/2509.21117v2
- Luyện tập có chủ đích: https://notes.andymatuschak.org/zAEV61QhJaiWLYQ8vZF36uT · https://www.iatrox.com/blog/unlimited-ai-osce-practice-deliberate-practice-or-rehearsing-mistakes

*INTV-PRD-001 · v1.1 · INTERVIA*
