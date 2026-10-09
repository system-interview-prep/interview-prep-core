# INTERVIA — Product Requirements Document

**Phiên bản 1.0 · 09/10/2026**

| Hạng mục | Nội dung |
|---|---|
| Sản phẩm | INTERVIA — nền tảng luyện phỏng vấn bằng AI, bám CV và JD |
| Chủ sở hữu tài liệu | Nhóm KLTN INTERVIA — PM / PO (Chờ điền tên) |
| Hợp đồng nguồn | INTERVIA Product Contract — INTV-PC-001 Rev 1.0 |
| Nguồn bổ sung | Mã nguồn `interview-prep-core` nhánh `feat/interview-langgraph` (09/10/2026); thư mục `docs/` của repo; khảo sát thị trường 09/10/2026 |
| Trạng thái | Bản thảo, chờ review |

---

## Record of Changes

A = Thêm · M = Sửa · D = Xoá

| Ngày | A/M/D | Phiên bản | Nội dung thay đổi | Người thực hiện |
|---|---|---|---|---|
| 09/10/2026 | A | 1.0 | Bản thảo đầu tiên. Mục 1–14 rút từ hợp đồng INTV-PC-001 Rev 1.0, mã nguồn hiện tại và khảo sát thị trường ngày 09/10. Thêm 7 tính năng đề xuất F-01 đến F-07 ở mục 7.3. Ghi nhận 3 khoảng trống giữa hợp đồng và code ở mục 13 (RK-1 đến RK-3); RK-2 đã xác nhận là vi phạm đang xảy ra. | Nhóm INTERVIA |

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

---

## 7. Yêu cầu

### 7.1 Rút từ hợp đồng — Bắt buộc

