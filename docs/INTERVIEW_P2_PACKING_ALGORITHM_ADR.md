# ARCHITECTURE DECISION RECORD (ADR)
## P2 Dynamic Question Packing Algorithm & Feasibility Architecture

- **Mã định danh**: `ADR-INTERVIEW-P2-PACKING-001`
- **Dự án**: INTERVIA — Interview Prep Core Engine
- **Repository**: `interview-prep-core`
- **Nhánh**: `feat/interview-text-runtime`
- **Ngày lập**: 30/09/2026
- **Tác giả**: Lead Architect
- **Trạng thái**: **PROPOSED / READY FOR REVIEW (Technical Review Completed — Pending Formal Lead Architect Sign-off)**
- **Ghi chú Thẩm định**: Vòng rà soát kỹ thuật của Lead Architect đã hoàn tất, các trade-offs và quyết định kỹ thuật đã chốt (Phương án B Two-Phase Subset Search, Thứ tự turns nội bộ theo `_candidate_rank`, Kiến trúc Atomic Preflight, Benchmark Protocol tái lập). Trạng thái chính thức giữ `PROPOSED` chờ xác nhận ký duyệt chính thức (formal sign-off) từ Lead Architect, không tự tạo chữ ký hoặc ngày duyệt giả định.
- **Liên kết ngữ cảnh**:
  - [`docs/INTERVIEW_P1_P2_CONTRACT_AUDIT.md`](INTERVIEW_P1_P2_CONTRACT_AUDIT.md)
  - [`docs/INTERVIEW_DYNAMIC_QUESTION_BUDGET_PROPOSAL.md`](INTERVIEW_DYNAMIC_QUESTION_BUDGET_PROPOSAL.md)
  - [`docs/INTERVIEW_P1_GATE2_SPEC_AND_ACCEPTANCE_CASES.md`](INTERVIEW_P1_GATE2_SPEC_AND_ACCEPTANCE_CASES.md)

---

## 1. BỐI CẢNH VÀ VẤN ĐỀ KIẾN TRÚC (CONTEXT & PROBLEM STATEMENT)

### 1.1. Hiện trạng sau Gate 2
Module P1 Planner (`src/modules/interviews/planner.py`) đã hoàn thành Gate 2 với khả năng sinh kế hoạch động (`interview-planner-v2-dynamic`), tính toán ngân sách kỹ thuật (`techPoolSeconds`), phân bổ phong bì thời gian nguyên giây (`timeEnvelopeSeconds`), sàn thời lượng (`floorSeconds` $\ge 180$s cho `TEXT`, $\ge 360$s cho `CODING`), và loại bỏ target không áp dụng (`nonInterviewedTargets`).

Tuy nhiên, module P2 Question Selector (`src/modules/interviews/question_selector.py`) hiện tại vẫn chạy logic tĩnh:
1. Ép cứng tối thiểu 2 câu hỏi/target (`needed = max(targetQuestionCount, 2)` tại line 494).
2. Tự ý tráo câu hỏi coding vào target `TEXT` (lines 498–501).
3. Duyệt Greedy tuần tự theo điểm relevance đơn lẻ, dẫn tới hiện tượng **Greedy Suboptimality / Floor Feasibility Failure**.

### 1.2. Thất bại Khả thi của Thuật toán Greedy (Floor Feasibility Failure)
Xét kịch bản fixture chuẩn:
- Một target có ngân sách phong bì `timeEnvelopeSeconds = 360`s và sàn thời lượng `floorSeconds = 360`s.
- Candidate pool trong Question Bank có 3 câu:
  - Câu $A$: chi phí $300$s, độ liên quan (relevance) $= 0.95$ (xếp hạng 1).
  - Câu $B$: chi phí $180$s, độ liên quan (relevance) $= 0.85$ (xếp hạng 2).
  - Câu $C$: chi phí $180$s, độ liên quan (relevance) $= 0.80$ (xếp hạng 3).
- **Hành vi Greedy tuần tự**: Chọn câu $A$ ($300$s). Ngân sách còn lại $360 - 300 = 60$s. Duyệt tiếp $B$ ($180$s $> 60$s) $\to$ bỏ qua; duyệt tiếp $C$ ($180$s $> 60$s) $\to$ bỏ qua. Thuật toán kết thúc với $\{A\}$, tổng thời lượng $300$s $< 360$s sàn. Hệ thống báo thiếu hụt hoặc vi phạm mức sàn, **mặc dù tồn tại tập hợp lệ $\{B, C\}$ vừa khít ngân sách ($180 + 180 = 360$s) và đạt sàn 100%**.
- Một bước pre-check khả thi đơn lẻ (single feasibility pre-check) trước vòng lặp Greedy không giải quyết được vấn đề này nếu việc kết nạp câu $A$ sau đó vẫn triệt tiêu khả năng chọn $\{B, C\}$.

Do đó, P2 bắt buộc phải sở hữu một thuật toán đóng gói bảo toàn khả năng đạt sàn (Feasibility-Preserving Algorithm).

---

## 2. CÁC QUYẾT ĐỊNH PRODUCT OWNER ĐÃ DUYỆT (PRODUCT CONSTRAINTS)

Các nguyên tắc sau đã được Product Owner phê duyệt chính thức (28/09/2026), có tính chất ràng buộc tiên quyết và không thay đổi trong ADR này:

1. **Quyết định 2 (Question Bank Deficit) — 2A Fail-Closed**:
   - P2 Dynamic chỉ sử dụng câu hỏi đạt chuẩn phê duyệt/calibration (`qv.status IN ('APPROVED', 'CALIBRATED')` theo hằng số `_ELIGIBLE_STATUSES` tại `src/modules/interviews/question_selector.py:23, 144`), có `q.retired_at IS NULL`, phiên bản trỏ đúng `current_approved_version_id`, và có rubric chấm điểm hợp lệ qua bảng `question_version_rubrics`.
   - Nếu bất kỳ target bắt buộc nào không có tập câu thỏa mãn đồng thời mức sàn (`floorSeconds`), đúng archetype (`targetArchetype`) và trần (`timeEnvelopeSeconds`), hệ thống **tuyệt đối không tạo queue một phần và không mở phiên**.
   - Trả lỗi HTTP **409 Conflict** kèm error code ổn định `question_bank_insufficient`. Payload chỉ chứa thông tin target/concept bị thiếu ở dạng an toàn, không rò rỉ barem chấm điểm hay câu hỏi nội bộ.
   - Nhánh dynamic không tự ý loại bỏ target, không tái phân bổ ngân sách, không kích hoạt `_fallback_snapshot`, không gọi LLM sinh câu tự động, và không chỉnh sửa CSDL Question Bank.
   - **Nhánh legacy (`interview-planner-v1`) cũng fail-closed.** Quyết định bổ sung, Product Owner duyệt khi rà soát lại toàn bộ luồng interview: fail-closed áp dụng cho **cả hai** nhánh. `_fallback_snapshot` đã bị **xoá khỏi mã nguồn**, không còn đường sinh câu hỏi tạm ở bất kỳ nhánh nào.
     - *Lý do*: snapshot fallback sinh turn có `questionVersionId = None` **và `rubric = None`. Evaluation lấy barem từ `question_snapshot.rubric.criteria`, nên turn fallback bị chấm hoàn toàn không có barem — đúng rủi ro "điểm số không tin cậy" mà phương án 2B đã bị loại vì nó. Turn fallback cũng phá hợp đồng "freeze exact `questionVersionId`", khiến phiên không replay/audit được.
     - *Bù lại rủi ro từ chối phục vụ*: seed Question Bank đã mở rộng lên 25 concept / 93 câu và bổ sung mapping `TARGET_ROLE` cho toàn bộ career code; đo trên 103 file JD golden hiện **không còn concept nào bị hở**. Một test dev-gate chặn hồi quy độ phủ này.
     - *Dữ liệu lịch sử*: phiên đã freeze trước quyết định này vẫn có thể chứa turn `deterministic_fallback_unreviewed`; đường đọc lại plan `LOCKED` vẫn đếm và báo cáo chúng.
2. **Quyết định 4 (Time Envelope Ceiling) — Lựa chọn A: Hard Ceiling**:
   - Với mọi competency target, tổng `estimated_cost` của các câu được chọn bắt buộc:
     $$\sum_{q \in \text{selected}} \text{estimated\_cost}(q) \le \text{timeEnvelopeSeconds}$$
   - Không có dung sai phần trăm (+10–15%) và không có phụ trội giây/câu (+30s/câu).
3. **Phạm vi Triển khai Gate 3 — Phạm vi A: P2 Module Testing**:
   - Kiểm thử P2 độc lập thông qua test fixtures kế hoạch v2 (`tests/modules/interviews/test_question_selector.py`).
   - Chưa sửa caller thật hoặc persistence bridge trong `planner.py`. Chưa kiểm thử end-to-end P1–DB–P2 trong Gate 3.
4. **Ràng buộc Production**:
   - Không bật dynamic planner trên production. Toàn bộ session production tiếp tục dùng `policy_config=None` (`interview-planner-v1`).

---

## 3. ĐÁNH GIÁ VÀ LỰA CHỌN THUẬT TOÁN ĐÓNG GÓI (ALGORITHM SELECTION)

Lead Architect đã phân tích đối chiếu hai phương án kỹ thuật:

### 3.1. Phương án A: Feasibility-Preserving Lookahead Selection
- **Cơ chế**: Sắp xếp candidates theo thứ tự ưu tiên câu hỏi đơn lẻ. Khi duyệt đến ứng viên $q$, chỉ thêm $q$ nếu bài toán con còn lại (ngân sách còn lại $\text{budget} - \text{cost}(q)$, sàn còn lại $\max(0, \text{floor} - \text{cost}(q))$, tập candidate còn lại) vẫn có ít nhất một nghiệm hợp lệ.
- **Ưu điểm**: Tiến trình duyệt có vẻ trực quan theo thứ tự ưu tiên câu hỏi.
- **Nhược điểm nghiêm trọng**:
  1. *Tính không tối ưu toàn cục (Local Myopia)*: Lookahead chỉ kiểm tra tính "khả thi còn lại" (feasibility), chứ không bảo đảm tổ hợp tạo thành sau đó là tập hợp có chất lượng cao nhất. Việc cam kết chọn $q$ sớm có thể ép thuật toán phải chọn các câu cực kỳ kém ở các bước sau để vừa khít ngân sách.
  2. *Bản chất đệ quy ẩn*: Để kiểm tra feasibility tại mỗi bước kết nạp, bản thân bước lookahead phải chạy một bài toán Subset Sum / 0-1 Knapsack con. Nếu thực hiện lookahead có quay lui (backtracking), nó thực chất biến thành Two-Phase Search nhưng với mã nguồn phức tạp và khó kiểm soát trạng thái hơn.

### 3.2. Phương án B: Two-Phase Subset Search (LỰA CHỌN CỦA LEAD ARCHITECT)
- **Cơ chế**:
  - **Pha 1 (Lọc Không gian Khả thi — Feasibility Space Filtering)**:
    Tìm tất cả các tập con $S \subseteq \text{Candidates}$ thỏa mãn đồng thời:
    1. $\sum_{q \in S} \text{estimated\_cost}(q) \le \text{timeEnvelopeSeconds}$ (Hard Ceiling).
    2. $\sum_{q \in S} \text{estimated\_cost}(q) \ge \text{floorSeconds}$ (Floor Satisfaction).
    3. Mọi câu trong $S$ đều đúng `targetArchetype` và đạt điều kiện phê duyệt/calibration (`qv.status IN ('APPROVED', 'CALIBRATED')`, `q.retired_at IS NULL`, trỏ đúng `current_approved_version_id`, có rubric hợp lệ qua bảng `question_version_rubrics`).
    4. Kích thước tập hợp $|S| \ge 1$.
  - **Pha 2 (Định giá & Chọn Lọc Tối ưu — Subset Ranking & Selection)**:
    Nếu không có tập nào vượt qua Pha 1 $\implies$ Ngắt ngay lập tức, trả lỗi Q2 Fail-Closed (HTTP 409).
    Nếu tồn tại $\ge 1$ tập khả thi $\implies$ Áp dụng Hàm mục tiêu cấp tập hợp (Subset Objective Function) và Cơ chế Tie-Break xác định để chọn tập tối ưu duy nhất $S^*$.
