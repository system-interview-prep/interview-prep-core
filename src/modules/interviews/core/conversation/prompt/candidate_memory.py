"""Trí nhớ về ứng viên trong prompt, và prompt trích xuất trí nhớ sau phiên.

Khối ``block`` chỉ gọi tên chủ đề ("còn hở phần invalidation"), không bao giờ
trích lời ứng viên; trống thì prompt y như cũ (giống ``playbook.block``).
``extract_prompt`` + ``MEMORY_OPS_SCHEMA`` dùng cho một lần gọi
``generate_text(json_schema=...)`` ở lệnh ``score`` — đầu vào là kết luận của
grader, không phải câu trả lời.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

__all__ = ["MAX_ITEMS", "MAX_BLOCK_CHARS", "MEMORY_OPS_SCHEMA", "block", "extract_prompt"]

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


MEMORY_OPS_SCHEMA: Dict[str, Any] = {
    "name": "candidate_memory_ops",
    "schema": {
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["ADD", "UPDATE", "RETIRE"]},
                        "key": {"type": "string"},
                        "kind": {"type": "string", "enum": ["gap", "strength", "project_fact", "preference"]},
                        "skill_key": {"type": "string"},
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["op", "key", "kind", "skill_key", "text", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["ops"],
        "additionalProperties": False,
    },
}


def extract_prompt(is_vi: bool = True) -> str:
    if is_vi:
        return (
            "Bạn cập nhật sổ ghi nhớ về một ứng viên sau một buổi phỏng vấn luyện tập, từ KẾT LUẬN của "
            "người chấm (không có nguyên văn câu trả lời).\n"
            "Mỗi mục ghi nhớ là MỘT câu ngắn (≤ 160 ký tự) gọi tên chủ đề: ý còn hở, ý đã nắm, dự án đã kể, "
            "lưu ý khi hỏi. Không ghi điểm số, không ghi nhận xét tính cách, không ghi thông tin cá nhân "
            "(tên, email, công ty cũ, lương).\n"
            "Đề xuất tối đa 5 thao tác: ADD (key để trống), UPDATE (key của mục đang có), RETIRE (key của mục "
            "không còn đúng). Mục trùng chủ đề với mục đang có thì UPDATE, không ADD.\n"
            "confidence 0–1 theo mức chắc chắn của người chấm. Trả về đúng JSON theo schema."
        )
    return (
        "You update the memory notebook about one candidate after a practice interview, from the GRADER'S "
        "conclusions (no verbatim answers).\n"
        "Each memory is ONE short sentence (≤ 160 chars) naming a topic: a gap, a solid point, a project "
        "mentioned, a note for asking. No scores, no personality remarks, no personal data (names, email, "
        "former employers, salary).\n"
        "Propose at most 5 operations: ADD (empty key), UPDATE (key of an existing item), RETIRE (key of an "
        "item no longer true). Same topic as an existing item → UPDATE, not ADD.\n"
        "confidence 0–1 from the grader's certainty. Return exactly the JSON of the schema."
    )
