# ĐẶC TẢ CHI TIẾT P1 PLANNER VÀ BỘ KỊCH BẢN NGHIỆM THU GATE 2
**Tài liệu Đặc tả Kỹ thuật Độc lập & Bộ Kịch bản Kiểm thử Thủ công cho P1 Planner**  
*Dự án: INTERVIA — Interview Chat Core*  
*Nhánh: `feat/interview-text-runtime`*  
*Ngày lập: 27/09/2026*  
*Trạng thái: Đặc tả đề xuất & Kịch bản phân tích lý thuyết — Chờ Product Owner phê duyệt trước khi triển khai code Gate 2*  

---

> [!IMPORTANT]
> **Tuyên bố Phạm vi & Nguyên tắc Thực thi**:
> - Tài liệu này được biên soạn để chuẩn bị cho **Gate 2 (Tái cấu trúc module P1 Planner)**.
> - **Chưa triển khai code, tests, hay database**: Mọi kịch bản, công thức và dữ liệu đầu ra trong tài liệu này là **kết quả tính toán phân tích theo mô hình đặc tả giả định (analytical specification modeling)**, phục vụ công tác review và phê duyệt của Product Owner, **hoàn toàn KHÔNG PHẢI là kết quả thực thi runtime hay unit test đã chạy trong mã nguồn**.
> - **Không tự quyết định thay Product**: Các hành vi phụ thuộc vào các quyết định Product đang mở (Decision Log) được trình bày thành các nhánh cấu hình độc lập; kỹ thuật không âm thầm chốt cứng bất kỳ phương án nào.

---

## 1. BẢNG KIỂM TRA CÁC QUYẾT ĐỊNH ẢNH HƯỞNG TRỰC TIẾP ĐẾN P1 PLANNER

Từ 8 quyết định trong Decision Log của Gate 1, dưới đây là bảng phân loại chi tiết các quyết định có ảnh hưởng trực tiếp đến logic nghiệp vụ của P1 Planner, kèm các phương án và đề xuất mặc định:

| Mã Quyết định | Nội dung cần chốt | Các phương án hiện có | Ảnh hưởng cụ thể lên P1 Planner | Đề xuất kỹ thuật cho Gate 2 (Đánh dấu chờ duyệt) |
| :--- | :--- | :--- | :--- | :--- |
| **Quyết định 1** | Cấu trúc lượt mở đầu & Ngân sách Onboarding | • **1A**: Tách 2 lượt cố định ($N_{\text{onboarding}}=2$, $T_{\text{onboarding\_base}} \approx 210–270$s).<br>• **1B**: Gộp 1 lượt cố định ($N_{\text{onboarding}}=1$, $T_{\text{onboarding\_base}} \approx 90–120$s).<br>• **1C.1**: 1 lượt + CV follow-up dùng chung trần probe và `T_probe_pool`.<br>• **1C.2a**: 1 lượt + Quota CV riêng ($N_{\text{cv}} \le 1$), dùng chung `T_probe_pool`.<br>• **1C.2b**: 1 lượt + Quota CV riêng + Quỹ riêng (Cách 1 Inclusive gộp vào $T_{\text{onboarding}}$ hoặc Cách 2 Decoupled trừ riêng $T_{\text{cv\_standalone\_reserve}}$). | Ảnh hưởng trực tiếp đến công thức tính ngân sách chuyên môn khả dụng $T_{\text{tech\_pool}}$. P1 cần biết chính xác các khoản $T_{\text{onboarding}}$, $N_{\text{onboarding}}$, và $T_{\text{cv\_standalone\_reserve}}$ để trừ khỏi tổng thời lượng phiên. | **[Đề xuất tham số hóa - Chờ Product duyệt]**: Gate 2 thiết kế P1 nhận $T_{\text{onboarding}}$, $N_{\text{onboarding}}$, $T_{\text{cv\_standalone\_reserve}}$ như các **tham số cấu hình tiêm vào (dependency injection)**. Mặc định kiểm thử đề xuất tạm thời: Baseline Mode 1 lượt Decoupled 1C.2a ($T_{\text{onboarding}} = 120$s, $T_{\text{cv\_standalone\_reserve}} = 0$s). |
| **Quyết định 5** | Chính sách khi Competency Coding nhận Envelope nhỏ hơn 360 giây | • **5A**: Tự động giáng cấp xuống archetype `TEXT` (giữ trong agenda, chỉ hỏi lý thuyết, sàn giảm còn 180s).<br>• **5B**: Bỏ qua target (`omission_reason: "insufficient_envelope_for_coding_assessment"`), chuyển pool thành buffer chưa phân bổ hoặc xét target kế tiếp theo Decision 7.<br>• **5C (Target Consolidation — Nằm ngoài phạm vi Gate 2)**: P1 chủ động cắt giảm 1 competency phụ khác để dồn ngân sách nâng Time Envelope của target coding lên tối thiểu 360s. Phương án này đòi hỏi cơ chế tối ưu hóa tổ hợp (combinatorial knapsack/backtracking), nằm ngoài phạm vi tái cấu trúc P1 tuần tự ở Gate 2 và được chuyển vào Backlog tương lai; Gate 2 chỉ hỗ trợ và chờ Product duyệt giữa 5A và 5B. | Ảnh hưởng đến việc P1 gán archetype và quyết định giữ hay loại bỏ target coding khi ngân sách kỹ thuật còn lại không đạt mức sàn 360s. | **[Tham số hóa chờ Product duyệt giữa 5A và 5B]**: P1 hỗ trợ tham số cấu hình `coding_fallback_policy` (`'downgrade_to_text'` [5A] \| `'omit'` [5B]).<br>• **Không triển khai 5C trong Gate 2** (thuộc backlog tối ưu hóa tương lai).<br>• Nếu JD có cờ `strict_hands_on_required: true`: **Bắt buộc áp dụng 5B (Omission)**, tuyệt đối không giáng cấp.<br>• Nếu `strict_hands_on_required: false`: Áp dụng theo chính sách Product chọn (5A hoặc 5B, chưa mặc định chốt cứng). |
| **Quyết định 7** | Chính sách khi Competency đầu bảng có mức sàn vượt quá `T_tech_pool` | • **7A**: Strict Priority Stop — dừng duyệt ngay, không phỏng vấn target phụ phía sau, $K_{\text{eligible}} = 0$, chuyển toàn bộ pool thành buffer.<br>• **7B**: Skip-and-Continue — bỏ qua target đầu bảng bị thiếu thời gian, tiếp tục duyệt target phía sau có mức sàn vừa với pool. | Ảnh hưởng đến điều kiện dừng của vòng lặp duyệt tuần tự target đủ điều kiện ($K_{\text{eligible}}$). Quyết định xem P1 có được phép "nhảy cóc" qua năng lực cốt lõi để phỏng vấn năng lực phụ hay không. | **[Đề xuất cờ cấu hình - Chờ Product duyệt]**: P1 hỗ trợ tham số `strict_priority_stop` (boolean). Khuyến nghị mặc định `strict_priority_stop = True` (Phương án 7A) cho chế độ Formal Tuyển dụng; `False` (Phương án 7B) cho chế độ Mock Practice. |

### Các quyết định thuộc Gate 3 / Gate 4 (Không chặn triển khai P1 Planner)

Các quyết định sau đây thuộc về các giai đoạn sau trong pipeline, **hoàn toàn không chặn (non-blocking) công việc triển khai P1 ở Gate 2**:
- **Quyết định 2 (Question Bank Deficit Policy)**: Thuộc **Gate 3 (P2 Question Selector)**. P1 chỉ phân bổ Time Envelope theo competency, không truy vấn Question Bank nên không bị ảnh hưởng bởi việc thiếu câu hỏi trong kho.
- **Quyết định 3 (UI Progress Display)**: Thuộc **Frontend / Gate 4**. Quy định cách hiển thị tiến độ (Stage Stepper vs Dải tương tác), P1 chỉ xuất metadata ước tính phục vụ hiển thị.
- **Quyết định 4 (Time Envelope Ceiling - Hard vs Soft)**: Thuộc **Gate 3 (P2 Question Selector)**. Quy định việc P2 có được vượt nhẹ envelope hay không. P1 luôn giao envelope chuẩn xác theo thuật toán phân bổ.
- **Quyết định 6 (`max_runtime_probes`)**: Thuộc **Gate 4 (Runtime Engine)**. Quy định trần số lượt probe thực tế tại runtime. P1 chỉ sử dụng giá trị này như một tham số tham chiếu để tính trường heuristic định hướng `estimated_total_interaction_turns_range`.
- **Quyết định 8 (Quyết toán `T_probe_pool`)**: Thuộc **Gate 4 (Runtime Engine)**. Quy định cơ chế giữ buffer hay hoàn lại thời gian probe thừa tại runtime. P1 chỉ trừ khoản dự trữ vĩ mô $T_{\text{probe\_pool}}$ ban đầu.

---

## 2. ĐẶC TẢ KỸ THUẬT P1 PLANNER ĐỘC LẬP (GATE 2 SPECIFICATION)

### 2.1. Hợp đồng Dữ liệu Đầu vào (Input Contract)
P1 Planner nhận các dữ liệu cấu trúc sau để lập kế hoạch phiên:

1. **Thông tin phiên & Thời lượng**:
   - `duration_minutes`: int (ví dụ: 15, 25, 45 phút).
   - $T_{\text{session}} = \text{duration\_minutes} \times 60$ (giây).
2. **Hồ sơ yêu cầu công việc (`job: CanonicalJob`)**:
   - Phân biệt rõ hai tầng dữ liệu đầu vào:
     - **Tầng Requirement cấp JD (`job.requirements`)**: Tập hợp các yêu cầu trích xuất nguyên bản từ JD ($R_{\text{all}}$). Mỗi requirement gồm:
       - `requirement_id`: str.
       - `priority`: `'must_have'` | `'nice_to_have'` | `'context'`.
       - `taxonomy_refs`: Danh sách các concept năng lực chuẩn hóa (`concept_id`, `label`, `taxonomy_version`).
       - `tags`: Danh sách nhãn kỹ thuật (ví dụ: `['coding_problem', 'python', 'algorithm']` hoặc `['system_design', 'architecture']`).
       - `source_evidence_ref`: Trích dẫn văn bản JD gốc.
     - **Tầng Canonical Competency Target ($C_{\text{all}}$)**: Danh sách các concept năng lực chuẩn hóa duy nhất thu được sau khi phân giải taxonomy từ các requirement kỹ thuật. Quan hệ giữa requirement và competency concept là N-N (1 requirement có thể chứa nhiều `taxonomy_refs`, hoặc nhiều requirement cùng quy chiếu về 1 concept).
   - `job.strict_hands_on_required`: bool (Mặc định `false`. Nếu `true`, bắt buộc phải kiểm tra live coding hands-on cho năng lực code, không cho phép giáng cấp sang câu hỏi lý thuyết).
   - `job.seniority`: Cấp bậc chuyên môn (`intern`, `fresher`, `junior`, `mid`, `senior`, `lead`).
