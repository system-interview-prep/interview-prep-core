"""In toàn bộ kịch bản nói của người phỏng vấn AI ra một file để đọc và duyệt.

Không gọi AI, không cần database: script đọc thẳng các câu soạn sẵn trong code và
lấy đúng các hướng dẫn (prompt) mà engine gửi cho AI ở mỗi tình huống, bằng một AI
giả chỉ ghi lại prompt. Vì đọc từ code đang chạy nên file luôn khớp với code.

Chạy trong thư mục interview-prep-core:

    docker compose exec backend python scripts/dump_interviewer_script.py

Kết quả: trace-logs/interviewer_script.md (ghi đè mỗi lần chạy).

Lời nói nằm ở đâu trong code:
- core/interviewer_script.py     MỌI câu soạn sẵn, một kịch bản chung cho chat, voice và video
                                 (kể cả câu chỉ có ở kênh nói; voice agent đọc file này theo đường dẫn)
- core/interviewer_voice.py      văn phong chung cho prompt, cách chọn câu, câu chuyển
- core/interview_engine.py       prompt cho AI (ghi nhận, hỏi thêm, giải thích lại)
- core/opening_script.py         ghép lời mở đầu từ các câu trong kịch bản
- planning/spoken_questions.py   prompt viết bản nói của câu hỏi trong kho
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # chạy được cả khi không đặt PYTHONPATH

from src.modules.interviews.adapters.voice import answer_window
from src.modules.interviews.core import interview_engine as engine_module
from src.modules.interviews.core import interviewer_script as script
from src.modules.interviews.core import interviewer_voice as voice
from src.modules.interviews.core.interview_engine import InterviewCoreEngine, candidate_question_response
from src.modules.interviews.core.interview_types import InterviewStage
from src.modules.interviews.core.opening_script import build_opening, warmup_question
from src.modules.interviews.planning.spoken_questions import _rewrite_prompt

QUESTION = "Trong dự án quản lý đơn hàng, bạn trực tiếp làm phần nào?"

# (tên nhóm, khi nào dùng, file)
POOLS: list[tuple[str, str, str]] = [
    ("ACK_FALLBACK_VI", "Câu ghi nhận dự phòng khi câu AI viết bị rào chắn loại", "interviewer_script.py"),
    ("INTRO_ACK_VI", "Câu ghi nhận dự phòng sau lời tự giới thiệu", "interviewer_script.py"),
    ("NON_ANSWER_ACK_VI", "Lượt không phải câu trả lời: lời chèn lệnh, hỏi chuyện khác", "interviewer_script.py"),
    ("GIVE_UP_ACK_VI", "Ứng viên nói không biết", "interviewer_script.py"),
    ("SKIP_ACK_VI", "Ứng viên xin qua câu", "interviewer_script.py"),
    ("PROBE_FALLBACK_VI", "Câu hỏi thêm dự phòng khi AI không viết được", "interviewer_script.py"),
    ("REPEAT_PREFIX_VI", "Mở đầu khi nhắc lại câu hỏi", "interviewer_script.py"),
    ("CLOSE_COMPLETED_VI", "Kết thúc: đã hỏi đủ các phần", "interviewer_script.py"),
    ("TIMEOUT_VI", "Kết thúc: hết giờ", "interviewer_script.py"),
    ("EARLY_STOP_VI", "Kết thúc: dừng sớm", "interviewer_script.py"),
    ("WRAP_TIMEOUT_PARTIAL_VI", "Kết thúc: hết giờ khi chưa đi hết kế hoạch", "interviewer_script.py"),
    ("WRAP_ENDED_EARLY_PARTIAL_VI", "Kết thúc: dừng sớm khi chưa đi hết kế hoạch", "interviewer_script.py"),
    ("USER_ENDED_VI", "Kết thúc: ứng viên bấm dừng", "interviewer_script.py"),
    ("FAREWELL_VI", "Chào tạm biệt", "interviewer_script.py"),
    ("LAST_QNA_CLOSING_VI", "Kết thúc sau phần hỏi đáp cuối buổi", "interviewer_script.py"),
]


class PromptRecorder:
    """AI giả: ghi lại prompt engine gửi, trả về kết quả rỗng."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_json(self, system_prompt: str, user_content: str) -> dict[str, Any]:
        self.calls.append((system_prompt, user_content))
        return {"intent": "ANSWER", "sufficiency_status": "SUFFICIENT", "acknowledgement": "", "score": 6}

    async def generate_text(self, system_prompt: str, user_content: str) -> str:
        self.calls.append((system_prompt, user_content))
        return ""