| ID | Yêu cầu | Nguồn |
|---|---|---|
| R-01 | CV phải được xử lý bất đồng bộ, báo tiến độ cho ứng viên, và cho phép ứng viên xem và sửa dữ liệu đã trích xuất trước khi dùng. Trích xuất phải có đường dự phòng khi dịch vụ OCR chính lỗi. | Hợp đồng §1.1.1 · Code — `src/modules/user_cvs/` |
| R-02 | Đối chiếu CV–JD phải trả kết quả theo từng yêu cầu của JD, mỗi kết luận kèm bằng chứng trích từ CV. Không được dùng điểm đối chiếu để kết luận đạt hay trượt. | Hợp đồng §1.1.2, §1.2.1 · Code — `src/modules/matching/` |
| R-03 | Kế hoạch phỏng vấn phải được lập từ CV, JD và kết quả đối chiếu đã chuẩn hóa; mỗi mục trong kế hoạch phải gắn với một năng lực và, khi có, một dự án trong CV. | Hợp đồng §1.1.3 · Code — `planning/planner.py`, `planning/project_evidence.py` |
| R-04 | Câu hỏi phải được chốt (freeze) trước khi phiên mở. Sau khi chốt, bộ câu hỏi của phiên không đổi. | Hợp đồng §1.1.4, SF-02 · Code — `planning/question_selector.py` |
| R-05 | Câu hỏi chỉ đến từ ngân hàng đã duyệt. Câu do AI sinh để lấp chỗ thiếu phải mang trạng thái IN_REVIEW và người duyệt phải khác người soạn. Khi ngân hàng không phủ được mục nào trong kế hoạch thì trả 409 `question_bank_insufficient` và không mở phiên; thiếu một phần thì phiên vẫn mở với các mục đã phủ. | Hợp đồng §1.1.5 · Code — `planning/question_generation.py`, `src/modules/question_bank/` |
| R-06 | Phiên chạy theo máy trạng thái WARM_UP → VALIDATE → DEEP_DIVE → CHALLENGE → BEHAVIORAL → CLOSING → CLOSED. Mô hình ngôn ngữ không được tự chuyển giai đoạn. | Hợp đồng §1.1.6 · Code — `core/interview_engine.py` |
| R-07 | Mỗi câu hỏi được đào sâu (PROBE) tối đa một lần. Khi ứng viên hỏi lại hoặc trả lời mơ hồ, hệ thống làm rõ (CLARIFY) thay vì chấm. | Hợp đồng §1.1.7 · Code — `core/interview_types.py` |
| R-08 | Ứng viên muốn dừng sớm thì hệ thống phải hỏi xác nhận (CONFIRM_ABORT) trước khi đóng phiên. | Hợp đồng §1.1.8 · Code — `application/chat_runtime.py` |
| R-09 | Một phiên chỉ được đóng với lý do COMPLETED khi đã phủ đủ agenda. Endpoint đóng phiên chung không nhận COMPLETED. Lý do do hệ thống quyết định (HARD_TIMEOUT, FAST_FAIL_TECH) không được nhận từ client. | Hợp đồng §2.2, SF-06 · Code — `api/router.py` (`CloseInterviewSession`, `ClientEndReason`) |
| R-10 | Mỗi lượt trả lời được chấm trên thang 0–10, kèm phân tích STAR, trích dẫn bằng chứng, điểm mạnh, điểm yếu và câu trả lời mẫu. Lượt mô hình không trả điểm hợp lệ phải đánh dấu `graded = false`, không được tính vào điểm tổng. | Hợp đồng §1.1.10 · Code — `evaluation/evaluation_types.py`. Lưu ý: `GET /evaluation` hiện chưa trả `graded` và `what_good_looks_like` cho ứng viên. |
| R-11 | Mọi `evidence_quotes` phải là trích nguyên văn từ câu trả lời của ứng viên trong chính lượt đó. | Hợp đồng §1.1.11, SF-01 · **Chưa có trong code — xem RK-1** |
| R-12 | Ứng viên chỉ đọc và ghi được phiên, CV và báo cáo của chính mình. | Hợp đồng SF-03 · Code — `api/router.py` (`_owned_session`) |
| R-13 | Ba kênh chat, giọng nói và video phải đi qua cùng một lõi phỏng vấn; kênh chỉ chuyển lời nói thành văn bản cuối và ngược lại. | Hợp đồng §1.3.2 · Code — `agent/graph.py`, `adapters/` · **Một phần — xem RK-5** |
| R-14 | Mọi lệnh gửi tin nhắn mang `clientMessageId`; gửi lại cùng một id không được tạo lượt mới. | Hợp đồng §4.2.3 · Code — `api/router.py` (`SendChatMessage`) |
| R-15 | Báo cáo hiển thị cho ứng viên không được chứa nhãn tuyển hay loại (PASS, REJECT, STRONG_PASS, CONSIDER). | Hợp đồng SF-04 · **Đang vi phạm trong code — xem RK-2** |
| R-16 | Hệ thống không được hỏi về tuổi, tình trạng hôn nhân, kế hoạch sinh con, tôn giáo, dân tộc, quê quán hay sức khỏe. | Hợp đồng SF-05 · Chưa có bộ lọc — xem RK-3 |

### 7.2 Rút từ hợp đồng — Nên có

| ID | Yêu cầu | Nguồn |
|---|---|---|
| R-17 | Thời lượng phiên do ứng viên chọn trong khoảng 2–120 phút; số câu hỏi co giãn theo ngân sách thời gian. | Hợp đồng §1.1.9 · Code — `CreateInterviewSession.duration_minutes`, `docs/INTERVIEW_DYNAMIC_QUESTION_BUDGET_PROPOSAL.md` |
| R-18 | Câu xác minh CV ở giai đoạn VALIDATE nên hỏi mở về một chi tiết trong CV, không đọc chi tiết đó ra rồi hỏi "đúng không". | Hợp đồng §1.1.6 · Đề xuất |
| R-19 | Báo cáo nên có hai phần tách bạch: phần phản hồi cho ứng viên và phần ghi chú kỹ thuật; phần ghi chú kỹ thuật không hiển thị cho ứng viên. | Hợp đồng SF-04 · Đề xuất |