3. **Kết quả đối sánh hồ sơ ứng viên (`match: MatchResult`)**:
   - `match.requirement_results`: Kết quả thẩm định từng requirement:
     - `requirement_id`: str.
     - `status`: `'met'` | `'not_met'` | `'unknown'` | `'not_applicable'`.
     - `concept_results`: Chi tiết trạng thái của từng concept con.
4. **Cấu hình chính sách hệ thống (`policy_config: PlannerPolicyConfig`)**:
   - Bảng tham số cấu hình tiêm vào (Dependency Injection), cho phép thích ứng với các quyết định Product:
     - $T_{\text{onboarding\_base}}$ (mặc định 120s cho Mode 1 lượt; 240s cho Mode 2 lượt).
     - $T_{\text{cv\_addon\_inclusive}}$ (mặc định 0s khi Decoupled; 90–120s khi Inclusive kèm 1C.2b).
     - $T_{\text{cv\_standalone\_reserve}}$ (mặc định 0s khi 1C.2a/Inclusive; 90–120s khi Decoupled kèm 1C.2b).
     - $N_{\text{onboarding}}$ (1 hoặc 2 lượt).
     - $T_{\text{behavioral}}$ (mặc định 210s = 3.5 phút cho câu STAR).
     - $\text{probe\_pool\_ratio}$ (mặc định 0.15–0.20, tức 15%–20% $T_{\text{session}}$).
     - $T_{\text{closing\_reserve}}$ (mặc định 180s = 3.0 phút).
     - $t_{\text{arch\_text\_min}}$ (180s), $t_{\text{arch\_text\_max}}$ (240s).
     - $t_{\text{arch\_code\_min}}$ (360s), $t_{\text{arch\_code\_max}}$ (480s).
     - `strict_priority_stop`: bool (Phương án 7A: `True` | Phương án 7B: `False` — Chờ Product duyệt).
     - `coding_fallback_policy`: `'downgrade_to_text'` (Phương án 5A) | `'omit'` (Phương án 5B) — Chờ Product duyệt (Gate 2 chỉ triển khai 5A và 5B; Phương án 5C Target Consolidation nằm ngoài phạm vi Gate 2 và thuộc backlog tương lai).

---

### 2.2. Quy trình Tính toán Ngân sách Chuyên môn Khả dụng ($T_{\text{tech\_pool}}$)

Công thức tổng quát tính quỹ thời gian chuyên môn kỹ thuật khả dụng:
$$T_{\text{tech\_pool}} = T_{\text{session}} - \left( T_{\text{onboarding}} + T_{\text{cv\_standalone\_reserve}} + T_{\text{behavioral}} + T_{\text{probe\_pool}} + T_{\text{closing\_reserve}} \right)$$

Trong đó:
1. **Thời lượng mở đầu ($T_{\text{onboarding}}$)**:
   $$T_{\text{onboarding}} = T_{\text{onboarding\_base}} + T_{\text{cv\_addon\_inclusive}}$$
   - Khi chọn **Cách 1 (Inclusive)**: $T_{\text{cv\_addon\_inclusive}} \approx 90–120$s $\implies T_{\text{onboarding}} \approx 3.0–4.0$ phút; $T_{\text{cv\_standalone\_reserve}} = 0$.
   - Khi chọn **Cách 2 (Decoupled)**: $T_{\text{cv\_addon\_inclusive}} = 0 \implies T_{\text{onboarding}} = T_{\text{onboarding\_base}} \approx 1.5–2.0$ phút; $T_{\text{cv\_standalone\_reserve}} \approx 90–120$s (nếu 1C.2b) hoặc $0$s (nếu 1C.2a).
2. **Quỹ dự trữ câu hỏi đào sâu ($T_{\text{probe\_pool}}$)**:
   $$T_{\text{probe\_pool}} = \text{probe\_pool\_ratio} \times T_{\text{session}}$$
   - Gói 15 phút ($900$s) $\rightarrow 135–180$s.
   - Gói 25 phút ($1500$s) $\rightarrow 225–300$s.
   - Gói 45 phút ($2700$s) $\rightarrow 405–540$s.
   - *Nguyên tắc bảo vệ chống tính trùng*: Nếu 1C.2b được áp dụng (đã cấp quỹ riêng cho CV follow-up), $T_{\text{probe\_pool}}$ được bảo toàn 100% cho probe kỹ thuật. Nếu 1C.1 hoặc 1C.2a được áp dụng, lượt CV follow-up dùng chung thời gian từ $T_{\text{probe\_pool}}$.

---

### 2.3. Quy trình Xếp hạng Ưu tiên Competency

P1 chuyển đổi các requirements thành danh sách competency ứng viên và tính trọng số kết hợp:

0. **Tiền lọc Target (Candidate Pre-filtering) & Phân loại Truy vết**:
   - Với mỗi canonical competency target $c_i$: Nếu $100\%$ các requirements cấu thành nên $c_i$ đều có trạng thái thẩm định là `not_applicable` (dẫn đến trọng số thô $W_i = 0$ do $b_{\text{stat}} = 0.0$), target này sẽ được **loại khỏi danh sách candidate targets** bước vào quy trình xếp hạng và cấp ngân sách thời gian.
   - **Xử lý truy vết hai tầng nhất quán**:
     - *Ở tầng Canonical Competency Target*: Target $c_i$ được ghi nhận trực tiếp vào danh sách `nonInterviewedTargets` với lý do thẩm định:
       `omissionReason: "not_applicable_for_candidate"`, kèm chi tiết `omissionDetail: {"matchStatus": "not_applicable", "weight": 0.0, "reason": "100% of underlying requirements evaluated as not_applicable"}`. Nhờ đó, tập toàn thể $C_{\text{all}}$ được bảo toàn hoàn hảo: $C_{\text{all}} = \text{targets} \cup \text{nonInterviewedTargets}$ mà không để bất kỳ target nào biến mất khỏi hệ thống truy vết.
     - *Ở tầng JD Requirement*: Toàn bộ các requirements cấu thành nên $c_i$ được phản ánh đầy đủ trong mảng `evaluationTargets` với `status: "not_applicable"`, `evaluationMode: "skipped"`, và `attention: "not_applicable"`.
1. **Trọng số Ưu tiên (Priority Weight)**:
   - `must_have`: $w_{\text{pri}} = 1.0$
   - `nice_to_have`: $w_{\text{pri}} = 0.45$
   - `context`: $w_{\text{pri}} = 0.20$
2. **Hệ số Thúc đẩy Trạng thái (Status Boost)**:
   - `not_met`: $b_{\text{stat}} = 1.35$ (khoảng trống năng lực, cần xác thực kỹ)
   - `unknown`: $b_{\text{stat}} = 1.20$ (thiếu bằng chứng, cần kiểm tra)
   - `met`: $b_{\text{stat}} = 1.00$ (đã có bằng chứng nhưng vẫn cần phỏng vấn đối soát)
   - `not_applicable`: $b_{\text{stat}} = 0.0$ (được tiền lọc ở Bước 0 chuyển sang `nonInterviewedTargets` và `evaluationTargets`)
3. **Trọng số thô của Competency Target ($W_i$)**:
   $$W_i = \sum_{req \in \text{requirements}(c_i)} \frac{w_{\text{pri}}(req) \times b_{\text{stat}}(req)}{\text{num\_concepts}(req)}$$
4. **Quy tắc Xếp hạng Thứ tự (Deterministic Ranking)**:
   Danh sách candidate targets còn lại được sắp xếp giảm dần theo tuple tiêu chí:
   1. Có chứa `must_have` hay không (True xếp trước False).
   2. Trọng số thô $W_i$ giảm dần.
   3. `taxonomy_version` tăng dần (ổn định định danh).
   4. `concept_id` tăng dần (ổn định định danh).

---

### 2.4. Xác định Archetype Sơ bộ và Mức sàn Thời lượng khi chưa có Question Bank

Tại P1, hệ thống hoàn toàn chưa truy vấn Question Bank. Archetype sơ bộ được suy đoán từ taxonomy metadata và tags của JD requirements:
1. **Quy tắc phân loại Archetype sơ bộ**:
   - Nếu requirement có chứa tags: `coding_problem`, `live_coding`, `algorithm`, `data_structures`, hoặc JD mô tả yêu cầu bài tập code thực hành $\implies$ Đánh dấu sơ bộ là `CODING`.
   - Nếu requirement thuộc nhóm khái niệm, kiến trúc, lý thuyết, quy trình, công cụ, DevOps, system design $\implies$ Đánh dấu sơ bộ là `TEXT`.
   - Trường hợp lai (Hybrid, ví dụ "Python Core"):
     - Nếu $T_{\text{tech\_pool}} \ge 720$s và JD có từ khóa lập trình hands-on $\implies$ Đánh dấu `CODING`.
     - Ngược lại $\implies$ Đánh dấu `TEXT`.
2. **Mức sàn thời lượng cơ sở ($t_{\text{floor}}$) và dải ước tính**:
   - `TEXT`: Mức sàn $t_{\text{floor}} = 180$s (3.0 phút). Dải thời lượng ước tính 1 câu: $[180\text{s}, 240\text{s}]$.
   - `CODING`: Mức sàn $t_{\text{floor}} = 360$s (6.0 phút). Dải thời lượng ước tính 1 câu: $[360\text{s}, 480\text{s}]$.

---

### 2.5. Thuật toán Lựa chọn Target và Phân bổ Time Envelope (Hamilton-Hare)