- **Ưu điểm kiến trúc vượt trội**:
  1. **Bảo toàn Khả thi 100% (Mathematical Invariant)**: Nếu trong kho tồn tại bất kỳ tập câu nào thỏa mãn sàn và trần, thuật toán chắc chắn 100% tìm thấy.
  2. **Tách biệt hoàn toàn (Decoupling)**: Phân tách rạch ròi giữa bài toán Ràng buộc (Constraint Satisfaction — Sàn, Trần, Archetype) và bài toán Tối ưu hóa (Optimization — Relevance, Difficulty, Purpose).
  3. **Tối ưu toàn cục**: Pha 2 luôn so sánh trên toàn bộ các phương án khả thi, không bị rơi vào bẫy tối ưu cục bộ của thuật toán tham lam.

> [!IMPORTANT]
> **ĐỀ XUẤT CỦA LEAD ARCHITECT (PROPOSED — CHỜ PHÊ DUYỆT CHÍNH THỨC)**:  
> Đề xuất chọn **Phương án B: Two-Phase Subset Search kèm kỹ thuật tỉa nhánh Branch-and-Bound** (Trạng thái kỹ thuật: Đang chờ Lead Architect phê duyệt chính thức).

---

## 4. HÀM MỤC TIÊU VÀ CHUYỂN ĐỔI RANKING TUPLE SANG CẤP TẬP HỢP

### 4.1. Nguyên tắc Dữ liệu Hiện có (No Phantom Fields)
Tuyệt đối không đưa vào các trường giả định như `quality_score`, `pedagogical_value`, hay `discrimination_index`.  
Mã nguồn hiện tại (`src/modules/interviews/question_selector.py:87–114`) sở hữu tuple xếp hạng câu hỏi đơn lẻ:
$$r(q) = \big( \text{locale\_rank}(q),\ \text{difficulty\_distance}(q),\ \text{purpose\_rank}(q),\ -\text{relevance}(q),\ \text{tiebreaker}(q),\ \text{version}(q),\ \text{qvid}(q) \big)$$

### 4.2. Định nghĩa Hàm Mục tiêu Xếp hạng Tập Hợp (Subset Scoring Function)
Mỗi tập hợp khả thi $S = \{q_1, q_2, \dots, q_k\}$ được đánh giá bằng một vector tiêu chí tổng hợp đa mục tiêu $R(S)$, sắp xếp theo thứ tự ưu tiên từ điển (Lexicographical Vector Comparison — giá trị càng nhỏ càng ưu tiên):

$$R(S) = \Big( \text{MaxLocaleRank}(S),\ \text{AvgDiffDistance}(S),\ \text{AvgPurposeRank}(S),\ -\text{TotalRelevance}(S),\ \text{DurationEfficiency}(S),\ \text{SubsetTiebreaker}(S) \Big)$$

Trong đó các thành phần được định nghĩa chính xác như sau:

1. **`MaxLocaleRank(S)`** $= \max_{q \in S} \big( \text{\_locale\_rank}(q.\text{canonical\_locale}, \text{locale}) \big)$:
   - Ưu tiên tập hợp có 100% câu hỏi khớp tuyệt đối locale (giá trị 0). Bất kỳ tập nào chứa câu khác ngôn ngữ (rank 10) sẽ bị đẩy xuống sau.
2. **`AvgDiffDistance(S)`** $= \frac{1}{|S|} \sum_{q \in S} \text{\_difficulty\_distance}(q.\text{difficulty\_band}, \text{difficulty})$:
   - Khoảng cách độ khó trung bình so với target level. Dùng trung bình cộng để không phạt tập có nhiều câu hỏi khi các câu đều khớp độ khó.
3. **`AvgPurposeRank(S)`** $= \frac{1}{|S|} \sum_{q \in S} \text{purpose\_rank}(q)$:
   - Ưu tiên tập gồm các câu hỏi có mục đích ánh xạ cốt lõi (`PRIMARY_COMPETENCY` = 0) trước các câu bổ trợ (`TARGET_SKILL` = 1, `TARGET_ROLE` = 2).
4. **`-TotalRelevance(S)`** $= -\sum_{q \in S} q.\text{relevance}$:
   - Tối đa hóa tổng độ liên quan chuyên môn của các câu hỏi được chọn đối với competency target.
5. **`DurationEfficiency(S)`** $= \text{timeEnvelopeSeconds} - \sum_{q \in S} \text{estimated\_cost}(q)$:
   - Giữa các tập có chất lượng chuyên môn tương đương, ưu tiên tập tận dụng ngân sách phong bì tốt hơn (phần dư thừa ít hơn).
6. **`SubsetTiebreaker(S)`**:
   - Khóa phá vỡ thế hòa xác định tuyệt đối (xem Mục 5).

---

## 5. CƠ CHẾ TIE-BREAK ỔN ĐỊNH VÀ ĐỘC LẬP THỨ TỰ SQL (DETERMINISTIC TIE-BREAK)

### 5.1. Nguồn Dữ liệu Định Danh và Session Salt
- **Session Salt**: Sử dụng `session_id` của phiên phỏng vấn (`str(session_row["id"])`).
- **Khóa định danh câu hỏi**: Sử dụng `question_version_id` (UUID chuỗi chuẩn hóa) của từng phiên bản câu hỏi trong tập.

