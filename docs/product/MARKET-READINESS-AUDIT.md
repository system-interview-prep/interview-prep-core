# INTERVIA — Đánh giá mức sẵn sàng ra thị trường

**09/10/2026 · Đánh giá tại một thời điểm, không phải yêu cầu sản phẩm**

| Hạng mục | Nội dung |
|---|---|
| Phạm vi rà soát | Backend `interview-prep-core` nhánh `feat/interview-langgraph`; frontend `interview-prep-frontend`; PRD INTV-PRD-001 v1.0; Product Contract INTV-PC-001 Rev 1.0 |
| Cách rà soát | Đọc mã nguồn, không chạy hệ thống. Các phát hiện quan trọng được kiểm lại trực tiếp bằng grep và ghi đường dẫn tại chỗ. |
| Kết luận | **Chưa sẵn sàng ra thị trường.** Đủ cho demo khóa luận sau khi xử lý 4 điểm chặn demo (B-1, B-2, B-4, B-6); hai điểm chặn ra mắt (B-3, B-5) phải xử lý trước khi thu tiền hay mở cho người dùng thật. |

---

## 1. Kết luận ngắn

INTERVIA có nền kỹ thuật mà một cuộc chat với ChatGPT không có: CV được bóc tách có bằng chứng, đối chiếu CV–JD theo từng yêu cầu, ngân hàng câu hỏi có phiên bản và rubric, máy trạng thái phỏng vấn, và bản ghi chấm điểm lưu trong cơ sở dữ liệu. Nhưng phần lớn lợi thế này **chưa đi tới người dùng**:

- Rubric đã chốt chỉ được gửi cho mô hình chấm dưới dạng tên và mô tả; các mốc 0–3, trọng số và ý bắt buộc bị bỏ qua.
- Điểm năng lực được lưu theo nhãn chữ tự do, không theo `concept_id`, nên không so được giữa các buổi.
- Không có bất kỳ tính năng theo dõi tiến bộ nào, dù trang giá đang bán "Readiness Score & Progress".
- Giai đoạn VALIDATE chỉ là một câu hỏi mẫu về dự án trong CV, không kiểm tra tính trung thực.

Vì vậy, ở trạng thái hiện tại, một người dùng dán CV và JD vào ChatGPT nhận được trải nghiệm không kém INTERVIA bao nhiêu. Khác biệt chỉ thành hiện thực khi các tài sản trên được nối vào trải nghiệm, đặc biệt là đo tiến bộ có giá trị đo lường (mục 4).

---

## 2. Điểm chặn

B-1, B-2, B-4, B-6 chặn **demo** (kể cả buổi bảo vệ). B-3, B-5 chặn **ra mắt** (thu tiền hoặc mở cho người dùng thật).

| # | Vấn đề | Bằng chứng | Hậu quả |
|---|---|---|---|
| B-1 | **Endpoint đối chiếu không có xác thực.** `/api/v1/matching/match`, `/match-ids`, `/ai/score-cv-jp` và `/clarifications*` không có `Depends(current_user)`. `resolve_resume` đọc `user_cvs` theo id mà không lọc `user_id`. | `src/modules/matching/api/router.py`; `src/modules/matching/application/input_resolver.py:25` | Ai biết id CV đều đọc được dữ liệu CV của người khác (IDOR), và có thể đốt chi phí mô hình không giới hạn. Vi phạm SF-03. |
| B-2 | **Ứng viên thấy nhãn tuyển/loại.** API trả `decisionRecommendation`; trang kết quả mở tab "nhà tuyển dụng" làm mặc định, hiện REJECT và red flags. | `evaluation/evaluation_service.py`; frontend `InterviewReportView.tsx` | Vi phạm SF-04 ở cả API lẫn giao diện. |
| B-3 | **Thanh toán giả.** Modal VietQR chờ timeout rồi báo "demo-complete". Credit tạo khi đăng ký nhưng không bao giờ bị trừ hay kiểm. | frontend `VietQrModal.tsx:88-92`; backend không có chỗ nào trừ `user_credits` | Không thể bán; nếu mở công khai là thu tiền không giao dịch thật hoặc cho dùng miễn phí không giới hạn. |
| B-4 | **Tuyên bố marketing không có căn cứ.** "98% Accuracy", "Sentiment Analysis Engine", lời chứng thực của nhân vật hư cấu. | frontend `i18n.ts:1368-1369`, `:3506-3514` | Quảng cáo sai sự thật. "Sentiment analysis" còn mâu thuẫn §1.2.3 của contract. |
| B-5 | **Không có đồng ý xử lý dữ liệu và không xóa được tài khoản.** Xóa CV không xóa transcript và snapshot chứa nội dung CV. | backend: không có endpoint xóa tài khoản; không có logic retention | Luật Bảo vệ dữ liệu cá nhân 2025 yêu cầu đồng ý rõ ràng và quyền xóa. |
| B-6 | **Xóa CV làm mất lịch sử phỏng vấn.** FK `resume_id` là `ON DELETE SET NULL`, trong khi danh sách phiên và `_owned_session` lọc `resume_id IS NOT NULL`. | `migrations/versions/20260925_0013_...py:30`; `api/router.py:182, 332` | Người dùng xóa CV cũ thì mất toàn bộ báo cáo cũ. Mọi tính năng theo dõi tiến bộ đều hỏng từ gốc. |