#### Bước 1: Xác định tập hợp Target Đủ điều kiện ($K_{\text{eligible}}$) và Áp dụng Chính sách Coding Fallback (Decision 5)
P1 duyệt tuần tự danh sách candidate targets đã xếp hạng ($c_1, c_2, \dots, c_n$):
- Khởi tạo danh sách đủ điều kiện $E = []$, tổng sàn tích lũy $S = 0$.
- Với từng target $c_i$, xác định ngân sách kỹ thuật còn lại: $T_{\text{rem}} = T_{\text{tech\_pool}} - S$.
  
  **Trường hợp 1: Target $c_i$ có archetype sơ bộ là `TEXT`**:
  - Mức sàn: $t_{\text{floor}}(c_i) = 180$s.
  - Nếu $S + 180 \le T_{\text{tech\_pool}}$:
    - Thêm $c_i$ vào $E$ với `targetArchetype = TEXT`.
    - Cập nhật $S \leftarrow S + 180$.
  - Nếu $S + 180 > T_{\text{tech\_pool}}$:
    - Target không đủ sàn $\implies$ Chuyển sang kiểm tra Chính sách Ưu tiên (Decision 7).

  **Trường hợp 2: Target $c_i$ có archetype sơ bộ là `CODING`**:
  - **Nhánh 2.1: Khi ngân sách còn lại đủ cho bài tập coding ($T_{\text{rem}} \ge 360$s)**:
    - Xác nhận chính thức `targetArchetype = CODING`, mức sàn $t_{\text{floor}}(c_i) = 360$s.
    - Thêm $c_i$ vào $E$, cập nhật $S \leftarrow S + 360$.
  - **Nhánh 2.2: Khi ngân sách còn lại KHÔNG đủ cho bài tập coding ($T_{\text{rem}} < 360$s)**:
    - **Áp dụng Chính sách Coding Fallback (Decision 5) TRƯỚC KHI quyết định tính đủ điều kiện**:
      1. **Kiểm tra tính bắt buộc hands-on (`job.strict_hands_on_required`)**:
         - Nếu `strict_hands_on_required == True`: Tuyệt đối **không giáng cấp** sang câu hỏi lý thuyết TEXT. Target $c_i$ giữ nguyên sàn 360s $\implies$ Bị xem là không đủ ngân sách theo hình thức bắt buộc $\implies$ Áp dụng hành vi **Omission (theo Phương án 5B)**: đưa $c_i$ vào `nonInterviewedTargets` với `omissionReason: "insufficient_envelope_for_coding_assessment"`. Sau đó chuyển sang kiểm tra Chính sách Ưu tiên (Decision 7).
      2. **Nếu `strict_hands_on_required == False`**: Hành vi phụ thuộc vào cấu hình `coding_fallback_policy` (chờ Product duyệt giữa 5A và 5B; Phương án 5C Target Consolidation nằm ngoài phạm vi Gate 2 do đòi hỏi cơ chế tối ưu hóa tổ hợp knapsack/backtracking):
         - **Nếu chọn Phương án 5A (`coding_fallback_policy == 'downgrade_to_text'`)**:
           - Tự động **giáng cấp hình thức phỏng vấn sang `targetArchetype = TEXT`** (chuyển sang phỏng vấn lý thuyết chuyên sâu).
           - Mức sàn được điều chỉnh giảm từ 360s xuống $t_{\text{floor}}(c_i) = 180$s.
           - Kiểm tra lại sức chứa với mức sàn mới 180s:
             - Nếu $S + 180 \le T_{\text{tech\_pool}}$: Target $c_i$ (dưới archetype `TEXT`) **trở thành ĐỦ ĐIỀU KIỆN (`eligible`)**! Thêm $c_i$ vào $E$, cập nhật $S \leftarrow S + 180$. Vòng lặp tiếp tục duyệt target kế tiếp $c_{i+1}$ (không kích hoạt Decision 7 vì target đã được chọn).
             - Nếu $S + 180 > T_{\text{tech\_pool}}$: Ngay cả mức sàn 180s cũng không đủ $\implies c_i$ bị loại, đưa vào `nonInterviewedTargets` với `omissionReason: "insufficient_tech_pool_for_minimum_envelope"`, sau đó chuyển sang kiểm tra Decision 7.
         - **Nếu chọn Phương án 5B (`coding_fallback_policy == 'omit'`)**:
           - Không giáng cấp, giữ nguyên $t_{\text{floor}}(c_i) = 360$s. Target $c_i$ bị loại khỏi agenda, đưa vào `nonInterviewedTargets` với `omissionReason: "insufficient_envelope_for_coding_assessment"`. Sau đó chuyển sang kiểm tra Decision 7.

  **Quy tắc kiểm tra Chính sách Ưu tiên (Decision 7)** (khi target hiện tại không đủ sàn):
  - Nếu `strict_priority_stop == True` (Phương án 7A): **Dừng duyệt ngay lập tức**. Không xét các target phía sau. Toàn bộ các target chưa duyệt chuyển vào `nonInterviewedTargets` với `omissionReason: "strict_priority_halted_due_to_higher_rank"`.
  - Nếu `strict_priority_stop == False` (Phương án 7B): Bỏ qua target hiện tại, ghi vào `nonInterviewedTargets`, và tiếp tục duyệt target tiếp theo $c_{i+1}$ (nếu $c_{i+1}$ có mức sàn vừa với $T_{\text{rem}}$ còn lại).

#### Bước 2: Xử lý Kịch bản Biên khi $K_{\text{eligible}} = 0$
Nếu sau Bước 1 mà danh sách $E$ rỗng ($K_{\text{eligible}} = 0$):
1. **Ngắt thuật toán phân bổ trọng số**: Tuyệt đối không thực hiện phép chia cho tổng trọng số ($\sum w_k = 0$).
2. **Gán ngân sách**:
   - Tổng envelope cấp cho các target: $\sum \text{time\_envelope} = 0$.
   - Toàn bộ quỹ kỹ thuật dôi dư chuyển thành buffer:
     $$\text{unallocated\_buffer\_seconds} = T_{\text{tech\_pool}}$$
3. **Xử lý danh sách bỏ qua**: Chuyển toàn bộ $100\%$ targets vào `nonInterviewedTargets` kèm lý do truy vết:
   - Target đầu tiên: `omission_reason: "insufficient_tech_pool_for_minimum_envelope"` (hoặc `"insufficient_envelope_for_coding_assessment"` nếu là coding target).
   - Các target tiếp theo (nếu áp dụng 7A): `omission_reason: "strict_priority_halted_due_to_higher_rank"`.

#### Bước 3: Phân bổ Thặng dư theo Largest Remainder (Hamilton-Hare) khi $K_{\text{eligible}} > 0$
Nếu $K_{\text{eligible}} > 0$:
1. **Gán sàn ban đầu**: Với mỗi target $i \in E$:
   $$\text{envelope}_i = t_{\text{floor}}(c_i)$$
2. **Tính thặng dư ngân sách**:
   $$R = T_{\text{tech\_pool}} - \sum_{i \in E} t_{\text{floor}}(c_i) \quad (\text{đơn vị giây nguyên})$$
3. **Chuẩn hóa trọng số & Fallback An toàn (Zero Division Safety)**:
   - **Trường hợp chuẩn ($\sum_{k \in E} W_k > 0$)**:
     $$w_i = \frac{W_i}{\sum_{k \in E} W_k}$$
   - **Trường hợp Phòng vệ Sâu — Fallback Phân bổ Đều ($\sum_{k \in E} W_k == 0$)**:
     Trong luồng nghiệp vụ hợp lệ, do mọi target `not_applicable` đã được tiền lọc trước (chuyển sang `nonInterviewedTargets`), các candidate target đủ điều kiện trong $E$ luôn có trọng số $W_i > 0$ (vì $w_{\text{pri}} > 0$ và $b_{\text{stat}} \ge 1.0$). Tuy nhiên, để đảm bảo tính **phòng vệ sâu (defensive circuit-breaker)** đối với các trạng thái dữ liệu bất thường hoặc test fixtures được dựng cưỡng bức (ví dụ: mock match status bị lỗi, upstream validator bị bypass, hoặc toàn bộ requirements cấu hình có $w_{\text{pri}} = 0.0$ dẫn đến $\sum W_k = 0$), hệ thống kích hoạt **Cơ chế Phân bổ Đều (Equal Share Allocation Fallback)**:
     $$w_i = \frac{1}{|E|}, \quad \forall i \in E$$
     Thặng dư $s_i = \frac{R}{|E|}$, đảm bảo an toàn tuyệt đối không bao giờ phát sinh lỗi chia cho 0 (`ZeroDivisionError`).
4. **Tính phần thặng dư lý tưởng**:
   $$s_i = R \times w_i$$
5. **Cộng phần nguyên**:
   $$\text{envelope}_i \leftarrow \text{envelope}_i + \lfloor s_i \rfloor$$
6. **Phân phối phần dư làm tròn ($\Delta = R - \sum \lfloor s_i \rfloor$)**:
   - Sắp xếp các target theo phần thập phân $(s_i - \lfloor s_i \rfloor)$ giảm dần (nếu bằng nhau, ưu tiên target có selection rank nhỏ hơn).
   - Cộng thêm $1$ giây cho từng target đứng đầu danh sách cho đến khi phân phối hết $\Delta$ giây.
7. **Bảo đảm toán học**:
   $$\sum_{i \in E} \text{envelope}_i \equiv T_{\text{tech\_pool}}$$
   Toàn bộ pool được phân bổ hết, không thừa, không thiếu, và không vượt quá dù chỉ 1 giây.

---

### 2.6. Hợp đồng Dữ liệu Đầu ra (Output Contract của P1)

Payload JSON đầu ra của P1 bao gồm các trường cấu trúc sau:

```json
{
  "policyVersion": "interview-planner-v2-dynamic",
  "sessionDurationMinutes": 25,
  "techPoolSeconds": 750,
  "unallocatedBufferSeconds": 0,
  "targets": [
    {
      "selectionRank": 0,
      "conceptId": "concept-python-core",
      "taxonomyVersion": "v1.0",
      "label": "Python Core & AsyncIO",
      "targetArchetype": "TEXT",
      "timeEnvelopeSeconds": 375,
      "floorSeconds": 180,
      "estimatedQuestionsRange": [1, 3],
      "importance": 0.583333,
      "rationale": {
        "source": "job_requirement",
        "requirementIds": ["req-py-01"],
        "priorities": ["must_have"],
        "matchStatuses": ["unknown"],
        "jobEvidenceRefs": ["JD Section 3.1"]
      }
    }
  ],
  "nonInterviewedTargets": [
    {
      "conceptId": "concept-docker",
      "taxonomyVersion": "v1.0",
      "label": "Docker Containerization",
      "priority": "nice_to_have",
      "omissionReason": "insufficient_tech_pool_for_minimum_envelope",
      "omissionDetail": {
        "poolRemainingSeconds": 0,
        "floorRequiredSeconds": 180
      }
    },
    {
      "conceptId": "concept-legacy-soap",
      "taxonomyVersion": "v1.0",
      "label": "SOAP/XML Legacy Integration",
      "priority": "nice_to_have",
      "omissionReason": "not_applicable_for_candidate",
      "omissionDetail": {
        "matchStatus": "not_applicable",
        "weight": 0.0,
        "reason": "100% of underlying requirements evaluated as not_applicable"
      }
    }
  ],
  "evaluationTargets": [
    {
      "requirementId": "req-py-01",
      "priority": "must_have",
      "kind": "skill",
      "label": "Python Core & AsyncIO",
      "status": "unknown",
      "evaluationMode": "competency",
      "attention": "validate_gap"
    },
    {
      "requirementId": "req-soap-01",
      "priority": "nice_to_have",
      "kind": "skill",
      "label": "SOAP/XML Legacy Integration",
      "status": "not_applicable",
      "evaluationMode": "skipped",
      "attention": "not_applicable"
    }
  ],
  "estimatedTurnsRange": {
    "estimatedFrozenTurnsRange": [3, 5],
    "estimatedTotalInteractionTurnsRange": [3, null]
  },
  "fingerprint": "a3f5b7..."
}
```

