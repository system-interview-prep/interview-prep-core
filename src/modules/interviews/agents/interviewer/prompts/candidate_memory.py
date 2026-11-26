"""Trí nhớ về ứng viên trong prompt của người phỏng vấn.

Khối ``block`` chỉ gọi tên chủ đề ("còn hở phần invalidation"), không bao giờ
trích lời ứng viên; trống thì prompt y như cũ (giống ``playbook.block``).
Chỉ dẫn trích xuất sau phiên thuộc về ``memory/prompts.py``, không phải ở đây.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

__all__ = ["MAX_BLOCK_CHARS", "MAX_ITEMS", "block"]

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