### 5.2. Công thức Băm Tập Hợp (Subset Hash Formula)
Để bảo đảm tính xác định tuyệt đối và hoàn toàn không phụ thuộc vào thứ tự trả về bất định của SQL query (`ORDER BY` ngẫu nhiên giữa các row tương đương):

1. Với tập câu hỏi $S$, trích xuất danh sách các `question_version_id` dạng chuỗi:
   $$V(S) = [ \text{str}(q.\text{question\_version\_id}) \quad \forall q \in S ]$$
2. Sắp xếp danh sách $V(S)$ theo thứ tự từ điển tăng dần:
   $$V_{\text{sorted}}(S) = \text{sort}(V(S))$$
3. Tạo chuỗi ký tự chuẩn hóa kết hợp session salt:
   $$\text{canonical\_seed} = \text{salt} + \text{":"} + \text{":".join}(V_{\text{sorted}}(S))$$
4. Tính giá trị băm MD5 32 ký tự hex:
   $$\text{SubsetTiebreaker}(S) = \text{hashlib.md5}(\text{canonical\_seed.encode("utf-8")}).\text{hexdigest()}$$

### 5.3. Định lý Xác định (Determinism Guarantee)
- Cùng một `session_id` và cùng một candidate pool, dù CSDL PostgreSQL trả về thứ tự câu hỏi như thế nào, $V_{\text{sorted}}(S)$ luôn đồng nhất $\implies \text{canonical\_seed}$ luôn đồng nhất $\implies \text{SubsetTiebreaker}(S)$ luôn đồng nhất.
- Đảm bảo tính tái lập 100% trong kiểm thử và vận hành hệ thống.

### 5.4. Thứ Tự Sắp Xếp Câu Hỏi Bên Trong Tập Đã Chọn (Intra-Subset Turn Ordering — QUYẾT ĐỊNH CỦA LEAD ARCHITECT)
- **Phân định rõ ranh giới của SubsetTiebreaker**: `SubsetTiebreaker(S)` chỉ có phạm vi so sánh và phá vỡ thế hòa **giữa các tập hợp ứng viên khác nhau** ($S_1$ vs $S_2$). Cơ chế này **hoàn toàn không xác định thứ tự trình bày các câu hỏi bên trong tập hợp đã chọn** ($q \in S^*$) khi chuyển thành các lượt phỏng vấn (`turn_index = 0, 1, \dots`) trong `interview_turns`.
- **Rủi ro kiến trúc**: Nếu không có quy tắc sắp xếp ổn định và tường minh cho các câu hỏi bên trong tập, thứ tự các câu hỏi khi phỏng vấn sẽ bị phụ thuộc vào thứ tự ngẫu nhiên của database row trong SQL query hoặc thứ tự duyệt set/dict trong Python runtime, làm mất tính xác định (determinism) giữa các lần chạy.

- **Đối chiếu và Đánh giá 3 Phương án Kỹ thuật**:
  1. *Phương án 1 (Theo Ranking Tuple Đơn lẻ $r(q)$ tăng dần)*:
     - **Cơ chế**: Sắp xếp các câu trong $S^*$ theo thứ tự ưu tiên của hàm `_candidate_rank(q, difficulty=target_difficulty, locale=session_locale, salt=session_id)`.
     - **Ưu điểm vượt trội**:
       - *Định hướng giá trị sư phạm và độ phủ bằng chứng (Pedagogical Prioritization)*: Đưa câu hỏi có độ liên quan cao nhất (`relevance` lớn nhất), độ khớp độ khó tốt nhất và đúng vai trò cốt lõi (`PRIMARY_COMPETENCY` > `TARGET_SKILL`) lên các lượt đầu tiên (`turn_index = 0, 1, \dots`). Ưu tiên câu hỏi có giá trị cao hơn ở các lượt đầu để tăng cơ hội thu thập evidence trọng yếu nếu phiên kết thúc sớm; điều này không bảo đảm độ tin cậy của điểm P4.
       - *Tính xác định tuyệt đối (Strict Determinism)*: Tuple $r(q)$ kết thúc bằng `(tiebreaker, version, question_version_id)` trong đó `tiebreaker` dựa trên MD5 salt session, tạo thành một quan hệ thứ tự toàn phần nghiêm ngặt (strict total order), loại trừ 100% sự phụ thuộc vào thứ tự trả về của PostgreSQL hay hash seed ngẫu nhiên của Python runtime.
       - *Tính nhất quán kiến trúc (Architectural Consistency)*: Tái sử dụng trực tiếp hàm `_candidate_rank` hiện hữu tại `src/modules/interviews/question_selector.py:87-114`, không sinh thêm mã nguồn hay logic sắp xếp phân mảnh.
  2. *Phương án 2 (Theo Chi Phí Thời Lượng `estimated_cost`)*:
     - **Cơ chế**: Sắp xếp câu ngắn trước, câu dài sau (hoặc ngược lại).
     - **Đánh giá loại bỏ**: Chi phí thời lượng chỉ là ước lượng tĩnh (selection-time estimate), hoàn toàn không phản ánh tầm quan trọng sư phạm hay thứ tự logic của cuộc phỏng vấn. Câu ngắn có thể là câu mở rộng phụ, câu dài có thể là câu kiến trúc trọng tâm.
  3. *Phương án 3 (Theo Định danh Xác định `version` / `question_version_id`)*:
     - **Cơ chế**: Sắp xếp theo thứ tự từ điển của UUID.
     - **Đánh giá loại bỏ**: Thuần túy mang tính kỹ thuật máy móc, phi ngữ cảnh và làm ngẫu nhiên hóa trải nghiệm phỏng vấn của ứng viên.

- **Quyết định Kỹ thuật Chính thức của Lead Architect**:
  **Chốt Lựa chọn Phương án 1 (Sắp xếp theo `_candidate_rank` tăng dần)**.