> [!NOTE]
> **Minh giải các trường dữ liệu và công thức trong Output Contract mẫu**:
> 1. **Cấu hình tạo ra `techPoolSeconds = 750`** (Phiên 25 phút, tức $T_{\text{session}} = 1500$s):
>    - $T_{\text{onboarding}} = 120$s ($N_{\text{onboarding}} = 1$, Decoupled Mode 1C.2a).
>    - $T_{\text{cv\_standalone\_reserve}} = 0$s (theo Mode 1C.2a dùng chung $T_{\text{probe\_pool}}$).
>    - $T_{\text{behavioral}} = 210$s (3.5 phút cho câu STAR).
>    - $T_{\text{probe\_pool}} = 240$s (ứng với $\text{probe\_pool\_ratio} = 0.16$, tức $16\%$ của $1500$s: $1500 \times 0.16 = 240$s).
>    - $T_{\text{closing\_reserve}} = 180$s (3.0 phút).
>    - Tổng các khoản trừ: $120 + 0 + 210 + 240 + 180 = 750$ giây.
>    - Ngân sách kỹ thuật: $T_{\text{tech\_pool}} = 1500 - 750 = 750$ giây.
> 2. **Dải câu hỏi `estimatedQuestionsRange = [1, 3]`** khi $\text{timeEnvelopeSeconds} = 375$ (archetype `TEXT`):
>    - Mức sàn: $t_{\text{arch\_min}} = 180$s; mức trần 1 câu: $t_{\text{arch\_max}} = 240$s.
>    - Cận dưới: $\max(1, \lfloor 375 / 240 \rfloor) = \max(1, 1) = 1$ câu.
>    - Cận trên: $\max(1, \lceil 375 / 180 \rceil) = \max(1, \lceil 2.083 \rceil) = 3$ câu.
>    - Do đó, nếu không có cap bổ sung thì dải số câu hỏi đúng theo công thức toán học bắt buộc phải là `[1, 3]`.
> 3. **Dải lượt tương tác `estimatedTotalInteractionTurnsRange = [3, null]`**:
>    - Trong chế độ 1C.2a (CV conditional follow-up có quota riêng tối đa 1 lượt nhưng dùng chung quỹ $T_{\text{probe\_pool}}$ với technical probes), cận trên của tổng số lượt tương tác runtime **chưa có giá trị định lượng cố định khi thiếu policy giới hạn probe cụ thể hoặc thiếu telemetry thực tế**.
>    - Giá trị `[3, 7]` từng xuất hiện trước đây chỉ là một **ví dụ có điều kiện** (giả định kịch bản có trần probe kỹ thuật $\le 2$ lượt và kích hoạt $1$ lượt CV follow-up: $5 + 2 = 7$), hoàn toàn không phải là dải cố định áp dụng chung cho mọi phiên.

---

### 2.7. Các Bất biến Hệ thống (System Invariants) Bắt buộc Duy trì

Mã nguồn triển khai của P1 Planner tại Gate 2 phải tuân thủ nghiêm ngặt các bất biến sau:

1. **Invariant 1 (Strict Budget Non-Exceedance)**:
   $$\sum_{i} \text{time\_envelope}(c_i) \le T_{\text{tech\_pool}} \quad (\text{Luôn đúng 100\% trong mọi điều kiện})$$
2. **Invariant 2 (Exact Pool Exhaustion when Eligible)**:
   $$\text{Khi } K_{\text{eligible}} > 0 \implies \sum_{i} \text{time\_envelope}(c_i) + \text{unallocatedBufferSeconds} \equiv T_{\text{tech\_pool}}$$
3. **Invariant 3 (Zero Division Safety)**:
   Tuyệt đối không bao giờ phát sinh ngoại lệ chia cho 0 (`ZeroDivisionError`):
   - Khi $K_{\text{eligible}} = 0 \implies$ Ngắt hoàn toàn thuật toán phân bổ thặng dư, chuyển toàn bộ $T_{\text{tech\_pool}}$ thành `unallocatedBufferSeconds`.
   - Khi $K_{\text{eligible}} > 0$ nhưng $\sum_{k \in E} W_k == 0 \implies$ Kích hoạt cơ chế Fallback Phân bổ Đều (Equal Share Allocation) với $w_i = \frac{1}{|E|}$.
4. **Invariant 4A (JD Requirement Level Traceability)**:
   Mọi requirement $req \in \text{job.requirements}$ phải được ánh xạ đúng một lần:
   $$\text{len}(\text{job.requirements}) \equiv \text{len}(\text{evaluationTargets}) + \text{len}(\text{nonCompetencyRequirements})$$
   Trong đó, mọi requirement liên quan đến năng lực chuyên môn đều được ghi nhận trong `evaluationTargets` kèm trạng thái rõ ràng (`attention`: `"validate_gap"`, `"confirm_strength"`, `"not_applicable"`, v.v.).
5. **Invariant 4B (Canonical Competency Target Level Traceability)**:
   Tập hợp tất cả Canonical Competency Targets được sinh ra từ các requirements kỹ thuật của JD ($C_{\text{all}}$) phải được phân hoạch hoàn toàn thành hai tập rời nhau:
   $$C_{\text{all}} = \text{targets} \cup \text{nonInterviewedTargets} \quad \text{với } \text{targets} \cap \text{nonInterviewedTargets} = \emptyset$$
   $$\text{len}(C_{\text{all}}) \equiv \text{len}(\text{targets}) + \text{len}(\text{nonInterviewedTargets})$$
   Trong đó, `nonInterviewedTargets` chứa đầy đủ cả các target bị bỏ qua do giới hạn thời lượng/thứ bậc ưu tiên (`"insufficient_tech_pool_for_minimum_envelope"`, `"insufficient_envelope_for_coding_assessment"`, `"strict_priority_halted_due_to_higher_rank"`) VÀ các target bị tiền lọc do không áp dụng (`"not_applicable_for_candidate"`). Tuyệt đối không để bất kỳ competency target nào biến mất khỏi hệ thống truy vết.
6. **Invariant 5 (Zero Question Bank Dependency)**:
   P1 không bao giờ import, truy vấn database hay gán ID câu hỏi cụ thể (`question_version_id`). P1 hoàn toàn độc lập với Question Bank.
7. **Invariant 6 (Floor Guarantee for Selected Targets)**:
   Mỗi target $i$ được chọn vào `targets` phải nhận Time Envelope thỏa mãn:
   $$\text{time\_envelope}(c_i) \ge t_{\text{floor}}(c_i)$$

---

## 3. BỘ KỊCH BẢN NGHIỆM THU THỦ CÔNG CHO P1 (MANUAL ACCEPTANCE CASES)

> [!NOTE]
> Toàn bộ các kết quả tính toán dưới đây là **mô hình phân tích lý thuyết theo giả định (analytical specification check)** nhằm chứng minh tính đúng đắn toán học của đặc tả, **chưa phải kết quả chạy code/test thực tế**.

---

### Kịch bản 1 (TC-01): Một Target TEXT Duy nhất
- **Mục tiêu**: Kiểm tra kịch bản tối giản khi JD chỉ có 1 competency lý thuyết, phiên ngắn.
- **Dữ liệu giả định đầu vào**:
  - Gói thời lượng: $T_{\text{session}} = 15$ phút ($900$ giây).
  - Cấu hình ngân sách:
    - $T_{\text{onboarding}} = 120$s ($N_{\text{onboarding}}=1$, Decoupled Mode 1C.2a).
    - $T_{\text{cv\_standalone\_reserve}} = 0$s.
    - $T_{\text{behavioral}} = 210$s (3.5m).
    - $T_{\text{probe\_pool}} = 0.15 \times 900 = 135$s.
    - $T_{\text{closing\_reserve}} = 180$s (3.0m).
    - Ngân sách kỹ thuật:
      $$T_{\text{tech\_pool}} = 900 - (120 + 0 + 210 + 135 + 180) = 900 - 645 = 255\text{ giây}$$
  - Danh sách Requirement JD:
    - Target $c_1$: `concept-sql-basic` (Label: "SQL Query Basics", priority: `must_have`, status: `unknown`).
    - Archetype: `TEXT` $\implies t_{\text{floor}}(c_1) = 180$s.
- **Các bước tính toán**:
  1. Xếp hạng: Duy nhất $c_1$, trọng số thô $W_1 = 1.0 \times 1.2 = 1.2$.
  2. Kiểm tra sàn: $t_{\text{floor}}(c_1) = 180\text{s} \le T_{\text{tech\_pool}} = 255\text{s} \implies K_{\text{eligible}} = 1$. Target $c_1$ đủ điều kiện.
  3. Gán sàn ban đầu: $\text{envelope}_1 = 180$s.
  4. Thặng dư: $R = 255 - 180 = 75$s.
  5. Phân bổ thặng dư: Vì chỉ có 1 target, $w_1 = 1.0 \implies s_1 = 75$s.
  6. Envelope cuối cùng: $\text{envelope}_1 = 180 + 75 = 255$s.
  7. Dải câu hỏi dự kiến (P2 frozen turns):
     - $\min_{\text{frozen}} = 1_{\text{onb}} + \max(1, \lfloor 255 / 240 \rfloor) + 1_{\text{star}} = 1 + 1 + 1 = 3$ câu.
     - $\max_{\text{frozen}} = 1_{\text{onb}} + \max(1, \lceil 255 / 180 \rceil) + 1_{\text{star}} = 1 + 2 + 1 = 4$ câu.
- **Expected Output**:
  - `targets`: 1 phần tử ($c_1$), `targetArchetype`: "TEXT", `timeEnvelopeSeconds`: 255, `estimatedQuestionsRange`: [1, 2].
  - `unallocatedBufferSeconds`: 0.
  - `nonInterviewedTargets`: [] (Rỗng).
  - `estimatedFrozenTurnsRange`: [3, 4].
- **Invariants được kiểm chứng**:
  - Invariant 1: $255 \le 255$ (Đạt).
  - Invariant 2: $255 + 0 == 255$ (Đạt).
  - Invariant 6: $255 \ge 180$ (Đạt).

---