def _block(text: str) -> list[str]:
    return ["```text", text.strip(), "```", ""]


def _pool(lines: tuple[str, ...]) -> list[str]:
    return [f"- {line}" for line in lines] + [""]


async def _prompts() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    question = {"main_prompt": QUESTION, "rubric_criteria": "Phần việc cụ thể; quyết định kỹ thuật"}
    for name, call in (
        (
            "Đánh giá câu trả lời + viết câu ghi nhận (mỗi lượt trả lời)",
            lambda e: e._evaluate_candidate_response(
                candidate_text="Em làm phần thanh toán, tích hợp VNPay.",
                current_stage=InterviewStage.VALIDATE,
                current_question=question,
                seed="dump:1",
            ),
        ),
        (
            "Viết câu hỏi thêm (khi câu trả lời chưa đủ)",
            lambda e: e._generate_probe_question(
                candidate_text="Em làm phần thanh toán.",
                current_question=question,
                missing_aspect="quyết định kỹ thuật khó nhất",
                seed="dump:2",
            ),
        ),
        (
            "Giải thích lại câu hỏi (ứng viên chưa hiểu)",
            lambda e: e._generate_clarify_text(current_question=question, candidate_text="ý anh là sao, em chưa hiểu"),
        ),
    ):
        recorder = PromptRecorder()
        try:
            await call(InterviewCoreEngine(llm_client=recorder))
        except Exception as exc:  # noqa: BLE001 - a missing prompt is reported, not fatal
            out[name] = (f"(không lấy được prompt: {exc})", "")
            continue
        if recorder.calls:
            out[name] = recorder.calls[0]
    return out