- **Quy tắc Gán `turn_index` Cụ thể**:
  Với tập câu hỏi tối ưu $S^*$ đã chọn cho competency target $T$:
  1. Trích xuất danh sách câu hỏi $q \in S^*$.
  2. Sắp xếp danh sách theo thứ tự tăng dần của tuple khóa:
     $$\text{key}(q) = \text{\_candidate\_rank}(q, \text{difficulty}=T.\text{difficulty}, \text{locale}=\text{session.locale}, \text{salt}=\text{session.session\_id})$$
  3. Gán tuần tự chỉ số lượt `turn_index`:
     $$\text{turn\_index}(q_j) = \text{current\_turn\_offset} + j \quad (j = 0, 1, \dots, |S^*| - 1)$$
     trong đó $\text{current\_turn\_offset}$ là vị trí bắt đầu tiếp theo trong phiên phỏng vấn.
  4. Quy tắc này độc lập 100% với thứ tự trả về của SQL query và đảm bảo tính tái lập tuyệt đối.

---

## 6. NGỮ NGHĨA CHI PHÍ ƯỚC TÍNH (ESTIMATED COST SEMANTICS)

### 6.1. Phân biệt Rạch ròi 3 Khái niệm Thời lượng
Dựa trên đối chiếu cấu trúc `InterviewQuestionVersion` (`src/modules/question_bank/models.py:69–71`):

| Khái niệm | Trường dữ liệu Model | Vai trò Kiến trúc |
| :--- | :--- | :--- |
| **Selection Cost (Chi phí Lựa chọn)** | $\text{thinking\_seconds} + \text{soft\_answer\_seconds}$ | **Đơn vị đóng gói tĩnh của P2**. Dùng để kiểm tra điều kiện sàn $\ge \text{floorSeconds}$ và trần $\le \text{timeEnvelopeSeconds}$. |
| **Turn Timeout Guardrail** | $\text{hard\_answer\_seconds}$ | **Trần bảo vệ kỹ thuật tại Runtime**. Dùng để ngắt cưỡng bức lượt trả lời của riêng câu đó nếu ứng viên nói quá thời gian. |
| **Runtime Wall-Clock** | Thời gian thực tế trôi qua | Đo bằng đồng hồ thời gian thực (`monotonic()`), phụ thuộc tương tác thực tế của ứng viên. |

### 6.2. Công thức Chuẩn hóa cho Gate 3
$$\text{estimated\_cost}(q) = q.\text{thinking\_seconds} + q.\text{soft\_answer\_seconds}$$

> [!WARNING]
> **Ràng buộc Biên Kiến trúc về Quỹ Probe**:  
> Tuyệt đối không giả định phần chênh lệch giữa `hard_answer_seconds` và `soft_answer_seconds` sẽ được "hấp thụ bởi `probePoolSeconds`". Quỹ probe pool ($T_{\text{probe\_pool}}$) theo đặc tả gốc chỉ phục vụ các lượt probe đào sâu tại runtime; việc gán thêm trách nhiệm hấp thụ độ trễ câu chính vào probe pool là giả định chưa có căn cứ và có nguy cơ làm cạn kiệt quỹ probe.

---

## 7. KIẾN TRÚC PREFLIGHT CHO CHÍNH SÁCH Q2 FAIL-CLOSED

### 7.1. Nguyên tắc Nguyên tử (Atomic Preflight Principle)
P2 Question Selector tuyệt đối **không được ghi từng phần (partial write)** các lượt câu hỏi vào bảng `interview_turns`, và **không được chuyển trạng thái plan sang `LOCKED`** trước khi tất cả các target bắt buộc được xác nhận khả thi 100%.

### 7.2. Luồng Thực thi Preflight Chi tiết
Thuật toán P2 Dynamic Selector trong hàm `select_and_freeze_questions` được thiết kế theo các bước tuần tự:

```text
BƯỚC 1: Đọc Plan v2 & Danh sách Targets bắt buộc.
BƯỚC 2: Mở DB Transaction.
BƯỚC 3 (VÒNG LẶP PREFLIGHT IN-MEMORY):
  Duyệt từng Target T_i:
    - Truy vấn candidate pool đạt chuẩn (`qv.status IN ('APPROVED', 'CALIBRATED')`, `q.retired_at IS NULL`, đúng `targetArchetype`, có rubric hợp lệ qua `question_version_rubrics`).
    - Chạy Two-Phase Subset Search với branch-and-bound.
    - Nếu KHÔNG tồn tại bất kỳ tập con nào thỏa mãn sàn và trần:
        -> Ngay lập tức hủy transaction (DB Rollback).
        -> Ném QuestionUnavailableError(409 Conflict, code='question_bank_insufficient').
    - Nếu tồn tại tập khả thi:
        -> Chọn tập tối ưu S_i* theo Hàm mục tiêu R(S).
        -> Sắp xếp các câu trong S_i* theo _candidate_rank tăng dần và gán turn_index tuần tự (Mục 5.4).
        -> Lưu S_i* vào danh sách in-memory tạm thời.
BƯỚC 4 (GHI DB NGUYÊN TỬ KHI 100% TARGETS ĐẠT CHUẨN):
  - Xóa turns cũ của session nếu có: DELETE FROM interview_turns WHERE session_id = :sid.
  - Insert toàn bộ các lượt câu hỏi từ danh sách in-memory vào interview_turns.
  - Cập nhật plan status: UPDATE interview_session_plans SET status = 'LOCKED'.
  - Commit DB Transaction.
  - Trả về payload danh sách Frozen Turns thành công.
```