### Kịch bản 2 (TC-02): Nhiều Target với Must-Have và Nice-To-Have, Status Khác Nhau
- **Mục tiêu**: Kiểm tra thuật toán xếp hạng ưu tiên và phân bổ thặng dư Hamilton-Hare theo đơn vị giây nguyên.
- **Dữ liệu giả định đầu vào**:
  - Gói thời lượng: $T_{\text{session}} = 25$ phút ($1500$ giây).
  - Cấu hình ngân sách:
    - $T_{\text{onboarding}} = 120$s, $T_{\text{cv\_standalone\_reserve}} = 0$s, $T_{\text{behavioral}} = 210$s.
    - $T_{\text{probe\_pool}} = 0.15 \times 1500 = 225$s, $T_{\text{closing\_reserve}} = 180$s.
    - $T_{\text{tech\_pool}} = 1500 - (120 + 0 + 210 + 225 + 180) = 1500 - 735 = 765\text{ giây}$.
  - Danh sách Requirements:
    - $c_1$: `concept-system-arch` (Must-have, status: `unknown`, Archetype: TEXT, floor: 180s).
      $W_1 = 1.0 \times 1.20 = 1.20$.
    - $c_2$: `concept-database-opt` (Must-have, status: `met`, Archetype: TEXT, floor: 180s).
      $W_2 = 1.0 \times 1.00 = 1.00$.
    - $c_3$: `concept-redis-cache` (Nice-to-have, status: `not_met`, Archetype: TEXT, floor: 180s).
      $W_3 = 0.45 \times 1.35 = 0.6075$.
- **Các bước tính toán**:
  1. Thứ tự xếp hạng:
     - Rank 0: $c_1$ (Must-have, $W_1 = 1.20$).
     - Rank 1: $c_2$ (Must-have, $W_2 = 1.00$).
     - Rank 2: $c_3$ (Nice-to-have, $W_3 = 0.6075$).
  2. Kiểm tra sức chứa sàn:
     - Thêm $c_1$: $180\text{s} \le 765\text{s}$ (OK).
     - Thêm $c_2$: $180 + 180 = 360\text{s} \le 765\text{s}$ (OK).
     - Thêm $c_3$: $360 + 180 = 540\text{s} \le 765\text{s}$ (OK).
     $\implies K_{\text{eligible}} = 3$. Cả 3 target được chọn.
  3. Gán sàn ban đầu: $\text{envelope}_1 = 180$, $\text{envelope}_2 = 180$, $\text{envelope}_3 = 180$.
  4. Quỹ thặng dư: $R = 765 - 540 = 225$ giây.
  5. Tổng trọng số: $\sum W = 1.20 + 1.00 + 0.6075 = 2.8075$.
  6. Phân bổ thặng dư lý tưởng:
     - $s_1 = 225 \times \frac{1.20}{2.8075} \approx 96.17097$s $\implies \lfloor s_1 \rfloor = 96$s, phần dư lẻ: $0.17097$.
     - $s_2 = 225 \times \frac{1.00}{2.8075} \approx 80.14247$s $\implies \lfloor s_2 \rfloor = 80$s, phần dư lẻ: $0.14247$.
     - $s_3 = 225 \times \frac{0.6075}{2.8075} \approx 48.68655$s $\implies \lfloor s_3 \rfloor = 48$s, phần dư lẻ: $0.68655$.
  7. Phân phối phần dư nguyên:
     - Tổng phần nguyên: $96 + 80 + 48 = 224$ giây.
     - Sai số cần bù: $\Delta = 225 - 224 = 1$ giây.
     - Xếp hạng phần lẻ: Target $c_3$ cao nhất ($0.68655 > 0.17097 > 0.14247$).
     $\implies$ Target $c_3$ được cộng thêm $+1$ giây: $\text{bonus}_3 = 48 + 1 = 49$s.
  8. Envelope cuối cùng:
     - $\text{envelope}_1 = 180 + 96 = 276$ giây (4.60 phút).
     - $\text{envelope}_2 = 180 + 80 = 260$ giây (4.33 phút).
     - $\text{envelope}_3 = 180 + 49 = 229$ giây (3.82 phút).
     - Tổng kiểm tra: $276 + 260 + 229 = 765$ giây $\equiv T_{\text{tech\_pool}}$.
- **Expected Output**:
  - `targets`: 3 phần tử ($c_1$: 276s, $c_2$: 260s, $c_3$: 229s).
  - `unallocatedBufferSeconds`: 0.
  - `nonInterviewedTargets`: [].
- **Invariants được kiểm chứng**:
  - Invariant 1: $765 \le 765$ (Đạt).
  - Invariant 2: $765 + 0 == 765$ (Đạt, không lệch 1 giây).
  - Invariant 6: Mỗi target đều $> 180$s (Đạt).

---

### Kịch bản 3 (TC-03): Target CODING Cần Mức Sàn Cao
- **Mục tiêu**: Kiểm tra khả năng cấp mức sàn phân biệt giữa CODING (360s) và TEXT (180s) khi ngân sách kỹ thuật dồi dào.
- **Dữ liệu giả định đầu vào**:
  - Phiên 25 phút ($T_{\text{session}} = 1500$s), $T_{\text{tech\_pool}} = 765$ giây.
  - Requirements:
    - $c_1$: `concept-algo-coding` (Must-have, status: `unknown`, Tags: `['coding_problem', 'algorithm']`).
      $\implies$ Archetype sơ bộ: `CODING`, $t_{\text{floor}}(c_1) = 360$s. Trọng số: $W_1 = 1.0 \times 1.2 = 1.2$.
    - $c_2$: `concept-backend-design` (Must-have, status: `met`, Tags: `['architecture']`).
      $\implies$ Archetype sơ bộ: `TEXT`, $t_{\text{floor}}(c_2) = 180$s. Trọng số: $W_2 = 1.0 \times 1.0 = 1.0$.
  - Cấu hình chính sách: `coding_fallback_policy` có thể là 5A hoặc 5B; `strict_hands_on_required` có thể là `true` hoặc `false`.
- **Các bước tính toán**:
  1. Thứ tự: Rank 0 là $c_1$ (Coding, $W_1=1.2$), Rank 1 là $c_2$ (Text, $W_2=1.0$).
  2. Kiểm tra sức chứa và áp dụng chính sách:
     - Xét $c_1$: $T_{\text{rem}} = 765\text{s} \ge 360\text{s}$. Ngân sách còn lại đủ cho bài tập coding ngay từ đầu $\implies$ Xác nhận chính thức `targetArchetype = CODING`, mức sàn $t_{\text{floor}}(c_1) = 360$s. Target $c_1$ đủ điều kiện ($E = [c_1]$, $S = 360$s).
     - *Ghi chú về Chính sách Coding Fallback (Decision 5)*: Do ngân sách đủ sàn $360$s, cơ chế fallback không bị kích hoạt. Target $c_1$ luôn giữ nguyên `CODING` cho cả hai phương án 5A và 5B.
     - Xét $c_2$: $T_{\text{rem}} = 765 - 360 = 405\text{s} \ge 180\text{s} \implies$ Xác nhận `targetArchetype = TEXT`, $t_{\text{floor}}(c_2) = 180$s ($E = [c_1, c_2]$, $S = 540$s).
     $\implies K_{\text{eligible}} = 2$.
  3. Thặng dư: $R = 765 - 540 = 225$ giây.
  4. Trọng số chuẩn hóa: $\sum W = 2.2$.
     - $s_1 = 225 \times \frac{1.2}{2.2} \approx 122.727$s $\implies \lfloor s_1 \rfloor = 122$s, lẻ $0.727$.
     - $s_2 = 225 \times \frac{1.0}{2.2} \approx 102.273$s $\implies \lfloor s_2 \rfloor = 102$s, lẻ $0.273$.
  5. Xử lý làm tròn: $\sum \lfloor s \rfloor = 224$s, dư $\Delta = 1$s. Target $c_1$ có phần lẻ lớn hơn ($0.727 > 0.273$) nhận $+1$s $\implies \text{bonus}_1 = 123$s.
  6. Envelope cuối cùng:
     - $\text{envelope}_1 = 360 + 123 = 483$ giây (8.05 phút).
     - $\text{envelope}_2 = 180 + 102 = 282$ giây (4.70 phút).
     - Tổng: $483 + 282 = 765$ giây $\equiv T_{\text{tech\_pool}}$.
  7. Dải câu hỏi dự kiến:
     - Target $c_1$ (Coding, dải câu $[360\text{s}, 480\text{s}]$): Envelope $483$s $\implies$ [1, 2] câu coding.
     - Target $c_2$ (Text, dải câu $[180\text{s}, 240\text{s}]$): Envelope $282$s $\implies$ [1, 2] câu text.
- **Expected Output**:
  - `targets`:
    - $c_1$: `targetArchetype`: "CODING", `timeEnvelopeSeconds`: 483.
    - $c_2$: `targetArchetype`: "TEXT", `timeEnvelopeSeconds`: 282.
  - `nonInterviewedTargets`: [].
- **Invariants được kiểm chứng**:
  - Invariant 1 & 2: Tổng đúng bằng 765s.
  - Invariant 6: Target coding nhận $\ge 360$s ($483 \ge 360$), target text nhận $\ge 180$s ($282 \ge 180$).

---

### Kịch bản 4 (TC-04): Ngân Sách Đủ Cho Một Số Target Nhưng Không Đủ Cho Tất Cả
- **Mục tiêu**: Kiểm tra logic cắt giảm target khi vượt ngân sách ($K \times t_{\text{floor}} > T_{\text{tech\_pool}}$) và ghi nhận truy vết `nonInterviewedTargets`.
- **Dữ liệu giả định đầu vào**:
  - Phiên 20 phút ($T_{\text{session}} = 1200$s).
  - Giả sử $T_{\text{tech\_pool}} = 550$ giây.
  - Có 4 Competencies dạng `TEXT` ($t_{\text{floor}} = 180$s, tổng sàn cần $4 \times 180 = 720\text{s} > 550\text{s}$):
    - $c_1$: Must-have, status: `not_met` ($W_1 = 1.35$).
    - $c_2$: Must-have, status: `unknown` ($W_2 = 1.20$).
    - $c_3$: Must-have, status: `met` ($W_3 = 1.00$).
    - $c_4$: Nice-to-have, status: `unknown` ($W_4 = 0.54$).
