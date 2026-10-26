"""What the signed-in person may do with one question version, and why not.

The service and the router guards stay the authority (they re-check every
call). This read model lets the admin page show only the buttons that will
work, with the reason when one is locked ("you wrote this question"), and
lets the list answer "what is waiting for me".

Roles (``src/core/roles.py``):

* QUESTION_AUTHOR: write drafts, edit and submit their own, start revisions.
* QUESTION_REVIEWER: record a review on someone else's version in review.
* QUESTION_BANK_ADMIN: final approval (needs at least one APPROVE review).
* ADMIN: every role, but never on their own version (four-eyes rule).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.core.roles import ADMIN, QUESTION_AUTHOR, QUESTION_BANK_ADMIN, QUESTION_REVIEWER

EDITABLE_STATUSES = frozenset({"DRAFT", "NEEDS_REVISION"})
REVIEW_STATUSES = frozenset({"IN_REVIEW", "PILOT_READY"})
# Finished versions: a change starts a new revision.
REVISABLE_STATUSES = frozenset({"APPROVED", "CALIBRATED", "REJECTED"})
REVIEW_TYPES = ("CONTENT", "DIFFICULTY", "FAIRNESS")

# "My work" buckets of the question list.
WORK_BUCKETS = ("drafts", "changes", "review", "approve")


def _has(roles: Iterable[str], *wanted: str) -> bool:
    held = {str(role).upper() for role in roles}
    return ADMIN in held or bool(held.intersection(wanted))


def _allowed() -> dict[str, Any]:
    return {"allowed": True, "reason": None}


def _locked(reason: str) -> dict[str, Any]:
    return {"allowed": False, "reason": reason}


def version_actions(
    *,
    status: str,
    created_by: str | None,
    actor_id: str,
    roles: Iterable[str],
    has_rubric: bool,
    approve_reviews: int,
) -> dict[str, dict[str, Any]]:
    """One entry per action: ``{"allowed": bool, "reason": code | None}``.

    Reason codes: ``no_role``, ``not_author``, ``own_question``,
    ``not_editable``, ``not_in_review``, ``needs_rubric``,
    ``needs_approve_review``, ``not_finished``.
    """
    roles = list(roles)
    status = str(status or "").upper()
    own = bool(created_by) and str(created_by) == str(actor_id)
    author = _has(roles, QUESTION_AUTHOR)
    reviewer = _has(roles, QUESTION_REVIEWER)
    approver = _has(roles, QUESTION_BANK_ADMIN)

    def author_action(*, needs_rubric: bool) -> dict[str, Any]:
        if not author:
            return _locked("no_role")
        if status not in EDITABLE_STATUSES:
            return _locked("not_editable")
        if not own:
            return _locked("not_author")
        if needs_rubric and not has_rubric:
            return _locked("needs_rubric")
        return _allowed()

    if not reviewer:
        review = _locked("no_role")
    elif status not in REVIEW_STATUSES:
        review = _locked("not_in_review")
    elif own:
        review = _locked("own_question")
    else:
        review = _allowed()

    if not approver:
        approve = _locked("no_role")
    elif status not in REVIEW_STATUSES:
        approve = _locked("not_in_review")
    elif own:
        approve = _locked("own_question")
    elif approve_reviews < 1:
        approve = _locked("needs_approve_review")
    else:
        approve = _allowed()

    if not author:
        revise = _locked("no_role")
    elif status not in REVISABLE_STATUSES:
        revise = _locked("not_finished")
    else:
        revise = _allowed()

    return {
        "edit": author_action(needs_rubric=False),
        "submit": author_action(needs_rubric=True),
        "review": review,
        "approve": approve,
        "revise": revise,
    }


def work_filter(bucket: str | None) -> str | None:
    """SQL condition (on ``qv``, with ``:me``) for a "my work" bucket; None for no filter."""
    if bucket == "drafts":
        return "qv.created_by = :me AND qv.status = 'DRAFT'"
    if bucket == "changes":
        return "qv.created_by = :me AND qv.status = 'NEEDS_REVISION'"
    if bucket == "review":
        return (
            "qv.status IN ('IN_REVIEW', 'PILOT_READY') AND qv.created_by <> :me "
            "AND NOT EXISTS (SELECT 1 FROM question_reviews r "
            "WHERE r.question_version_id = qv.id AND r.reviewer_id = :me)"
        )
    if bucket == "approve":
        return (
            "qv.status IN ('IN_REVIEW', 'PILOT_READY') AND qv.created_by <> :me "
            "AND EXISTS (SELECT 1 FROM question_reviews r "
            "WHERE r.question_version_id = qv.id AND r.decision = 'APPROVE')"
        )
    return None


def work_buckets_for(roles: Iterable[str]) -> list[str]:
    """The buckets that make sense for these roles (an author has no approval queue)."""
    roles = list(roles)
    buckets: list[str] = []
    if _has(roles, QUESTION_AUTHOR):
        buckets += ["drafts", "changes"]
    if _has(roles, QUESTION_REVIEWER):
        buckets.append("review")
    if _has(roles, QUESTION_BANK_ADMIN):
        buckets.append("approve")
    return buckets
