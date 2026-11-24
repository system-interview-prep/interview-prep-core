"""Trí nhớ về ứng viên trong prompt, và chỉ dẫn trích xuất trí nhớ sau phiên.

Khối ``block`` chỉ gọi tên chủ đề ("còn hở phần invalidation"), không bao giờ
trích lời ứng viên; trống thì prompt y như cũ (giống ``playbook.block``).
``extract_instructions`` là phần chỉ dẫn mà memory manager của ``langmem``
(``application/runtime/candidate_memory.py``) đưa cho model ở lệnh ``score``
— đầu vào là kết luận của grader, không phải câu trả lời. Thao tác (thêm /
vá / xoá) và schema do ``langmem`` + ``trustcall`` quy định, không phải ở đây.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

__all__ = ["MAX_ITEMS", "MAX_BLOCK_CHARS", "block", "extract_instructions"]

MAX_ITEMS = 6
MAX_BLOCK_CHARS = 700

KIND_LABEL_VI = {"gap": "còn hở", "strength": "đã nắm", "project_fact": "dự án", "preference": "lưu ý"}
KIND_LABEL_EN = {"gap": "gap", "strength": "solid", "project_fact": "project", "preference": "note"}


def block(items: Iterable[Dict[str, Any]], is_vi: bool = True, competency: str | None = None) -> str:
    """Prompt text for the memory hits of this turn; "" when nothing applies."""
    chosen = [i for i in items if str(i.get("text") or "").strip()]
    if competency:
        key = str(competency).strip().lower()
        preferred = [i for i in chosen if str(i.get("skill_key") or "").lower() == key]
        chosen = preferred + [i for i in chosen if i not in preferred]
    chosen = chosen[:MAX_ITEMS]
    if not chosen:
        return ""
    head = (
        "Điều đã biết về ứng viên từ các phiên trước (chỉ dùng để chọn hỏi gì; "
        "không nhắc 'lần trước bạn nói', không trích lời họ):"
        if is_vi
        else "Known about this candidate from earlier sessions (to choose what to ask; "
        "never say 'last time you said', never quote them):"
    )
    labels = KIND_LABEL_VI if is_vi else KIND_LABEL_EN
    lines, size = [], len(head)
    for item in chosen:
        label = labels.get(str(item.get("kind") or ""), str(item.get("kind") or ""))
        line = f"- [{label}] {str(item.get('text')).strip()}"
        if size + len(line) > MAX_BLOCK_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    if not lines:
        return ""
    return "\n\n" + head + "\n" + "\n".join(lines)


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