def build() -> str:
    lines: list[str] = [
        "# Kịch bản nói của người phỏng vấn AI",
        "",
        f"Tạo lúc {time.strftime('%Y-%m-%d %H:%M')} bằng `scripts/dump_interviewer_script.py`, đọc thẳng từ code.",
        "Câu soạn sẵn: mỗi nhóm có nhiều câu, mỗi lượt chọn một câu theo mã buổi và tránh câu vừa nói.",
        "Câu AI tự viết: đi qua rào chắn; trượt thì dùng câu soạn sẵn tương ứng.",
        "",
        "Mọi câu dưới đây giống nhau ở chat, voice và video, trừ mục 7 (chỉ kênh nói mới cần).",
        "Voice agent đọc lên sau khi làm sạch: bỏ dấu “ ”, đổi \" & \" thành \"và\", đọc số thành chữ.",
        "",
        "## 1. Mở đầu buổi",
        "",
        *_block(
            build_opening(
                is_vi=True, mode="chat", duration_minutes=25, job_title="Backend Developer", call_name="Hiền",
                planned_stages=["WARM_UP", "VALIDATE", "DEEP_DIVE", "CHALLENGE", "BEHAVIORAL"],
                first_question=warmup_question(is_vi=True, is_demo=False),
            )
        ),
        "Demo 3 phút:",
        "",
        *_block(
            build_opening(
                is_vi=True, mode="chat", duration_minutes=3, job_title="Backend Developer", call_name="Hiền",
                planned_stages=["WARM_UP", "DEEP_DIVE"],
                first_question=warmup_question(is_vi=True, is_demo=True),
            )
        ),
        "## 2. Câu soạn sẵn theo tình huống",
        "",
    ]
    for name, when, where in POOLS:
        lines += [f"### {when}", "", f"`{where}` → `{name}`", "", *_pool(getattr(script, name))]

    lines += ["## 3. Câu chuyển chủ đề và giai đoạn", "", "`interviewer_script.py` → `TRANSITION_STAGE_VI`, `TRANSITION_SKILL_VI`", ""]
    for stage, pool in script.TRANSITION_STAGE_VI.items():
        lines.append(f"**Sang giai đoạn {stage}:**")
        lines += _pool(pool)
    lines.append("**Đổi kỹ năng trong cùng giai đoạn** ({next} là tên kỹ năng):")
    lines += _pool(script.TRANSITION_SKILL_VI)
    lines += [
        "Sau lượt ứng viên nói không biết hoặc xin qua câu, câu có chữ \"khó hơn\" bị bỏ.",
        "",
        "## 4. Xin dừng",
        "",
        "`interviewer_script.py` → `ABORT_CONFIRMATION_VI` (tên nút khớp hộp xác nhận của cả hai phòng):",
        "",
        *_block(script.ABORT_CONFIRMATION_VI),
        "## 5. Khi ứng viên hỏi ngược hoặc đáp lạc đề",
        "",
        "`interviewer_script.py` → `QUESTION_RESPONSES_VI`, rồi `QUESTION_RETURN_VI` nhắc lại câu hỏi:",
        "",
    ]
    for intent in ("SCORING", "PROCESS", "ROLE_QA", "ABOUT_INTERVIEWER", "ANSWER_REQUEST", "OFF_TOPIC", "OFF_TOPIC_REPLY"):
        reply = candidate_question_response(intent, current_question=QUESTION, is_vi=True, duration_minutes=25)
        lines += [f"**{intent}**", "", *_block(reply)]
    lines += [
        "## 6. Câu dự phòng khác",
        "",
        "Giải thích lại câu hỏi khi câu AI viết bị loại (`CLARIFY_FALLBACK_VI`):",
        "",
        *_block(engine_module.clarify_fallback_text(QUESTION, is_vi=True)),
        "Câu hỏi thêm cho bài code (`CODING_FALLBACK_PROBE_VI`):",
        "",
        *_block(script.CODING_FALLBACK_PROBE_VI),
        "## 7. Chỉ có ở giọng nói",
        "",
        "`interviewer_script.py`, mục cuối; voice agent dùng qua `adapters/voice/answer_window.py`",
        "",
        "**Câu đệm lúc chờ AI xử lý** (`PROCESSING_FILLERS_VI`):",
        "",
        *_pool(answer_window.PROCESSING_FILLERS_VI),
        "**Chưa nghe rõ** (`NOT_HEARD_VI`):",
        "",
        *_pool(answer_window.NOT_HEARD_VI),
        "**Nhắc khi ứng viên im lặng** (`silence_reprompt`):",
        "",
        *_block(answer_window.silence_reprompt("vi-VN")),
        "**Hướng dẫn lượt nói** (`turn_taking_tip`):",
        "",
        *_block(answer_window.turn_taking_tip("vi-VN")),
        "**Nghe được nhưng quá nhiễu** (`UNCLEAR_TRANSCRIPT_VI`):",
        "",
        *_block(script.UNCLEAR_TRANSCRIPT_VI),
        "**Lỗi khi xử lý câu trả lời** (`CORE_ERROR_VI`):",
        "",
        *_block(script.CORE_ERROR_VI),
        "**Ứng viên chen “ừm”, xin chờ** (`BACKCHANNEL_CONTINUE_VI`, `HOLD_ACK_VI`):",
        "",
        *_pool((script.BACKCHANNEL_CONTINUE_VI, script.HOLD_ACK_VI)),
        "## 8. Hướng dẫn cho AI (prompt)",
        "",
        "Văn phong chung (`interviewer_voice.py` → `INTERVIEWER_STYLE_VI`), gắn vào mọi prompt sinh lời nói:",
        "",
        *_block(voice.INTERVIEWER_STYLE_VI),
        "Ví dụ câu ghi nhận đưa cho AI (`ACK_EXAMPLES_VI`, `ACK_BAD_EXAMPLES_VI`):",
        "",
        *_block(voice.ack_examples_block(True)),
    ]
    for name, (system_prompt, user_content) in asyncio.run(_prompts()).items():
        lines += [f"### {name}", "", "Hướng dẫn hệ thống:", "", *_block(system_prompt)]
        if user_content:
            lines += ["Nội dung gửi kèm (ví dụ):", "", *_block(user_content)]
    lines += [
        "### Viết bản nói của câu hỏi trong kho (lúc khoá câu hỏi)",
        "",
        "`planning/spoken_questions.py` → `_rewrite_prompt`",
        "",
        *_block(_rewrite_prompt(True)),
    ]
    return "\n".join(lines)


def main() -> None:
    out = Path("trace-logs")
    out.mkdir(exist_ok=True)
    path = out / "interviewer_script.md"
    path.write_text(build(), encoding="utf-8")
    print(f"Đã ghi {path}")


if __name__ == "__main__":
    main()
