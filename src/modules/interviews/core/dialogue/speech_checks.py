"""
Module này cung cấp các chức năng để ghi nhận và quản lý các kiểm tra giọng nói trong quá trình phỏng vấn.
Các kiểm tra giọng nói có thể bao gồm các loại như:
- ack: xác nhận rằng ứng viên đã nghe và hiểu câu hỏi,
- probe: yêu cầu ứng viên cung cấp thêm thông tin hoặc làm rõ câu trả lời,
- clarify: yêu cầu ứng viên làm rõ câu trả lời,
- qna: kiểm tra kiến thức hoặc kỹ năng của ứng viên thông qua các câu hỏi
- compose: yêu cầu ứng viên tạo ra một câu trả lời hoặc giải pháp mới.
Các kết quả của các kiểm tra giọng nói có thể là: pass, reject, fix, error.
"""

from __future__ import annotations

import contextvars
from typing import Any

KINDS = ("ack", "probe", "clarify", "qna", "compose")
OUTCOMES = ("pass", "reject", "fix", "error")
DRAFT_CHARS = 200
MAX_RECORDS = 20

_records: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "speech_checks", default=None
)


def begin() -> list[dict[str, Any]]:
    """Bắt đầu ghi nhận cho một lượt và trả về danh sách sẽ được điền thêm."""
    records: list[dict[str, Any]] = []
    _records.set(records)
    return records


def record(kind: str, outcome: str, reason: str | None = None, draft: str | None = None) -> None:
    records = _records.get()
    if records is None or len(records) >= MAX_RECORDS:
        return
    entry: dict[str, Any] = {"kind": kind, "outcome": outcome}
    if reason:
        entry["reason"] = str(reason)[:64]
    if draft and outcome in ("reject", "fix"):
        text = " ".join(str(draft).split())
        entry["draft"] = text if len(text) <= DRAFT_CHARS else text[: DRAFT_CHARS - 1] + "…"
    records.append(entry)


def collected() -> list[dict[str, Any]]:
    """Trả về bản sao các ghi nhận của lượt hiện tại."""
    return list(_records.get() or [])
