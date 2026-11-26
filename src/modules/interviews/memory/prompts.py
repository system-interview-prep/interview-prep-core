"""What the ``langmem`` memory manager is told at ``score``.

Owned by memory, not by the interviewer: the input is the grader's
conclusions, not the candidate's answers, and the operations (insert,
patch, remove) and schema come from ``langmem`` + ``trustcall``.

It lived under the interviewer's prompts until the V2 brief §2 split; a
memory module importing an agent is the dependency §10 forbids
("memory không điều khiển agent").
"""

from __future__ import annotations

__all__ = ["MAX_ITEMS", "extract_instructions"]

MAX_ITEMS = 6


def extract_instructions(is_vi: bool = True) -> str:
    """What the memory manager is told; it already lists the existing items with their ids."""
    if is_vi:
        return (
            "Bạn cập nhật sổ ghi nhớ về MỘT ứng viên sau một buổi phỏng vấn luyện tập, từ KẾT LUẬN của "
            "người chấm (không có nguyên văn câu trả lời).\n"
            "Mỗi mục ghi nhớ là một CandidateMemory: MỘT câu ngắn (≤ 160 ký tự) gọi tên chủ đề — ý còn hở "
            "(gap), ý đã nắm (strength), dự án đã kể (project_fact), lưu ý khi hỏi (preference). Viết bằng "
            "tiếng Việt. Không ghi điểm số, không nhận xét tính cách, không ghi thông tin cá nhân (tên, email, "
            "công ty cũ, lương), không trích lời ứng viên.\n"
            "Mục trùng chủ đề với mục đang có thì VÁ mục đó (đổi kind/text/confidence), không thêm mới. "
            "Mục không còn đúng thì XOÁ. Chỉ giữ các trường kind, skill_key, text, confidence; các trường "
            "khác của mục đang có để nguyên.\n"
            "Tối đa 5 thay đổi cho cả buổi; không có gì mới thì không làm gì. confidence 0–1 theo mức "
            "chắc chắn của người chấm."
        )
    return (
        "You update the memory notebook about ONE candidate after a practice interview, from the GRADER'S "
        "conclusions (no verbatim answers).\n"
        "Each memory is a CandidateMemory: ONE short sentence (≤ 160 chars) naming a topic — a gap, a solid "
        "point (strength), a project mentioned (project_fact), a note for asking (preference). No scores, no "
        "personality remarks, no personal data (names, email, former employers, salary), never quote the "
        "candidate.\n"
        "Same topic as an existing item → PATCH that item (kind/text/confidence), do not insert. An item no "
        "longer true → REMOVE it. Only set kind, skill_key, text, confidence; leave the other fields of an "
        "existing item as they are.\n"
        "At most 5 changes per session; nothing new → do nothing. confidence 0–1 from the grader's certainty."
    )