---

## 3. Bảng điểm theo tiêu chí

Đạt / Một phần / Chưa.

| Nhóm | Tiêu chí | Mức | Ghi chú |
|---|---|---|---|
| Giá trị cốt lõi | Câu hỏi bám CV và JD | Một phần | Có kế hoạch theo năng lực và câu về dự án trong CV; ngân hàng 130 câu seed dùng chung một rubric chung chung. |
| | Chấm theo rubric có căn cứ | Chưa | Mốc 0–3, trọng số, ý bắt buộc không được dùng khi chấm. `evaluation_service.py:199-213` |
| | Trích dẫn bằng chứng đúng nguyên văn | Chưa | Không kiểm. SF-01. |
| | Theo dõi tiến bộ theo thời gian | Chưa | Không có endpoint, không có màn hình. Bảng `scoring_history` có nhưng không ai dùng. |
| | Câu trả lời mẫu | Chưa | `what_good_looks_like` được sinh rồi bỏ, không có cột lưu. |
| Độ tin cậy | Điểm số lặp lại được | Chưa | Không có bộ đo độ lệch giữa các lần chấm. Nghiên cứu cho thấy LLM-chấm cho điểm khác nhau với cùng một câu trả lời (xem nguồn). |
| | Không mở phiên rỗng | Đạt | 409 `question_bank_insufficient`, sinh câu bù, báo cáo độ phủ. |
| | Không ghi "hoàn thành" sai | Đạt | `/chat/complete` kiểm độ phủ agenda. |
| | CI | Chưa | Hai workflow chỉ chạy trên nhánh khác, không chạy trên nhánh hiện tại hay `main`. |
| | Giám sát lỗi | Chưa | Chỉ có trace JSONL ở môi trường dev. Không Sentry, không metrics. |
| Bảo mật & pháp lý | Xác thực, phân quyền | Chưa | B-1. |
| | Đồng ý, xóa dữ liệu, thời hạn lưu | Chưa | B-5. |
| | Giới hạn tần suất | Một phần | Chỉ giới hạn đăng nhập sai, trong bộ nhớ tiến trình. |
| Kinh doanh | Thanh toán, hạn mức | Chưa | B-3. |
| | Kiểm soát chi phí mô hình | Chưa | Không đếm token; `ai_max_input_chars` không được áp. |
| | Marketing trung thực | Chưa | B-4. |
| Trải nghiệm | Thời gian tới giá trị đầu tiên | Một phần | Phải chờ bóc tách CV xong; chỉ chọn được JD do quản trị viên đăng, không dán JD của mình. |
| | Bảng điều khiển | Chưa | "Readiness" và "Practice time" luôn hiện "—". Không có trang lịch sử. |
| | Song ngữ | Một phần | Có từ điển vi/en nhưng trang kết quả và prompt chấm chỉ tiếng Việt. |
| | Di động, trợ năng | Một phần | Có layout responsive và aria; biểu đồ radar không có mô tả văn bản. |
| Kiểm thử | Test tự động | Một phần | Khoảng 775 hàm test, tập trung ở interviews, JD, matching. Ngân hàng câu hỏi 15, auth 11. |
| | Đo chất lượng | Một phần | Có eval cho CV, JD, matching; điểm 1.0 là ngưỡng hồi quy trên dữ liệu chuẩn, không phải độ chính xác thực tế. JD hybrid trên văn bản thật khoảng 0.53. |

---

## 4. So với "dán CV và JD vào ChatGPT"

ChatGPT làm được nhiều hơn người ta nghĩ: nó đọc CV và JD được dán vào, hỏi từng câu, hỏi đào sâu, chạy bằng giọng nói, và chấm điểm khi được yêu cầu. Vì vậy những thứ dưới đây **không** phải khác biệt:

- Câu hỏi cá nhân hóa theo CV và JD.
- Đặt thời lượng, số câu, phong cách người phỏng vấn.
- Phỏng vấn bằng giọng nói.
- Nhận xét và câu trả lời mẫu.
- Bảng điều khiển tiến bộ nói chung. Exponent ("My Progress"), Yoodli (bản doanh nghiệp) và Interview Prep Academy ("Readiness Dashboard") đều đã có.

Khác biệt chỉ đứng được ở những chỗ một cuộc chat không trạng thái **về cấu trúc không làm được**:

