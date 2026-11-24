"""Tell an interview expert what happened to the question version they submitted.

Admins already learn about versions waiting for review (admin_notifier). The
author is told only about outcomes they must act on or may want to know:
changes requested, rejected, approved. An APPROVE review on its own is a step
toward final approval, not an outcome, so it is not announced.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.notifications.service import notify_user

NOTIFICATION_TYPE = "question.decision"
QUOTE_CHARS = 80

_OUTCOMES = {
    "REQUEST_CHANGES": ("Câu hỏi cần chỉnh sửa", "cần chỉnh sửa trước khi gửi duyệt lại"),
    "REJECT": ("Câu hỏi bị từ chối", "bị từ chối"),
    "APPROVED": ("Câu hỏi đã được phê duyệt", "đã được phê duyệt và vào ngân hàng câu hỏi"),
}


def decision_message(decision: str, question_text: str, comment: str | None) -> tuple[str, str] | None:
    """(title, message) for an outcome, or None when the author need not be told."""
    outcome = _OUTCOMES.get(decision)
    if outcome is None:
        return None
    title, verb = outcome
    quote = " ".join(str(question_text or "").split())
    if len(quote) > QUOTE_CHARS:
        quote = quote[: QUOTE_CHARS - 1].rstrip() + "…"
    message = f"“{quote}” {verb}."
    if comment and comment.strip() and decision != "APPROVED":
        message += f" Góp ý: {comment.strip()}"
    return title, message


async def notify_question_author(
    db: AsyncSession,
    version: object,
    decision: str,
    *,
    comment: str | None = None,
) -> None:
    text_ = decision_message(decision, getattr(version, "canonical_text", ""), comment)
    if text_ is None:
        return
    title, message = text_
    question_id = str(getattr(version, "question_id", ""))
    version_id = str(getattr(version, "id", ""))
    await notify_user(
        db,
        str(getattr(version, "created_by", "")),
        type_=NOTIFICATION_TYPE,
        title=title,
        message=message,
        data={
            "kind": f"question_{decision.lower()}",
            "questionId": question_id,
            "questionVersionId": version_id,
            "link": f"/admin/question-bank/{question_id}",
        },
        dedupe_key=f"question:{version_id}:{decision}",
    )
