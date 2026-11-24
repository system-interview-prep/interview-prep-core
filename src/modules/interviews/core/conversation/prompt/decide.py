"""Prompt của bước ``decide``: model chọn một hành động trong menu.

Không phải lời nói cho ứng viên nên không dùng ``speech_prompt``. Luật cứng
nằm ở menu (code); ở đây chỉ có luật mềm về nhịp độ (§4.5 plan agent) và
định dạng lý do. Prompt không được chứa từ nào trong ``PROHIBITED_LEAK_PATTERNS``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from src.modules.interviews.core.guardrails.common import candidate_block

__all__ = ["decide_prompt", "decide_input"]


def decide_prompt(is_vi: bool) -> str:
    if is_vi:
        return (
            "# Vai trò\n"
            "Bạn là người phỏng vấn của Intervia, đang quyết định bước kế tiếp sau một câu trả lời của ứng viên.\n\n"
            "# Nhiệm vụ\n"
            "Chọn ĐÚNG MỘT hành động trong các công cụ được đưa ra (menu). Mọi hành động ngoài menu đều không tồn tại.\n"
            "Điền `rationale` một câu và `evidence_refs` trỏ tới căn cứ (answer:sent:<n>, expected_point:<k>, "
            "assessment_criteria:<ý>, pacing:<trường>).\n\n"
            "# Hợp đồng phiên\n"
            "Thực hiện mục tiêu, coverage và stage của hợp đồng đã khóa. Bạn được chọn cách xử lý câu trả lời "
            "trong menu, nhưng không tự đặt stage đích hoặc câu hỏi ngoài queue đã đóng băng.\n"
            "Tự đối chiếu câu trả lời với tiêu chí đánh giá đã cung cấp; không dựa vào phán quyết của decider khác.\n\n"
            "# Luật mềm về nhịp độ (pacing.pace)\n"
            "- behind: ưu tiên move_to_next_question; chỉ acknowledge_and_probe khi ý còn thiếu là ý cốt lõi của câu, "
            "và nói rõ trong rationale.\n"
            "- on_track: thiếu ý cốt lõi thì probe, đủ thì chuyển.\n"
            "- ahead và câu trả lời tốt: được probe sâu một lần (kind=deepen) để dùng thời gian dư cho bằng chứng.\n"
            "- Luôn: còn câu ở các phần sau (questions_remaining_by_stage) thì không kéo dài phần hiện tại bằng probe thứ hai.\n\n"
            "# Không bao giờ\n"
            "- Không nhắc tới đánh giá nội bộ hay xếp hạng trong rationale.\n"
            "- Không bịa chi tiết ứng viên chưa nói."
        )
    return (
        "# Role\n"
        "You are Intervia's interviewer deciding the next step after a candidate's answer.\n\n"
        "# Task\n"
        "Choose EXACTLY ONE of the tools offered (the menu). Anything outside the menu does not exist.\n"
        "Fill `rationale` (one sentence) and `evidence_refs` (answer:sent:<n>, expected_point:<k>, "
        "assessment_criteria:<point>, pacing:<field>).\n\n"
        "# Session contract\n"
        "Follow the frozen contract's objective, coverage target and stages. Choose how to handle the answer "
        "within the menu, but never choose a destination stage or question outside the frozen queue.\n"
        "Assess the answer against the supplied criteria yourself; do not defer to another decider's verdict.\n\n"
        "# Soft pacing rules (pacing.pace)\n"
        "- behind: prefer move_to_next_question; acknowledge_and_probe only when the missing point is core to the "
        "question, and say so in rationale.\n"
        "- on_track: probe a missing core point, otherwise move on.\n"
        "- ahead with a good answer: one deeper follow-up (kind=deepen) is allowed.\n"
        "- Always: with questions left in later stages, do not extend the current one with a second probe.\n\n"
        "# Never\n"
        "- Mention internal assessment or ranking in rationale.\n"
        "- Invent details the candidate did not say."
    )


def decide_input(
    *,
    question: str,
    candidate_text: str,
    stage: str,
    evaluation: Dict[str, Any] | None,
    recent_lines: Sequence[str],
    pacing: Dict[str, Any],
    allowed_actions: List[str],
    assessment_criteria: str = "",
    interview_contract: Dict[str, Any] | None = None,
    memory_hits: Sequence[Dict[str, Any]] = (),
    is_vi: bool = True,
) -> str:
    """The model's private decision input; it is never shown to the candidate."""
    ev = evaluation or {}
    eval_block = (
        {
            "intent": ev.get("intent"),
            "sufficiency": ev.get("sufficiency_status"),
            "missing_aspect": str(ev.get("missing_aspect") or "")[:300],
        }
        if ev
        else "chưa đánh giá" if is_vi else "not evaluated"
    )
    lines = [
        f"Giai đoạn: {stage}" if is_vi else f"Stage: {stage}",
        f"Câu hỏi: {question}" if is_vi else f"Question: {question}",
        (("Tiêu chí đánh giá nội bộ: " if is_vi else "Private assessment criteria: ")
         + f"<assessment_criteria>\n{assessment_criteria[:4000]}\n</assessment_criteria>")
        if assessment_criteria else "",
        ("Câu trả lời của ứng viên:" if is_vi else "Candidate answer:"),
        candidate_block(candidate_text),
        f"Đánh giá: {eval_block}" if is_vi else f"Evaluation: {eval_block}",
        f"Nhịp độ: {pacing}" if is_vi else f"Pacing: {pacing}",
        f"Menu: {', '.join(allowed_actions)}",
    ]
    if interview_contract:
        contract_view = {
            "reference": interview_contract.get("reference"),
            "objective": interview_contract.get("objective"),
            "coverage_target": interview_contract.get("coverage_target"),
            "planned_stages": interview_contract.get("planned_stages"),
        }
        lines.append(("Hợp đồng phiên cần thực hiện: " if is_vi else "Session contract to follow: ")
                     + str(contract_view))
    if recent_lines:
        lines.append(("Các câu bạn vừa nói: " if is_vi else "Your recent lines: ") + " | ".join(list(recent_lines)[-3:]))
    if memory_hits:
        lines.append(("Trí nhớ về ứng viên (chỉ chủ đề): " if is_vi else "Candidate memory (topics only): ")
                     + " | ".join(str(h.get("text", ""))[:160] for h in memory_hits))
    return "\n".join(lines)