### 7.3. Cấu trúc Payload Phản hồi Lỗi An toàn (Safe Error Payload)
Khi Preflight thất bại tại target $T_k$, ngoại lệ `QuestionUnavailableError` được ném ra và chuyển đổi thành HTTP 409:
```json
{
  "errorCode": "question_bank_insufficient",
  "message": "Question bank cannot satisfy interview plan requirements under fail-closed policy",
  "details": {
    "missingTargets": [
      {
        "conceptId": "kafka-core",
        "label": "Apache Kafka Core",
        "targetArchetype": "TEXT",
        "floorSeconds": 180,
        "timeEnvelopeSeconds": 360,
        "reason": "no_feasible_qualifying_subset"
      }
    ]
  }
}
```
*Lưu ý bảo mật*: Không trả về nội dung câu hỏi bị loại, không trả về barem điểm rubric hay thông tin nội bộ của hệ thống.

---

## 8. MA TRẬN KIỂM THỬ TỐI THIỂU CHO GATE 3 (TEST MATRIX)

Ma trận kiểm thử bắt buộc phải được triển khai trong `tests/modules/interviews/test_question_selector.py` cho Scope A:

| Test ID | Tên Kịch Bản Kiểm Thử | Dữ liệu Đầu Vào & Điều Kiện Biên | Kết Quả Mong Đợi (Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-PACK-01** | Feasibility Floor Preservation (Fixture 300/180/180) | Envelope = 360s, Floor = 360s. Candidate $A$ (300s, rel 0.95), $B$ (180s, rel 0.85), $C$ (180s, rel 0.80). | Thuật toán **bắt buộc chọn $\{B, C\}$** (tổng 360s). Tuyệt đối không chọn $\{A\}$ (300s, rớt sàn). |
| **TC-PACK-02** | Q2 Fail-Closed on Insufficient Floor | Envelope = 360s, Floor = 360s. Chỉ có duy nhất câu $A$ (300s). Không có câu nào khác. | Ném `QuestionUnavailableError`, HTTP 409 `question_bank_insufficient`. 0 turns được ghi. Plan giữ nguyên. |
| **TC-PACK-03** | Archetype Strictness Fail-Closed | Target archetype = `TEXT`. Candidate pool chỉ có các câu archetype `CODING`. | Ném HTTP 409 `question_bank_insufficient`. Tuyệt đối không tự ý lấy câu coding đưa vào target text. |
| **TC-PACK-04** | Hard Ceiling Budget Non-Exceedance | Envelope = 300s, Floor = 180s. Candidates có các câu 350s, 400s. Không câu nào $\le 300$s. | Các câu $> 300$s bị loại bỏ hoàn toàn. Báo 409 do không có tập thỏa mãn trần. |
| **TC-PACK-05** | Atomic Preflight (No Partial Queue) | Plan có 2 targets: Target 1 có đủ câu đạt sàn; Target 2 hoàn toàn thiếu câu. | Ném HTTP 409. Rollback toàn bộ: DB không có bất kỳ dòng `interview_turns` nào của Target 1. |
| **TC-PACK-06** | Deterministic Selection vs. SQL Shuffle | Cùng plan, cùng session salt, nhưng nạp danh sách candidate với thứ tự đảo ngược hoặc xáo trộn ngẫu nhiên. | Kết quả tập câu hỏi đóng băng cuối cùng và **thứ tự `turn_index` của từng câu hỏi bên trong tập đóng băng** phải **giống nhau 100%** (khớp chính xác với thứ tự sắp xếp theo `_candidate_rank` của Phương án 1 đã chốt tại Mục 5.4). |
| **TC-PACK-07** | Single Question Validity | Envelope = 200s, Floor = 180s. Có 1 câu duy nhất 190s thỏa mãn. | Chọn đúng 1 câu duy nhất (190s). Không báo lỗi, không ép nâng lên 2 câu. |
| **TC-PACK-08** | Legacy Branch Invariance | Plan có `policyVersion = 'interview-planner-v1'`. Kho câu hỏi thiếu câu. | Chạy luồng legacy, **fail-closed** với `question_bank_insufficient`, không gọi `_fallback_snapshot`, không ghi partial turns. (Đã cập nhật theo mã nguồn; xem ghi chú lệch ở mục 1.) |

---

## 9. GIỚI HẠN HIỆU NĂNG, RỦI RO TRUNCATION VÀ KẾ HOẠCH BENCHMARK TRONG GATE 3

### 9.1. Phân tích Độ phức tạp Tính toán & Ranh giới Kỹ thuật (Complexity Bounds)
- **Bản chất toán học**: Bài toán tìm tập hợp câu hỏi thỏa mãn sàn và trần thời lượng (floorSeconds $\le \sum \text{cost} \le \text{timeEnvelopeSeconds}$) là bài toán biến thể của **Subset-Sum / 0-1 Knapsack Feasibility Decision**, thuộc lớp bài toán **NP-đầy đủ (NP-complete)**.
- **Độ phức tạp Thời gian (Time Complexity)**:
  - **Theoretical Worst Case**: $\mathcal{O}(2^N)$. Trong trường hợp suy biến bệnh lý (pathological worst-case) khi các ràng buộc sàn và trần không thể cắt tỉa (ví dụ: floor = 0, envelope rất lớn), thuật toán buộc phải duyệt toàn bộ không gian tập con $2^N$. Do đó, **không tuyên bố thuật toán có độ phức tạp thời gian đa thức (polynomial) trong trường hợp xấu nhất lý thuyết, và kỹ thuật branch-and-bound không loại bỏ hoàn toàn nguy cơ bùng nổ tổ hợp trong trường hợp xấu nhất lý thuyết**.
  - **Observed Case (Thực nghiệm với Branch-and-Bound)**: Trong các kịch bản thực tế của Question Bank (thời lượng câu 120s–300s, phong bì 360s, sàn 180s–360s), hai điều kiện cắt tỉa loại bỏ phần lớn không gian tìm kiếm. Với $N=30$, số nút duyệt thực tế tối đa $\le 135$ nút, thời gian thực thi duy trì ở mức $\approx 1.5\text{ ms}$.
- **Độ phức tạp Bộ nhớ (Memory Complexity)**:
  - **Theoretical & Observed Worst Case**: $\mathcal{O}(N)$.
  - Bằng việc theo dõi `best_subset` và `best_objective` trực tiếp trong quá trình duyệt DFS (on-the-fly tracking) thay vì vật chất hóa (materialize) toàn bộ danh sách `feasible_subsets`, độ sâu ngăn xếp đệ quy tối đa là $N$, mảng `current_subset` tối đa $N$ phần tử, `best_subset` tối đa $N$ phần tử, và mảng `suffix_costs` có kích thước $N+1$. Điều này loại bỏ hoàn toàn nguy cơ bùng nổ bộ nhớ $\mathcal{O}(2^N \cdot N)$ (OOM).
- **Cơ chế Cắt tỉa Nhánh (Branch-and-Bound Pruning Criteria)**:
  1. *Cận trên (Hard Ceiling Bound)*: Cắt tỉa ngay nhánh khi $\text{current\_cost} + c\_cost > \text{timeEnvelopeSeconds}$. Vì mọi câu hỏi đều có chi phí dương ($\ge 1$), duyệt sâu hơn chắc chắn vi phạm trần cứng.
  2. *Cận dưới (Suffix-Sum Floor Bound)*: Cắt tỉa ngay nhánh khi $\text{current\_cost} + c\_cost + \text{suffix\_costs}[i+1] < \text{floorSeconds}$. Dù có lấy toàn bộ tất cả các ứng viên còn lại trong kho, tổng thời lượng cũng không thể chạm sàn yêu cầu.

### 9.2. Đánh giá Rủi ro Truncation & Kế hoạch Đo kiểm Thực tế (Benchmark Suite) trong Gate 3
- **Đánh giá Đề xuất Cắt giảm Ứng viên ($N_{\max} = 20$)**:
  - *Phân tích rủi ro nghiêm ngặt*: Việc áp dụng truncation thô bạo (ví dụ: chỉ giữ lại 20 câu có thứ hạng đơn lẻ cao nhất rồi cắt bỏ phần còn lại) **tiềm ẩn nguy cơ trực tiếp phá vỡ tính khả thi đạt sàn (Floor Feasibility Failure)**.
  - *Minh chứng phản ví dụ*: Giả sử 20 câu đầu bảng đều là các câu có thời lượng lớn ($300$s), trong khi câu số 21 và 22 có thời lượng $180$s là hai câu duy nhất kết hợp vừa khít ngân sách $360$s và thỏa mãn sàn $360$s. Nếu tự ý cắt bỏ các câu ngoài top 20, thuật toán sẽ kết luận sai lầm rằng không tồn tại tập khả thi và trả về lỗi 409 Fail-Closed sai lệch!
  - *Nguyên tắc bắt buộc*: **Tuyệt đối không áp dụng bất kỳ kỹ thuật truncation thô bạo nào có nguy cơ loại bỏ nghiệm đạt sàn duy nhất**. Các bài kiểm thử benchmark hiệu năng **không được phép làm suy yếu hoặc vi phạm Acceptance Criteria về tính khả thi đạt sàn (AC-GATE3-08)**.
  - *Xử lý trong trường hợp độ trễ vượt ngưỡng*: Nếu benchmark thực tế cho thấy kích thước candidate pool lớn làm thời gian tìm kiếm vượt ngưỡng cho phép (ví dụ $> 50\text{ms}$):
    1. Ưu tiên tối ưu hóa điều kiện cắt tỉa branch-and-bound chặt chẽ hơn.
    2. Nếu bắt buộc phải có cơ chế giới hạn để bảo vệ hệ thống, cơ chế đó phải được thiết kế có **chứng minh toán học bảo toàn tính khả thi**, hoặc phải **xử lý xác định (deterministic) và fail-closed minh bạch** (báo lỗi cụ thể về giới hạn tính toán, không trả về kết quả sai lệch hoặc kết luận thiếu câu khi chưa duyệt hết). Tuyệt đối không tuyên bố đã bảo toàn khả thi sau khi bỏ bớt candidates mà chưa có chứng minh toán học.

- **Quy Trình và Giao Thức Đo Kiểm Benchmark Tái Lập (Reproducible Benchmark Protocol)**:
  Để kiểm chứng giả thuyết lý thuyết và bảo đảm tính tái lập độc lập 100%, bộ đo kiểm hiệu năng tại Gate 3 Scope A bắt buộc phải tuân thủ nghiêm ngặt giao thức sau:
  1. *Kích thước Candidate Pool*: Đo kiểm lần lượt với 4 mốc:
     - Kịch bản 1: $N = 5$ câu (kích thước cơ sở).
     - Kịch bản 2: $N = 10$ câu (kích thước trung bình thực tế).
     - Kịch bản 3: $N = 20$ câu (kích thước cận trên kỳ vọng).
     - Kịch bản 4: $N = 30$ câu (stress test biên an toàn hệ thống).
  2. *Pha Khởi động (Warm-up Phase)*:
     - Thực hiện tối thiểu $50$ lượt chạy khởi động (warm-up iterations) cho mỗi kích thước $N$ trước khi bấm giờ để ổn định bộ cấp phát bộ nhớ (Python memory allocator), nạp bytecode và CPU instruction cache.
  3. *Số lần chạy lặp lại đo kiểm (Measurement Iterations)*:
     - Thực thi tối thiểu $M = 1.000$ lần chạy độc lập cho mỗi kích thước $N$ với các tập fixture candidate được sinh ngẫu nhiên có kiểm soát thông qua bộ sinh số ngẫu nhiên cố định hạt giống (`fixed random seed`) nhằm bảo đảm mọi lần chạy lại đều tái lập 100% cùng một tập dữ liệu đầu vào.
  4. *Môi trường và Cơ chế đo (Measurement Environment & Instrumentation)*:
     - **Đo in-memory thuần túy**: Sử dụng hàm `time.perf_counter_ns()` để đo riêng thời gian chạy của thuật toán Two-Phase Subset Search in-memory, hoàn toàn cách ly khỏi độ trễ I/O mạng, kết nối DB và câu lệnh SQL.
     - **Ghi nhận cấu hình môi trường**: Báo cáo benchmark bắt buộc đính kèm thông số phần cứng và môi trường: CPU Model, Tần số xung nhịp, Số Cores, Dung lượng RAM, Hệ điều hành (Windows 11 / Linux), Phiên bản Python runtime (`sys.version`, ví dụ Python 3.11.x 64-bit), và chế độ thực thi (Bare-metal hay Docker Container).
  5. *Phương pháp Tổng hợp Số liệu (Metrics Aggregation)*:
     - `mean_latency_ms`: Thời gian thực thi trung bình ($\frac{1}{M} \sum_{i=1}^M t_i$).
     - `p95_latency_ms`: Phân vị thứ 95 của mảng thời gian đo ($95\%$ số lượt chạy hoàn tất trong khoảng thời gian này).
     - `p99_latency_ms`: Phân vị thứ 99 của mảng thời gian đo.
     - `max_latency_ms`: Thời gian chạy lâu nhất ghi nhận được (worst-case latency).
     - `mean_node_count`: Số lượng trạng thái/nút duyệt trung bình trong cây branch-and-bound.
     - `max_node_count`: Số lượng trạng thái/nút duyệt tối đa trong cây.
  6. *Bảng Kết Quả Thực Nghiệm Chuẩn Bị Cho Gate 3 Scope A*:
     *(Tuyệt đối không tự bịa đặt hoặc điền trước các con số kết quả khi chưa chạy benchmark thực tế trên máy chủ; bảng sau đây sẽ được cập nhật chính thức bằng số liệu thực nghiệm sau khi thực thi bộ test benchmark)*:

| Pool Size ($N$) | Mean Latency (ms) | P95 Latency (ms) | P99 Latency (ms) | Max Latency (ms) | Mean Node Count | Max Node Count | Feasibility Guarantee |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **$N = 5$** | 0.1502 | 0.1966 | 0.3986 | 39.7438* | 8 | 8 | 100% Floor Preserved |
| **$N = 10$** | 0.2559 | 0.4693 | 0.9297 | 3.9916 | 19 | 19 | 100% Floor Preserved |
| **$N = 20$** | 0.5772 | 1.0145 | 1.8198 | 3.7926 | 50 | 50 | 100% Floor Preserved |
| **$N = 30$** | 1.4558 | 2.6808 | 3.3157 | 4.8049 | 135 | 135 | 100% Floor Preserved |

*\* Ghi chú: Giá trị Max Latency 39.74ms tại $N=5$ là observed outlier duy nhất trong 1.000 measurements (P99 = 0.3986ms); không suy đoán nguyên nhân khi chưa có profiling telemetry độc lập (ví dụ OS background preemption / thread context switch).*

---

## 10. ĐỀ XUẤT CỦA LEAD ARCHITECT VÀ CÁC RỦI RO CẦN LƯU Ý

### 10.1. Đề xuất Kỹ thuật Đã Hoàn tất Rà soát (Technical Review Completed — Awaiting Formal Sign-Off)
1. **Chốt Two-Phase Subset Search**: Chọn Two-Phase Subset Search kèm branch-and-bound làm giải pháp thuật toán cốt lõi, bảo đảm đạt sàn 100% và bảo toàn toàn bộ các quyết định Product (Q2 Fail-Closed, Q4 Hard Ceiling).
2. **Triển khai Preflight In-Memory**: Gom toàn bộ việc chọn câu của các targets vào bộ nhớ, chỉ mở ghi DB nguyên tử khi toàn bộ session được xác nhận hợp lệ.
3. **Chốt Quy tắc Sắp xếp Lượt Turns Nội bộ**: Đã chốt **Phương án 1 (Sắp xếp theo `_candidate_rank` tăng dần)** để gán tuần tự `turn_index`, bảo đảm ưu tiên câu hỏi có giá trị sư phạm cao nhất và tính xác định 100%.
4. **Quy trình Phê duyệt & Chuẩn bị Gate 3 Scope A**:
   - Toàn bộ thiết kế kỹ thuật, ranh giới dữ liệu và giao thức kiểm thử đã được rà soát và đặc tả đầy đủ trong ADR này.
   - Tài liệu giữ trạng thái **PROPOSED** chờ xác nhận ký duyệt chính thức (formal sign-off) từ Lead Architect, không tự tạo chữ ký hoặc ngày duyệt giả định.
   - Sau khi có formal sign-off, tiến hành triển khai mã nguồn tại `src/modules/interviews/question_selector.py` và kiểm thử nghiệm thu tại `tests/modules/interviews/test_question_selector.py` theo đúng Scope A.

### 10.2. Rủi ro Cần Product Owner Lưu Ý
1. **Tỷ lệ 409 tăng cao nếu Question Bank mỏng**:
   Do áp dụng Q2 2A Fail-Closed cùng Hard Ceiling không dung sai, nếu Question Bank tại môi trường thật thiếu các câu hỏi có thời lượng chuẩn (ví dụ chỉ có câu 400s trong khi ngân sách 360s), phiên phỏng vấn sẽ bị từ chối mở (HTTP 409). Product Owner và đội ngũ Content Ops cần bảo đảm Question Bank được nạp và kiểm định đầy đủ dữ liệu thời lượng trước khi tính năng dynamic được bật.
2. **Thời điểm kích hoạt Production**:
   Giữ nguyên quyết định: Toàn bộ công việc Gate 3 chỉ kiểm thử trong module test Scope A. Quyết định tích hợp Staging (Scope B) và bật cờ Production cần một phiên đánh giá riêng biệt sau khi có kết quả đo kiểm telemetry.
