# `conversation/prompt`

Toàn bộ văn bản prompt gửi cho LLM trong các lượt hội thoại phỏng vấn.
Các module ở đây **chỉ dựng chuỗi**: không gọi model, không kiểm tra kết quả,
không quyết định hành động của phiên. Phần đó nằm ở `turn_engine.py` và
`response_generation.py`.

Mục đích: sửa lời prompt mà không phải đọc logic, và ngược lại.

## Bản đồ

| Module | Hàm / hằng | Dùng ở |
| --- | --- | --- |
| `evaluation.py` | `evaluate_answer_prompt(is_vi)` | legacy evaluator cho `rule`/`shadow`; live agent tự quyết định từ câu trả lời + tiêu chí |
| | `WARM_UP_GUIDE_VI` | nối thêm khi `current_stage == WARM_UP` |
| | `SYSTEM_PROBE_EVAL_RULE_VI` | nằm sẵn trong prompt đánh giá |
| `probe.py` | `probe_prompt(is_vi)` | `_generate_probe_question`, lượt thường |
| | `coding_probe_prompt(...)` | `_generate_probe_question`, lượt code |
| `clarify.py` | `clarify_prompt(is_vi)` | `_generate_clarify_text` |
| `interviewer_line.py` | `interviewer_line_prompt(...)` | `_generate_interviewer_line` |
| `transition.py` | `transition_context(...)` | Context builder độc lập; luồng hỏi hiện tại phát nguyên câu hỏi đã đóng băng, không ghép thêm lời chuyển |
| `closing_qna.py` | `closing_qna_speech_prompt(...)` | `turn_engine._answer_closing_question` |
| `decide.py` | `decide_prompt(is_vi)`, `decide_input(...)` | `agent/decide.py`, model decider (shadow/live) |
| `candidate_memory.py` | `block(items, is_vi, competency)` | evaluate/probe/clarify, sau `playbook.block` |
| | `extract_instructions(is_vi)` | `application/runtime/candidate_memory.py`, chỉ dẫn cho memory manager của `langmem` ở lệnh `score` |

## Quy ước

- Mỗi hàm nhận `is_vi` và các biến ngữ cảnh đã tính sẵn, trả về một chuỗi.
- Các prompt dùng thống nhất `speech_prompt(...)`; không còn cờ môi trường để
  chuyển giữa hai bộ prompt.
- Những phần nối chung cho mọi prompt — `playbook.block(...)` và
  `candidate_data_rule(is_vi)` — vẫn do nơi gọi nối vào, vì chúng là chính
  sách của phiên chứ không thuộc riêng prompt nào.
- Văn phong chung (`style_guide`) và bố cục Vai trò / Giọng văn / Nhiệm vụ /
  Luật bắt buộc / Câu mẫu / Đầu ra đến từ `core.dialogue.policy.speech_prompt`,
  không định nghĩa lại ở đây.
