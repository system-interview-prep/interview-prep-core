"""Sổ tay của người phỏng vấn: những bài học từ các cuộc phỏng vấn trước đã được một người phê duyệt.

"AI tích luỹ skill phỏng vấn". Được xây dựng theo ACE (Agentic Context Engineering): các bài học ngắn,
được liệt kê và có id, chỉnh sửa từng bài một, không bao giờ viết lại toàn bộ ngữ cảnh.

Mô-đun này chỉ lưu các mục ĐANG HOẠT ĐỘNG trong bộ nhớ và đưa chúng vào
prompt của các bước trong engine dùng để đọc câu trả lời của ứng viên, viết câu hỏi
tiếp theo hoặc giải thích lại một câu hỏi. Tầng ứng dụng tải chúng từ cơ sở dữ liệu và
đề xuất các mục mới; chỉ những mục được một người phê duyệt mới được đưa vào prompt,
và khối này luôn nằm bên dưới các quy tắc cố định, vốn được ưu tiên khi có xung đột.

Sổ tay trống = không có khối nội dung = các prompt hoàn toàn giống như trước đây.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

STEPS = ("evaluate_answer", "probe", "clarify")
ALL_STEPS = "all"
MAX_ENTRIES_PER_PROMPT = 8
MAX_BLOCK_CHARS = 1200

_entries: list[dict[str, Any]] = []
_version = ""


def load(entries: Iterable[dict[str, Any]]) -> None:
    """Replace the in-memory playbook (ACTIVE entries: id, step, text, skill)."""
    global _entries, _version
    _entries = [
        {
            "id": str(e["id"]),
            "step": str(e.get("step") or ALL_STEPS),
            "text": str(e["text"]).strip(),
            "skill": e.get("skill"),
        }
        for e in entries
        if str(e.get("text") or "").strip()
    ]
    joined = "|".join(f"{e['id']}:{e['text']}" for e in sorted(_entries, key=lambda e: e["id"]))
    digest = hashlib.sha1(joined.encode())
    _version = digest.hexdigest()[:8] if _entries else ""


def version() -> str:
    """Short hash of the active playbook ("" when empty); stored on every traced turn."""
    return _version


def ids() -> list[str]:
    return [e["id"] for e in _entries]


def entries_for(step: str, skill: str | None = None) -> list[dict[str, Any]]:
    chosen = [
        e
        for e in _entries
        if e["step"] in (step, ALL_STEPS)
        and (not e.get("skill") or (skill and str(e["skill"]).strip().lower() == str(skill).strip().lower()))
    ]
    return chosen[:MAX_ENTRIES_PER_PROMPT]


def block(step: str, is_vi: bool = True, skill: str | None = None) -> str:
    """Prompt text for ``step``; "" when nothing applies."""
    chosen = entries_for(step, skill)
    if not chosen:
        return ""
    head = (
        "Kinh nghiệm phỏng vấn đã được người duyệt "
        "(áp dụng khi phù hợp; mọi quy tắc ở trên luôn được ưu tiên):"
        if is_vi
        else "Interviewing lessons approved by a reviewer "
        "(apply when relevant; every rule above takes precedence):"
    )
    lines, size = [], len(head)
    for entry in chosen:
        line = f"- {entry['text']}"
        if size + len(line) > MAX_BLOCK_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n\n" + head + "\n" + "\n".join(lines)