### 7.3 Tính năng mới đề xuất

Mỗi tính năng ghi rõ điểm đau và nguồn, "đúng" nghĩa là gì theo dạng §1.1 của hợp đồng, và nhóm phải cắt gì để có chỗ làm. Tính năng không có đánh đổi không được duyệt. [Hợp đồng §7.3]

| ID | Tính năng | Điểm đau và nguồn | "Đúng" nghĩa là | Đánh đổi |
|---|---|---|---|---|
| F-01 | **Viết lại câu trả lời từ chính CV của bạn.** Với mỗi lượt điểm dưới 7, hệ thống viết lại câu trả lời theo STAR chỉ dùng dữ kiện có trong CV và câu trả lời gốc, đánh dấu từng dữ kiện lấy từ đâu. | Công cụ hiện có chấm cách nói, không chấm nội dung. [Khảo sát — finalroundai.com, mocky.pro] | Mọi con số, tên dự án, công nghệ trong câu viết lại phải có trong CV hoặc câu trả lời gốc. Không có dữ kiện thì để ô trống có nhãn "bạn cần bổ sung", không tự bịa. | Thay thế trường `what_good_looks_like` hiện tại, không thêm lời gọi mô hình mới. Hoãn phần phân tích video. |
| F-02 | **Vòng luyện lại câu yếu nhất.** Sau báo cáo, ứng viên trả lời lại 1–3 câu điểm thấp nhất trong một phiên ngắn, nhận điểm mới và độ chênh so với lần đầu. | Người dùng muốn theo dõi tiến bộ của chính mình. [Khảo sát — interviewdrills.com] | Phiên luyện lại dùng đúng phiên bản câu hỏi và rubric đã chốt của lần đầu; độ chênh chỉ tính giữa hai lần chấm cùng rubric. | Dùng lại `experience_type = question_practice`. Cắt màn hình lịch sử chi tiết. |
| F-03 | **Kế hoạch lấp khoảng trống so với JD.** Từ các yêu cầu JD chưa khớp trong kết quả đối chiếu, hệ thống đề xuất kế hoạch luyện 7 ngày và một phiên phỏng vấn tập trung vào các năng lực đó. | Ứng viên không biết nên luyện gì tiếp. [Giả định] | Mỗi mục trong kế hoạch trỏ về một yêu cầu JD cụ thể có kết quả "chưa khớp" hoặc "thiếu bằng chứng". Không tạo mục cho yêu cầu đã khớp. | Phụ thuộc chất lượng đối chiếu, hiện đánh giá là thấp. [Code — `docs/CV-JD-MATCHING-ASSESSMENT.md`] Phải làm bộ gold corpus trước. |
| F-04 | **"AI chấm gì" — rubric minh bạch.** Trước phiên hiển thị các năng lực sẽ được chấm; sau phiên, mỗi điểm gắn với tiêu chí rubric đã đạt và chưa đạt. | 39% ứng viên muốn biết AI đang đo gì; 44% muốn được báo trước. [Khảo sát — greenhouse.com 2026] | Mỗi điểm trên báo cáo liên kết được tới đúng phiên bản rubric đã chốt cho câu đó. | Không đổi mô hình chấm. Chi phí nằm ở giao diện. |
| F-05 | **Phong cách người phỏng vấn và phỏng vấn tiếng Anh.** Chọn phong cách (thân thiện / áp lực / kỹ thuật sâu) và ngôn ngữ (vi-VN / en-US) khi tạo phiên. | Ứng viên Việt ứng tuyển công ty nước ngoài phải phỏng vấn bằng tiếng Anh. [Giả định] | Phong cách chỉ đổi câu chữ và nhịp hỏi; không đổi bộ câu hỏi đã chốt, rubric hay máy trạng thái. | Cần ngân hàng câu hỏi tiếng Anh. Hoãn kênh video cho tiếng Anh. |
| F-06 | **Kiểm tra trước khi vào phòng.** Trước khi ứng viên bấm bắt đầu, hệ thống chạy thử lập kế hoạch và chốt câu hỏi, báo trước nếu JD thiếu câu hỏi, thay vì để ứng viên vào một phiên hỏng. | Người dùng Huru báo công cụ không hỏi câu nào sau khi nạp JD. [Khảo sát — finalroundai.com/blog/huru-review-pros-cons] | Không phiên nào được mở mà chưa chốt xong câu hỏi. Lỗi kỹ thuật đóng phiên với TECHNICAL_FAILURE, không quy cho ứng viên. | Dùng lại báo cáo độ phủ ngân hàng câu hỏi (`planning/question_coverage.py`). Thêm khoảng 1–2 giây trước khi vào phòng. |
| F-07 | **Khởi động 3 phút.** Phiên rất ngắn, 2–3 câu, không chấm điểm số, chỉ trả một nhận xét, để giảm lo âu trước buổi phỏng vấn thật. | Người tìm việc dành gần 5 giờ lo lắng, 3 giờ chuẩn bị. [Khảo sát — adobe.com] | Phiên khởi động không ghi điểm vào lịch sử tiến bộ và không sinh báo cáo đầy đủ. | Dùng lại chế độ demo 2–3 phút. [Code — `core/demo_mode.py`] Không làm bản giọng nói ở giai đoạn đầu. |