- **Các bước tính toán**:
  1. Thứ tự ưu tiên: $c_1$ (Rank 0) $\rightarrow c_2$ (Rank 1) $\rightarrow c_3$ (Rank 2) $\rightarrow c_4$ (Rank 3).
  2. Duyệt tuần tự tích lũy sàn:
     - Xét $c_1$: Cần 180s. Tích lũy: $180\text{s} \le 550\text{s}$ (Nhận $c_1$).
     - Xét $c_2$: Cần 180s. Tích lũy: $180 + 180 = 360\text{s} \le 550\text{s}$ (Nhận $c_2$).
     - Xét $c_3$: Cần 180s. Tích lũy: $360 + 180 = 540\text{s} \le 550\text{s}$ (Nhận $c_3$).
     - Xét $c_4$: Cần 180s. Tích lũy: $540 + 180 = 720\text{s} > 550\text{s}$ (Không đủ sàn $\implies$ Loại $c_4$).
     $\implies K_{\text{eligible}} = 3$ ($c_1, c_2, c_3$). Target $c_4$ bị loại.
  3. Phân bổ thặng dư: $R = 550 - 540 = 10$ giây.
  4. Trọng số 3 target được chọn: $\sum W = 1.35 + 1.20 + 1.00 = 3.55$.
     - $s_1 = 10 \times \frac{1.35}{3.55} \approx 3.80$s $\implies \lfloor s_1 \rfloor = 3$s, lẻ $0.80$.
     - $s_2 = 10 \times \frac{1.20}{3.55} \approx 3.38$s $\implies \lfloor s_2 \rfloor = 3$s, lẻ $0.38$.
     - $s_3 = 10 \times \frac{1.00}{3.55} \approx 2.82$s $\implies \lfloor s_3 \rfloor = 2$s, lẻ $0.82$.
  5. Làm tròn phần dư: $\sum \lfloor s \rfloor = 3 + 3 + 2 = 8$s. Dư $\Delta = 10 - 8 = 2$ giây.
     - Hai target có phần lẻ lớn nhất là $c_3$ ($0.82$) và $c_1$ ($0.80$), mỗi target nhận $+1$s.
     $\implies \text{bonus}_1 = 4$s, $\text{bonus}_2 = 3$s, $\text{bonus}_3 = 3$s.
  6. Envelope cuối cùng:
     - $\text{envelope}_1 = 180 + 4 = 184$s.
     - $\text{envelope}_2 = 180 + 3 = 183$s.
     - $\text{envelope}_3 = 180 + 3 = 183$s.
     - Tổng: $184 + 183 + 183 = 550$ giây $\equiv T_{\text{tech\_pool}}$.
- **Expected Output**:
  - `targets`: 3 phần tử ($c_1: 184$s, $c_2: 183$s, $c_3: 183$s).
  - `unallocatedBufferSeconds`: 0.
  - `nonInterviewedTargets`:
    - `conceptId`: $c_4$.
    - `omissionReason`: `"insufficient_tech_pool_for_minimum_envelope"`.
    - `omissionDetail`: `{"poolRemainingSeconds": 10, "floorRequiredSeconds": 180}`.
- **Invariants được kiểm chứng**:
  - Invariant 1 & 2: Tổng bằng đúng 550s.
  - Invariant 4B: Target $c_4$ xuất hiện đầy đủ trong `nonInterviewedTargets` với giải trình rõ ràng.

---

### Kịch bản 5 (TC-05): Target Ưu Tiên Đầu Tiên Không Đủ Sàn CODING nhưng Đủ Sàn TEXT
- **Mục tiêu**: Kiểm tra tính đồng bộ giữa thứ tự áp dụng Chính sách Coding Fallback (Decision 5: 5A vs 5B) và Chính sách Ưu tiên (Decision 7: 7A vs 7B) khi target đầu bảng là CODING cần 360s nhưng pool chỉ có 240s.
- **Phạm vi Quyết định 5 trong Gate 2**: Kịch bản kiểm tra toàn diện hai phương án khả thi của Gate 2 là **5A (`downgrade_to_text`)** và **5B (`omit`)**. Phương án **5C (Target Consolidation — dồn ngân sách bằng cách loại bỏ competency khác)** nằm ngoài phạm vi duyệt tuần tự của Gate 2 (thuộc backlog tương lai) nên không được đưa vào kịch bản kiểm thử của Gate này.
- **Dữ liệu giả định đầu vào**:
  - Ngân sách kỹ thuật khả dụng: $T_{\text{tech\_pool}} = 240$ giây.
  - Danh sách Requirements:
    - $c_1$: Must-have, có tag `coding_problem` ($W_1 = 1.20$, archetype sơ bộ `CODING`, sàn coding cơ sở $360$s). Rank 0.
    - $c_2$: Nice-to-have, lý thuyết backend ($W_2 = 0.54$, archetype sơ bộ `TEXT`, sàn text cơ sở $180$s). Rank 1.
- **Tính toán theo các Tổ hợp Chính sách Product**:

#### Tổ hợp 1: Không bắt buộc Hands-on (`strict_hands_on_required == False`) & Áp dụng Phương án 5A (`coding_fallback_policy == 'downgrade_to_text'`)
1. **Xét target $c_1$**:
   - Ngân sách còn lại: $T_{\text{rem}} = 240\text{s} < 360\text{s}$ (không đủ sàn coding).
   - **Áp dụng Decision 5A trước khi xét eligibility**: Target $c_1$ tự động được giáng cấp sang `targetArchetype = TEXT`, mức sàn được điều chỉnh giảm từ 360s xuống $t_{\text{floor}}(c_1) = 180$s.
   - Kiểm tra sức chứa với sàn mới: $T_{\text{rem}} = 240\text{s} \ge 180\text{s} \implies c_1$ **trở thành ĐỦ ĐIỀU KIỆN (`eligible`)**!
   - Thêm $c_1$ vào $E$, cập nhật $S \leftarrow 180$s ($E = [c_1]$).
2. **Xét target $c_2$**:
   - Ngân sách còn lại: $T_{\text{rem}} = 240 - 180 = 60$s.
   - Mức sàn của $c_2$ là 180s $> 60$s $\implies c_2$ không đủ sàn, đưa vào `nonInterviewedTargets` với `omissionReason: "insufficient_tech_pool_for_minimum_envelope"`, `omissionDetail: {"poolRemainingSeconds": 60, "floorRequiredSeconds": 180}`.
3. **Phân bổ thặng dư**:
   - Thặng dư: $R = 240 - 180 = 60$s.
   - Toàn bộ thặng dư cấp cho $c_1 \implies \text{envelope}_1 = 180 + 60 = 240$s.
4. **Expected Output (Tổ hợp 1: 5A Downgrade)**:
   - `targets`: 1 phần tử ($c_1$, `targetArchetype`: "TEXT", `timeEnvelopeSeconds`: 240, `estimatedQuestionsRange`: [1, 2]).
   - `unallocatedBufferSeconds`: 0.
   - `nonInterviewedTargets`: [$c_2$ với lý do thiếu ngân sách].
   *(Lưu ý: Không xảy ra Decision 7 vì target ưu tiên cao $c_1$ đã được chọn thành công sau khi giáng cấp).*

#### Tổ hợp 2: Bắt buộc Hands-on (`strict_hands_on_required == True`) HOẶC Áp dụng Phương án 5B (`coding_fallback_policy == 'omit'`)
1. **Xét target $c_1$**:
   - Ngân sách còn lại: $T_{\text{rem}} = 240\text{s} < 360\text{s}$.
   - Target $c_1$ **tuyệt đối không được giáng cấp**, giữ nguyên sàn 360s. Target $c_1$ không đủ sàn cho coding $\implies$ Đưa $c_1$ vào `nonInterviewedTargets` với `omissionReason: "insufficient_envelope_for_coding_assessment"`.
2. **Kích hoạt kiểm tra Chính sách Ưu tiên (Decision 7)**:
   - **Nhánh 2A (Áp dụng Phương án 7A — Strict Priority Stop, khuyến nghị Formal)**:
     - Vì target ưu tiên đầu bảng $c_1$ bị hụt sàn, hệ thống tuân thủ thứ bậc nghiêm ngặt và **dừng duyệt ngay lập tức**, không nhảy cóc qua $c_1$ để phỏng vấn $c_2$.
     - Kết quả: $K_{\text{eligible}} = 0$ ($E = []$).
     - Ngân sách: $\sum \text{envelope} = 0$, toàn bộ $240$s chuyển thành buffer (`unallocatedBufferSeconds = 240`).
     - **Expected Output (Nhánh 2A: 5B + 7A)**:
       - `targets`: [].
       - `unallocatedBufferSeconds`: 240.
       - `nonInterviewedTargets`:
         + $c_1$: `omissionReason: "insufficient_envelope_for_coding_assessment"`.
         + $c_2$: `omissionReason: "strict_priority_halted_due_to_higher_rank"`.
   - **Nhánh 2B (Áp dụng Phương án 7B — Skip-and-Continue, khuyến nghị Mock)**:
     - Bỏ qua $c_1$, tiếp tục duyệt target kế tiếp $c_2$.
     - Target $c_2$ có mức sàn $180\text{s} \le 240\text{s} \implies c_2$ đủ điều kiện ($K_{\text{eligible}} = 1$).
     - Phân bổ cho $c_2$: Sàn 180s + thặng dư 60s $= 240$s.
     - **Expected Output (Nhánh 2B: 5B + 7B)**:
       - `targets`: 1 phần tử ($c_2$, `targetArchetype`: "TEXT", `timeEnvelopeSeconds`: 240).
       - `unallocatedBufferSeconds`: 0.
       - `nonInterviewedTargets`: [$c_1$ với `omissionReason: "insufficient_envelope_for_coding_assessment"`].

---

### Kịch bản 6 (TC-06): $T_{\text{tech\_pool}} < \text{minimum\_envelope}$ (Không Target Nào Đủ Điều Kiện)
- **Mục tiêu**: Kiểm tra tình huống cực hạn khi ngân sách kỹ thuật quá nhỏ ($T_{\text{tech\_pool}} < \min t_{\text{floor}}$), dẫn đến $K_{\text{eligible}} = 0$ ngay từ đầu.
- **Dữ liệu giả định đầu vào**:
  - Phiên rất ngắn (ví dụ cấu hình thử nghiệm $T_{\text{session}} = 10$ phút = $600$s), ngân sách kỹ thuật còn lại:
    $$T_{\text{tech\_pool}} = 120\text{ giây}$$
  - Mức sàn tối thiểu toàn danh mục JD: $\text{minimum\_envelope} = \min(t_{\text{floor}}) = 180$ giây (TEXT).
  - Có 2 Competencies: $c_1$ (Text, floor 180s), $c_2$ (Text, floor 180s).
- **Các bước tính toán**:
  1. So sánh sơ bộ: $T_{\text{tech\_pool}} = 120\text{s} < \text{minimum\_envelope} = 180\text{s}$.
  2. Số target đủ điều kiện: $K_{\text{eligible}} = 0$.
  3. Xử lý an toàn toán học:
     - Thuật toán nhận diện $E = []$, **ngắt hoàn toàn bước tính thặng dư và phân bổ trọng số**.
     - Không thực hiện phép chia $\frac{w_i}{\sum w_k}$ (tránh lỗi chia cho 0).
  4. Hạch toán ngân sách:
     - Tổng envelope cấp cho chuyên môn = $0$.
     - Toàn bộ $120$s chuyển thành buffer:
       $$\text{unallocated\_buffer\_seconds} = 120$$
  5. Chuyển toàn bộ $100\%$ targets vào `nonInterviewedTargets`:
     - $c_1, c_2$: `omissionReason: "tech_pool_insufficient_for_minimum_envelope"`, `omissionDetail: {"poolAvailableSeconds": 120, "minimumRequiredSeconds": 180}`.
- **Expected Output**:
  - `targets`: [].
  - `techPoolSeconds`: 120.
  - `unallocatedBufferSeconds`: 120.
  - `nonInterviewedTargets`: Chứa cả $c_1$ và $c_2$.
