"""In toàn bộ kịch bản nói của người phỏng vấn AI ra một file để đọc và duyệt.

Không gọi AI, không cần database: script đọc thẳng các câu soạn sẵn trong code và
lấy đúng các hướng dẫn (prompt) mà engine gửi cho AI ở mỗi tình huống, bằng một AI
giả chỉ ghi lại prompt. Vì đọc từ code đang chạy nên file luôn khớp với code.

Chạy trong thư mục interview-prep-core:

    docker compose exec backend python scripts/dump_interviewer_script.py

Kết quả: trace-logs/interviewer_script.md (ghi đè mỗi lần chạy).

Lời nói nằm ở đâu trong code:
- core/interviewer_voice.py      văn phong chung, các nhóm câu soạn sẵn, câu chuyển, câu xin dừng
- core/interview_engine.py       prompt cho AI (ghi nhận, hỏi thêm, giải thích lại), câu trả lời
                                 cho câu hỏi ngoài lề, câu giải thích dự phòng
- core/opening_script.py         lời chào và câu khởi động
- planning/spoken_questions.py   prompt viết bản nói của câu hỏi trong kho
- adapters/voice/answer_window.py  câu chỉ có ở giọng nói (câu đệm, chưa nghe rõ, nhắc khi im lặng)
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
from src.modules.interviews.core import interviewer_voice as voice
from src.modules.interviews.core.interview_engine import InterviewCoreEngine, candidate_question_response
from src.modules.interviews.core.interview_types import InterviewStage
from src.modules.interviews.core.opening_script import build_opening, warmup_question
from src.modules.interviews.planning.spoken_questions import _rewrite_prompt

QUESTION = "Trong dự án quản lý đơn hàng, bạn trực tiếp làm phần nào?"

# (tên nhóm, khi nào dùng, file)
POOLS: list[tuple[str, str, str]] = [
    ("ACK_FALLBACK_VI", "Câu ghi nhận dự phòng khi câu AI viết bị rào chắn loại", "interviewer_voice.py"),
    ("INTRO_ACK_VI", "Câu ghi nhận dự phòng sau lời tự giới thiệu", "interviewer_voice.py"),
    ("NON_ANSWER_ACK_VI", "Lượt không phải câu trả lời: lời chèn lệnh, hỏi chuyện khác", "interviewer_voice.py"),
    ("GIVE_UP_ACK_VI", "Ứng viên nói không biết", "interviewer_voice.py"),
    ("SKIP_ACK_VI", "Ứng viên xin qua câu", "interviewer_voice.py"),
    ("PROBE_FALLBACK_VI", "Câu hỏi thêm dự phòng khi AI không viết được", "interviewer_voice.py"),
    ("REPEAT_PREFIX_VI", "Mở đầu khi nhắc lại câu hỏi", "interviewer_voice.py"),
    ("CLOSE_COMPLETED_VI", "Kết thúc: đã hỏi đủ các phần", "interviewer_voice.py"),
    ("TIMEOUT_VI", "Kết thúc: hết giờ", "interviewer_voice.py"),
    ("EARLY_STOP_VI", "Kết thúc: dừng sớm", "interviewer_voice.py"),
    ("WRAP_TIMEOUT_PARTIAL_VI", "Kết thúc: hết giờ khi chưa đi hết kế hoạch", "interviewer_voice.py"),
    ("WRAP_ENDED_EARLY_PARTIAL_VI", "Kết thúc: dừng sớm khi chưa đi hết kế hoạch", "interviewer_voice.py"),
    ("USER_ENDED_VI", "Kết thúc: ứng viên bấm dừng", "interviewer_voice.py"),
    ("FAREWELL_VI", "Chào tạm biệt", "interviewer_voice.py"),
    ("LAST_QNA_CLOSING_VI", "Kết thúc sau phần hỏi đáp cuối buổi", "interviewer_voice.py"),
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
        "## 1. Mở đầu buổi",
        "",
        "Giọng nói (voice):",
        "",
        *_block(
            build_opening(
                is_vi=True, mode="voice", duration_minutes=25, job_title="Backend Developer", call_name="Hiền",
                planned_stages=["WARM_UP", "VALIDATE", "DEEP_DIVE", "CHALLENGE", "BEHAVIORAL"],
                first_question=warmup_question(is_vi=True, is_demo=False),
            )
        ),
        "Chat:",
        "",
        *_block(
            build_opening(
                is_vi=True, mode="chat", duration_minutes=25, job_title="Backend Developer", call_name="Hiền",
                planned_stages=["WARM_UP", "VALIDATE", "DEEP_DIVE", "CHALLENGE", "BEHAVIORAL"],
                first_question=warmup_question(is_vi=True, is_demo=False),
            )
        ),
        "Demo 3 phút: " + warmup_question(is_vi=True, is_demo=True),
        "",
        "## 2. Câu soạn sẵn theo tình huống",
        "",
    ]
    for name, when, where in POOLS:
        lines += [f"### {when}", "", f"`{where}` → `{name}`", "", *_pool(getattr(voice, name))]

    lines += ["## 3. Câu chuyển chủ đề và giai đoạn", "", "`interviewer_voice.py` → `_TRANSITION_STAGE_VI`, `_TRANSITION_SKILL_VI`", ""]
    for stage, pool in voice._TRANSITION_STAGE_VI.items():
        lines.append(f"**Sang giai đoạn {stage}:**")
        lines += _pool(pool)
    lines.append("**Đổi kỹ năng trong cùng giai đoạn** ({next} là tên kỹ năng):")
    lines += _pool(voice._TRANSITION_SKILL_VI)
    lines += [
        "Sau lượt ứng viên nói không biết hoặc xin qua câu, câu có chữ \"khó hơn\" bị bỏ.",
        "",
        "## 4. Xin dừng",
        "",
        "Chat (nêu đúng tên nút):",
        "",
        *_block(voice.abort_confirmation(True, spoken=False)),
        "Giọng nói:",
        "",
        *_block(voice.abort_confirmation(True, spoken=True)),
        "## 5. Khi ứng viên hỏi ngược hoặc đáp lạc đề",
        "",
        "`interview_engine.py` → `candidate_question_response` (bản giọng nói, có nhắc lại câu hỏi):",
        "",
    ]
    for intent in ("SCORING", "PROCESS", "ROLE_QA", "ABOUT_INTERVIEWER", "ANSWER_REQUEST", "OFF_TOPIC", "OFF_TOPIC_REPLY"):
        reply = candidate_question_response(intent, current_question=QUESTION, is_vi=True, duration_minutes=25)
        lines += [f"**{intent}**", "", *_block(reply)]
    lines += [
        "## 6. Câu dự phòng khác",
        "",
        "Giải thích lại câu hỏi khi câu AI viết bị loại (`clarify_fallback_text`):",
        "",
        *_block(engine_module.clarify_fallback_text(QUESTION, is_vi=True)),
        "Câu hỏi thêm cho bài code (`CODING_FALLBACK_PROBE_VI`):",
        "",
        *_block(engine_module.CODING_FALLBACK_PROBE_VI),
        "## 7. Chỉ có ở giọng nói",
        "",
        "`adapters/voice/answer_window.py`",
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