| Năng lực | Chat chung | INTERVIA cần đạt | Hiện tại |
|---|---|---|---|
| Điểm lặp lại được | Mỗi cuộc chat chấm khác nhau, không có rubric cố định, không đo được sai số | Cùng câu trả lời, cùng rubric phiên bản → điểm nằm trong biên sai số đã đo | Chưa |
| Tiến bộ đo được | Chỉ có trí nhớ dạng văn bản tự do; không có chuỗi điểm theo kỹ năng | Sổ kỹ năng theo `concept_id`, khoảng tin cậy, chỉ báo "tiến bộ thật" vượt ngưỡng nhiễu | Chưa |
| Câu hỏi và rubric do người duyệt | Mô hình tự nghĩ câu hỏi mỗi lần | Câu đã duyệt, người duyệt khác người soạn, có câu neo lặp lại để so sánh | Một phần |
| Bảo đảm cứng | Chỉ dựa vào lời dặn trong prompt | Không bịa trích dẫn, không hỏi câu phân biệt đối xử, không ghi hoàn thành sai — kiểm bằng code | Một phần |
| Vòng khép kín | Người dùng tự nhớ phải luyện gì | Khoảng trống JD → bài luyện → đo lại → cập nhật sổ kỹ năng | Chưa |
| Phòng thủ CV | Không biết CV có những tuyên bố nào cần bảo vệ | Mỗi dự án, con số, công nghệ trong CV là một mục cần bảo vệ; theo dõi mục nào đã được trả lời vững | Chưa (VALIDATE hiện chỉ là một câu mẫu) |

**Luận điểm sáng tạo đề xuất:** *"Tiến bộ đo được, không phải cảm giác."* Nếu làm xong P-01 đến P-04, INTERVIA sẽ công bố sai số chấm của chính mình và chỉ báo tiến bộ khi thay đổi vượt sai số đó. Trong phạm vi khảo sát ngày 09/10/2026 (không tìm riêng sản phẩm công bố sai số), chưa thấy công cụ luyện phỏng vấn nào làm vậy.

**Giới hạn của luận điểm.** Sai số đo bằng cách chấm lại cùng câu trả lời chỉ là cận dưới; giữa hai buổi còn có độ khó câu hỏi và phong độ. Vì vậy chỉ kết luận trên cặp câu neo hoặc trung bình nhiều lần đo. Nếu sai số thật lớn (khoảng 1 điểm trên 10), phần lớn thay đổi sẽ nằm trong biên nhiễu và người dùng có thể thấy đường phẳng; cần thêm tín hiệu tiến bộ không dựa vào điểm (PRD RK-15). Chi tiết ở PRD v1.1, mục 7.5 và 7.6.

---

## 5. Thứ tự xử lý đề xuất

1. **Tuần 1 — điểm chặn:** B-1 (thêm xác thực và lọc `user_id`), B-2 (bỏ trường quyết định khỏi API ứng viên, đổi tab mặc định), B-4 (gỡ tuyên bố không căn cứ), B-6 (bỏ lọc `resume_id IS NOT NULL`, giữ lịch sử).
2. **Tuần 2 — nền đo lường:** chấm theo mốc rubric; lưu điểm theo `concept_id`; kiểm trích dẫn; dựng bộ đo sai số chấm.
3. **Tuần 3–4 — trải nghiệm tiến bộ:** sổ kỹ năng, dòng thời gian, so sánh trước/sau, mức sẵn sàng theo JD.
4. **Trước khi thu tiền:** B-3 thanh toán thật và trừ hạn mức; B-5 đồng ý, xóa tài khoản, thời hạn lưu; CI trên `main`; giám sát lỗi; đếm chi phí.

---

## Nguồn

- So sánh ChatGPT cho luyện phỏng vấn: https://mockif.com/chatgpt-mock-interview · https://www.makeuseof.com/how-use-chatgpt-voice-mode-interview-preparation/ · https://mocky.pro/en/blog/chatgpt-vs-gemini-interview-prep
- Bộ nhớ của ChatGPT: https://openai.com/blog/memory-and-new-controls-for-chatgpt
- Bảng điều khiển tiến bộ của đối thủ: https://www.tryexponent.com/blog/new-my-progress-dashboard · https://yoodli.ai/blog/introducing-analytics-centralized-insights-within-yoodli · https://www.producthunt.com/posts/1239902
- Độ không ổn định của LLM khi chấm điểm: https://arxiv.org/abs/2506.22316 · https://www.alphaxiv.org/abs/2603.04417 · https://arxiv.org/abs/2509.21117v2
- Luyện tập có chủ đích và giới hạn bằng chứng: https://notes.andymatuschak.org/zAEV61QhJaiWLYQ8vZF36uT · https://www.iatrox.com/blog/unlimited-ai-osce-practice-deliberate-practice-or-rehearsing-mistakes