- **Invariants được kiểm chứng**:
  - Invariant 1: Tổng envelope = $0 \le 120$s (Đạt).
  - Invariant 2: Tổng envelope ($0$) + unallocatedBuffer ($120$) == 120s (Đạt tuyệt đối).
  - Invariant 3: Không phát sinh ngoại lệ toán học.

---

### Kịch bản 7 (TC-07): Target `not_applicable` được Tiền Lọc và Truy Vết Toàn Diện (Business Pre-filtering Flow)
- **Mục tiêu**: Kiểm tra luồng nghiệp vụ hợp lệ khi hồ sơ ứng viên có competency được thẩm định là không áp dụng (`not_applicable`): target được tiền lọc, đưa ngay vào `nonInterviewedTargets` với lý do chuẩn hóa, và các target hợp lệ còn lại được phân bổ ngân sách bình thường theo Largest Remainder.
- **Dữ liệu giả định đầu vào**:
  - Phiên 15 phút ($T_{\text{session}} = 900$s), $T_{\text{tech\_pool}} = 400$ giây.
  - Danh sách Requirements từ JD:
    - $req_{\text{na}}$: "Kinh nghiệm làm việc với SOAP/XML Legacy" (Priority: `nice_to_have`). Match status: `not_applicable` ($b_{\text{stat}} = 0.0$). Trỏ đến duy nhất target $c_{\text{na}}$.
    - $req_1$: "Kiến trúc hệ thống phân tán" (Priority: `must_have`, Match status: `unknown` $\implies b_{\text{stat}} = 1.2$). Trỏ đến target $c_1$ (Archetype `TEXT`, sàn 180s, $W_1 = 1.0 \times 1.2 = 1.2$).
    - $req_2$: "Tối ưu hóa cơ sở dữ liệu" (Priority: `must_have`, Match status: `met` $\implies b_{\text{stat}} = 1.0$). Trỏ đến target $c_2$ (Archetype `TEXT`, sàn 180s, $W_2 = 1.0 \times 1.0 = 1.0$).
  - Tập Canonical Competency Targets ban đầu: $C_{\text{all}} = \{c_1, c_2, c_{\text{na}}\}$ ($\text{len}(C_{\text{all}}) = 3$).
- **Các bước tính toán**:
  1. **Thực thi Tiền lọc (Pre-filtering)**:
     - Target $c_{\text{na}}$ có $100\%$ requirements cấu thành mang trạng thái `not_applicable` ($W_{\text{na}} = 0.0$).
     - Target $c_{\text{na}}$ được chuyển trực tiếp vào `nonInterviewedTargets`:
       `omissionReason: "not_applicable_for_candidate"`, `omissionDetail: {"matchStatus": "not_applicable", "weight": 0.0}`.
     - Đồng thời $req_{\text{na}}$ được phản ánh trong `evaluationTargets`:
       `{"requirementId": "req-na", "status": "not_applicable", "evaluationMode": "skipped", "attention": "not_applicable"}`.
  2. **Xếp hạng và Duyệt target hợp lệ còn lại**:
     - Danh sách candidate còn lại: $c_1$ (Rank 0, $W_1 = 1.2$), $c_2$ (Rank 1, $W_2 = 1.0$).
     - Kiểm tra sức chứa: $180 + 180 = 360\text{s} \le 400\text{s} \implies K_{\text{eligible}} = 2$ ($E = [c_1, c_2]$).
  3. **Phân bổ thặng dư theo Hamilton-Hare**:
     - Gán sàn cơ sở: $\text{envelope}_1 = 180$s, $\text{envelope}_2 = 180$s.
     - Thặng dư: $R = 400 - 360 = 40$ giây.
     - Tổng trọng số hợp lệ: $\sum_{k \in E} W_k = 1.2 + 1.0 = 2.2 > 0$.
     - Phần thặng dư lý tưởng:
       * $s_1 = 40 \times \frac{1.2}{2.2} \approx 21.818$s $\implies \lfloor s_1 \rfloor = 21$s, lẻ $0.818$.
       * $s_2 = 40 \times \frac{1.0}{2.2} \approx 18.182$s $\implies \lfloor s_2 \rfloor = 18$s, lẻ $0.182$.
     - Làm tròn phần dư: $\sum \lfloor s \rfloor = 39$s, dư $\Delta = 1$s. Target $c_1$ có phần lẻ lớn hơn nhận $+1$s $\implies \text{bonus}_1 = 22$s, $\text{bonus}_2 = 18$s.
  4. **Envelope cuối cùng**:
     - $\text{envelope}_1 = 180 + 22 = 202$s.
     - $\text{envelope}_2 = 180 + 18 = 198$s.
     - Tổng kiểm tra: $202 + 198 = 400$s $\equiv T_{\text{tech\_pool}}$.
- **Expected Output**:
  - `targets`: 2 phần tử ($c_1: 202$s, $c_2: 198$s).
  - `unallocatedBufferSeconds`: 0.
  - `nonInterviewedTargets`: 1 phần tử ($c_{\text{na}}$ với `omissionReason: "not_applicable_for_candidate"`).
  - `evaluationTargets`: Chứa $req_{\text{na}}$ với `attention: "not_applicable"`.
- **Invariants được kiểm chứng**:
  - Invariant 1 & 2: Tổng envelope đúng bằng 400s.
  - Invariant 4A: Requirement $req_{\text{na}}$ được ghi nhận trong `evaluationTargets`.
  - Invariant 4B: Toàn bộ $C_{\text{all}}$ được phân hoạch hoàn hảo:
    $$\text{len}(C_{\text{all}}) = 3 \equiv \text{len}(\text{targets}) [2] + \text{len}(\text{nonInterviewedTargets}) [1]$$
    Tuyệt đối không có target nào bị biến mất khỏi hệ thống.

---

### Kịch bản 8 (TC-08): Kịch Bản Biên Phòng Vệ — Phân Bổ Đều khi Tổng Trọng Số Bằng 0 (Defensive Equal-Share Allocation Fallback)
- **Mục tiêu**: Kiểm tra cơ chế phòng vệ sâu (defensive circuit-breaker) của thuật toán: Khi dữ liệu bất thường hoặc mock test fixture khiến tất cả candidate targets đủ điều kiện đều có trọng số thô bằng 0 ($\sum_{k \in E} W_k == 0$), hệ thống kích hoạt Equal Share Allocation, chia đều thặng dư và tuyệt đối không phát sinh lỗi `ZeroDivisionError`.
- **Bản chất kỹ thuật**: Đây là **nhánh phòng vệ sâu cho trạng thái dữ liệu bất thường / synthetic test fixture** (ví dụ: upstream validator bị bypass, lỗi cấu hình trọng số khiến $w_{\text{pri}} = 0.0$ ở tất cả requirements, hoặc mock test cố tình tiêm $W_i = 0$). Trong luồng nghiệp vụ hợp lệ thông thường, target `not_applicable` đã được tiền lọc trước (TC-07), nên các candidate target bình thường luôn có $W_i > 0$.
- **Dữ liệu giả định đầu vào**:
  - Phiên 15 phút ($T_{\text{session}} = 900$s), $T_{\text{tech\_pool}} = 400$ giây.
  - Test fixture giả lập đầu vào có 2 candidate targets $c_1, c_2$ dạng `TEXT` ($t_{\text{floor}} = 180$s):
    - Requirement liên kết của $c_1$ và $c_2$ đều có $w_{\text{pri}} = 0.0$ (hoặc mock status khiến tích $w_{\text{pri}} \times b_{\text{stat}} = 0.0$).
    - Trọng số thô: $W_1 = 0.0$ và $W_2 = 0.0$.
- **Các bước tính toán**:
  1. Kiểm tra sức chứa:
     - $c_1$: sàn 180s $\le 400$s (OK).
     - $c_2$: sàn tích lũy $180 + 180 = 360\text{s} \le 400\text{s}$ (OK).
     $\implies K_{\text{eligible}} = 2$ ($E = [c_1, c_2]$).
  2. Gán sàn cơ sở: $\text{envelope}_1 = 180$s, $\text{envelope}_2 = 180$s.
  3. Thặng dư: $R = 400 - 360 = 40$ giây.
  4. **Kiểm tra tổng trọng số**:
     $$\sum_{k \in E} W_k = 0.0 + 0.0 = 0.0$$
  5. **Kích hoạt Cơ chế Fallback Phân bổ Đều (Equal Share Allocation)**:
     - Nhận diện tổng trọng số bằng 0 $\implies$ ngắt hoàn toàn phép chia $\frac{W_i}{\sum W_k}$.
     - Tự động gán tỷ trọng đều:
       $$w_1 = w_2 = \frac{1}{|E|} = \frac{1}{2} = 0.5$$
     - Thặng dư lý tưởng của mỗi target:
       $$s_1 = s_2 = 40 \times 0.5 = 20\text{ giây}$$
  6. Envelope cuối cùng:
     - $\text{envelope}_1 = 180 + 20 = 200$s.
     - $\text{envelope}_2 = 180 + 20 = 200$s.
     - Tổng kiểm tra: $200 + 200 = 400$s $\equiv T_{\text{tech\_pool}}$.
- **Expected Output**:
  - Tuyệt đối không ném ngoại lệ `ZeroDivisionError`.
  - `targets`: 2 phần tử ($c_1: 200$s, $c_2: 200$s).
  - `unallocatedBufferSeconds`: 0.
- **Invariants được kiểm chứng**:
  - Invariant 1 & 2: Tổng đúng bằng 400s.
  - Invariant 3: An toàn toán học tuyệt đối (Zero Division Safety).

---

## 4. TIÊU CHÍ NGHIỆM THU GATE 2 (ACCEPTANCE CRITERIA CHO UNIT TESTS)

Các tiêu chí nghiệm thu dưới đây được thiết kế theo dạng có thể kiểm thử trực tiếp bằng `pytest` khi bước vào giai đoạn lập trình mã nguồn:

1. **AC-P1-UNIT-01 (Strict Budget Non-Exceedance)**:
   - Với mọi đầu vào hợp lệ bất kỳ (`duration_minutes`, cấu hình job, kết quả match), hàm `derive_competency_plan` phải luôn đảm bảo:
     $$\sum_{t \in \text{plan}["targets"]} t["\text{timeEnvelopeSeconds}"] \le \text{plan}["\text{techPoolSeconds}"]$$
2. **AC-P1-UNIT-02 (Exact Conservation when Eligible)**:
   - Khi `len(plan["targets"]) > 0`, tổng Time Envelope của tất cả các target cộng với `unallocatedBufferSeconds` phải bằng **chính xác 100%** giá trị `techPoolSeconds` trên đơn vị giây nguyên (không sai lệch $\pm 1$ giây).