### 7.4 Luật trích dẫn bằng chứng cho R-11

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
| GC-REP-01 | Ứng viên gọi `GET /sessions/{id}/evaluation`. | Không có trường `decision_recommendation` / `decisionRecommendation` và không có chuỗi PASS, REJECT, STRONG_PASS, CONSIDER (R-15). **Hiện trượt.** |

---

## 10. Chỉ số thành công

Trọng số dưới đây là đề xuất của nhóm cho bộ ca kiểm thử nội bộ, chưa được hội đồng chốt. [Đề xuất · Hợp đồng §5]

| Chỉ số | Trọng số |
|---|---|
| Ca kết thúc ở đúng trạng thái phiên và có bản ghi đánh giá hợp lệ | 35% |
| Số severe failure (SF-01 đến SF-06), cộng tỉ lệ phục hồi sau một lần hiểu nhầm | 25% |
| p95 độ trễ mỗi lượt và chi phí mô hình mỗi phiên | 15% |
| Chất lượng test, vận hành và tài liệu bàn giao | 25% |

**Cổng, không phải trọng số.** Một buổi phỏng vấn giọng nói thật chạy trọn vẹn trong buổi bảo vệ. [Hợp đồng §5.2]

**Chỉ số kết quả** (tỉ lệ người dùng quay lại luyện lần hai, điểm cải thiện sau F-02, tỉ lệ phiên hoàn thành): *Không quy định*. Chưa có người dùng thật để có số nền.

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
| RK-1 | **Trích dẫn chưa được kiểm.** `evidence_quotes` hiện lấy nguyên từ đầu ra mô hình (`evaluation_engine.py`) mà không đối chiếu với câu trả lời. Một trích dẫn bịa là SF-01. Giảm rủi ro bằng luật ở mục 7.4. |
| RK-2 | **Nhãn tuyển/loại đang được trả cho ứng viên.** `get_session_evaluation` (`evaluation/evaluation_service.py`) trả `decision_recommendation` và `decisionRecommendation` (STRONG_PASS, PASS, CONSIDER, REJECT) qua `GET /sessions/{id}/evaluation` và `/report`, là endpoint của chính ứng viên. Đây là SF-04 đang xảy ra. Cần bỏ trường này khỏi API phía ứng viên hoặc đổi thành mức sẵn sàng ("cần luyện thêm / khá / sẵn sàng"). |
| RK-3 | **Chưa có bộ lọc câu hỏi phân biệt đối xử.** Câu hỏi do AI sinh và câu PROBE chưa qua bộ lọc thuộc tính được bảo vệ. |

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

*INTV-PRD-001 · v1.0 · INTERVIA*