3. **AC-P1-UNIT-03 (Floor Envelope Guarantee)**:
   - Mỗi target được chọn trong `plan["targets"]` phải có `timeEnvelopeSeconds >= 180` (nếu là `TEXT`) hoặc `>= 360` (nếu là `CODING`).
4. **AC-P1-UNIT-04 (Zero Division & Defensive Zero-Weight Fallback Safety)**:
   - Khi `techPoolSeconds < 180`, hàm không ném ra ngoại lệ chia cho 0, trả về `targets = []`, và gán `unallocatedBufferSeconds = techPoolSeconds`.
   - Khi `len(plan["targets"]) > 0` nhưng tổng trọng số của các target đủ điều kiện bằng 0 ($\sum W = 0$ do trạng thái dữ liệu bất thường hoặc test fixture tiêm vào), hàm tự động kích hoạt Equal Share Allocation, chia đều thặng dư cho các target mà không phát sinh lỗi `ZeroDivisionError`.
5. **AC-P1-UNIT-05 (Two-Level Traceability Contract)**:
   - **Tầng Requirement cấp JD**: Mọi requirement trong `job.requirements` phải thỏa mãn:
     $$\text{len}(\text{job.requirements}) \equiv \text{len}(\text{plan}["\text{evaluationTargets}"]) + \text{len}(\text{plan}["\text{nonCompetencyRequirements}"])$$
   - **Tầng Canonical Competency Target**: Mọi competency concept kỹ thuật $C_{\text{all}}$ được đưa vào kế hoạch phải thỏa mãn:
     $$\text{len}(C_{\text{all}}) \equiv \text{len}(\text{plan}["\text{targets}"]) + \text{len}(\text{plan}["\text{nonInterviewedTargets}"])$$
     với $\text{targets} \cap \text{nonInterviewedTargets} = \emptyset$.
   - Mỗi phần tử trong `nonInterviewedTargets` bắt buộc phải có trường `omissionReason` thuộc tập enum quy chuẩn gồm:
     - `"tech_pool_insufficient_for_minimum_envelope"` (thiếu ngân sách sàn cho TEXT/câu hỏi chung)
     - `"insufficient_envelope_for_coding_assessment"` (thiếu ngân sách sàn 360s cho bài tập code)
     - `"strict_priority_halted_due_to_higher_rank"` (dừng duyệt do chính sách ưu tiên 7A)
     - `"not_applicable_for_candidate"` (tiền lọc do $100\%$ requirements cấu thành có status là `not_applicable`)
6. **AC-P1-UNIT-06 (Must-Have Priority & Coding Policy Enforcement)**:
   - Nếu ngân sách chỉ đủ cho một phần các competency, các competency có priority `must_have` bắt buộc phải được ưu tiên xét trước các competency `nice_to_have`.
   - Nếu target coding không đủ mức sàn 360s:
     - Nếu `strict_hands_on_required: true`: Bắt buộc omission target coding, không được hạ cấp sang TEXT.
     - Nếu `strict_hands_on_required: false`: Hành vi tuân thủ chính xác cấu hình `coding_fallback_policy` được tiêm vào (giáng cấp sang TEXT 180s nếu chọn 5A; omission nếu chọn 5B; Gate 2 không triển khai phương án 5C).
7. **AC-P1-UNIT-07 (Modality & Question Bank Decoupling)**:
   - Module `planner.py` không được import bất kỳ model nào từ `src.modules.question_bank` và không chứa trường `question_version_id` hay `rubric_version_id` trong payload kết quả.
8. **AC-P1-UNIT-08 (Configurable Parameter Injection)**:
   - Hàm `derive_competency_plan` cho phép truyền đối tượng cấu hình `policy_config`. Kết quả phân bổ phải phản ánh chính xác các tham số tiêm vào (ví dụ kiểm thử thành công cho cả Mode 1 lượt và Mode 2 lượt của Quyết định 1; cả nhánh 5A và 5B của Quyết định 5 [Gate 2 không triển khai 5C]; cả nhánh 7A và 7B của Quyết định 7).

---

## 5. BẢNG ĐỐI CHIẾU CHÉO (CROSS-REFERENCE MATRIX)

Bảng đối chiếu làm rõ ranh giới giữa hiện trạng code, đề xuất Gate 2, các quyết định còn mở và các khoảng trống telemetry:

| Thành phần / Hành vi | Hiện trạng trong Code (`planner.py` hiện tại) | Đề xuất thay đổi ở Gate 2 | Quyết định Product còn mở | Giả định mô hình chưa có Telemetry |
| :--- | :--- | :--- | :--- | :--- |
| **Đơn vị phân bổ ngân sách** | Phân bổ số lượng câu hỏi cố định theo gói: 15m $\rightarrow$ 2 câu tech; 25m $\rightarrow$ 4 câu tech; 45m $\rightarrow$ 6 câu tech. | **Chuyển thành phân bổ Time Envelope động** theo đơn vị giây nguyên cho từng target dựa trên $T_{\text{tech\_pool}}$. | Đã chốt nguyên tắc: không cố định số câu theo gói. | $t_{\text{expected}}$ của các loại câu hỏi (text 180–240s, code 360–480s) là giả định lý thuyết. |
| **Công thức ngân sách mở đầu** | Chưa có khái niệm $T_{\text{tech\_pool}}$, chỉ mặc định 2 câu mở đầu (Turn 0 Warm-up, Turn 1 CV). | Áp dụng công thức tổng quát: $T_{\text{tech\_pool}} = T_{\text{session}} - (T_{\text{onboarding}} + T_{\text{cv\_standalone\_reserve}} + \dots)$. | **Quyết định 1**: Cần duyệt Mode 1 lượt (1B/1C) hay Mode 2 lượt (1A); duyệt Cách 1 Inclusive hay Cách 2 Decoupled. | Thời lượng trả lời thực tế của ứng viên cho câu chào hỏi và CV (~90–120s) chưa có dữ liệu đo đạc. |
| **Xác định Archetype & Fallback** | Không có archetype; coi mọi câu hỏi kỹ thuật có tải lượng tương đương. | Suy đoán `TEXT` (sàn 180s) vs `CODING` (sàn 360s). Áp dụng cờ `strict_hands_on_required` và `coding_fallback_policy` trước khi xác định eligibility. | **Quyết định 5**: Chính sách khi competency coding nhận envelope $< 360$s (5A Downgrade vs 5B Omission — Chờ Product duyệt; Phương án 5C Target Consolidation nằm ngoài phạm vi Gate 2, thuộc backlog tương lai). | Tỷ lệ thí sinh cần bao nhiêu phút để hoàn thành live coding chưa được hiệu chuẩn qua telemetry. |
| **Xử lý khi thiếu ngân sách sàn** | Chọn cố định `selected = ranked[:budget]`, luôn ép chia đều câu hỏi. | Lọc tuần tự $K_{\text{eligible}}$, gán sàn $t_{\text{floor}}$, phân bổ thặng dư Hamilton-Hare. Nếu $K_{\text{eligible}}=0 \implies$ buffer. Fallback Equal Share khi $\sum W == 0$. | **Quyết định 7**: Dừng duyệt nghiêm ngặt (7A) hay bỏ qua để duyệt tiếp target sau (7B) khi đầu bảng vượt pool — Chờ Product duyệt. | Chưa có dữ liệu thực tế về tỷ lệ phiên bị rơi vào kịch bản $K_{\text{eligible}} = 0$. |
| **Bảo lưu và quyết toán Probe** | Không có khái niệm $T_{\text{probe\_pool}}$ trong P1; runtime pacing chỉ pacing theo tỷ lệ turn. | P1 trừ khoản dự trữ vĩ mô $T_{\text{probe\_pool}} = \text{ratio} \times T_{\text{session}}$ khỏi $T_{\text{tech\_pool}}$. | **Quyết định 6** (`max_runtime_probes`) và **Quyết định 8** (chính sách hoàn lại probe pool) thuộc Gate 4. | Thời lượng 1 lượt probe ($t_{\text{probe\_min}} \approx 60–90$s) và lượt CV follow-up ($90–120$s) là giả định đề xuất mới. |
| **Xử lý Target bị loại bỏ & Truy vết** | Bỏ sót im lặng (chỉ có `nonCompetencyRequirementIds`, không truy vết competency bị thiếu thời gian). | Xuất mảng tường minh `nonInterviewedTargets` kèm `omissionReason` chi tiết (bao gồm cả `"not_applicable_for_candidate"`); phân định rõ 2 tầng truy vết JD Requirement và Competency Target. | Đã thống nhất nguyên tắc bảo vệ `must_have` có truy vết. | Không có. |

---

## 6. KẾT LUẬN & ĐIỀU KIỆN ĐỂ BẮT ĐẦU GATE 2 IMPLEMENTATION

1. **Hiện trạng tài liệu**: Tài liệu này đã đặc tả hoàn chỉnh về mặt kỹ thuật thuật toán phân bổ Time Envelope, công thức $T_{\text{tech\_pool}}$, cơ chế bảo vệ bất biến hệ thống (bao gồm xử lý an toàn chia cho 0 và truy vết hai tầng toàn vẹn), cùng 8 kịch bản kiểm thử thủ công độc lập minh họa đầy đủ các luồng nghiệp vụ và nhánh phòng vệ.
2. **Tình trạng các Quyết định Product**: Mọi phương án chính sách nghiệp vụ (Quyết định 1, Quyết định 5, Quyết định 7) **vẫn được giữ nguyên ở trạng thái đề xuất tham số hóa và đang chờ Product Owner chính thức phê duyệt**, kỹ thuật không tự ý chốt mặc định bất kỳ phương án nào. Riêng Phương án 5C (Target Consolidation) đã được phân định rõ ràng là nằm ngoài phạm vi tái cấu trúc tuần tự của Gate 2 và được chuyển sang backlog tương lai.
3. **Cảnh báo về điều kiện triển khai (Gate 2 Implementation Entry Gate)**:
   - **Chưa đủ điều kiện để tuyên bố Gate 2 sẵn sàng triển khai code** chừng nào các quyết định Product ảnh hưởng trực tiếp đến P1 Planner (đặc biệt là Quyết định 1, 5, 7) vẫn còn mở.
   - Khi Product Owner chính thức chốt các chính sách:
     - Chọn Mode Onboarding và phương án hạch toán ngân sách (Quyết định 1).
     - Chọn hành vi mặc định cho Coding Fallback giữa 5A Downgrade và 5B Omission khi không có cờ hands-on bắt buộc (Quyết định 5; phương án 5C Target Consolidation không triển khai ở Gate 2).
     - Chọn chính sách duyệt ưu tiên (7A Strict Stop hay 7B Skip) theo chế độ phỏng vấn (Quyết định 7).
   - Đội ngũ kỹ thuật mới có căn cứ nghiệp vụ hoàn chỉnh để lập trình `src/modules/interviews/planner.py` và triển khai bộ unit test tương ứng theo các tiêu chí đã định nghĩa tại Mục 4.
