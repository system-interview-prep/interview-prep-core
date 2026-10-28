"""Who may do what with a question version, and the reason shown when not."""

from src.modules.question_bank.permissions import version_actions, work_buckets_for, work_filter


def _actions(status="DRAFT", created_by="me", roles=("QUESTION_AUTHOR",), rubric=True, approvals=0):
    return version_actions(
        status=status,
        created_by=created_by,
        actor_id="me",
        roles=roles,
        has_rubric=rubric,
        approve_reviews=approvals,
    )


def test_author_edits_and_submits_only_their_own_draft_with_a_rubric() -> None:
    mine = _actions()
    assert mine["edit"]["allowed"] and mine["submit"]["allowed"]
    assert _actions(rubric=False)["submit"] == {"allowed": False, "reason": "needs_rubric"}
    assert _actions(created_by="someone")["edit"]["reason"] == "not_author"
    assert _actions(status="IN_REVIEW")["edit"]["reason"] == "not_editable"
    assert _actions(roles=("CANDIDATE",))["edit"]["reason"] == "no_role"


def test_review_is_admin_only_on_a_version_in_review_and_someone_elses_question() -> None:
    assert _actions("IN_REVIEW", "other", ("ADMIN",))["review"]["allowed"]
    assert _actions("IN_REVIEW", "me", ("ADMIN",))["review"]["reason"] == "own_question"
    assert _actions("DRAFT", "other", ("ADMIN",))["review"]["reason"] == "not_in_review"
    assert _actions("IN_REVIEW", "other", ("QUESTION_AUTHOR",))["review"]["reason"] == "no_role"


def test_retired_reviewer_and_bank_admin_roles_grant_nothing() -> None:
    for role in ("QUESTION_REVIEWER", "QUESTION_BANK_ADMIN"):
        actions = _actions("IN_REVIEW", "other", (role,), approvals=1)
        assert actions["review"]["reason"] == "no_role"
        assert actions["approve"]["reason"] == "no_role"
        assert work_buckets_for([role]) == []


def test_approval_needs_an_approve_review_and_never_on_ones_own_question() -> None:
    approver = ("ADMIN",)
    assert (
        _actions("IN_REVIEW", "other", approver, approvals=0)["approve"]["reason"] == "needs_approve_review"
    )
    assert _actions("IN_REVIEW", "other", approver, approvals=1)["approve"]["allowed"]
    # ADMIN holds every role but the four-eyes rule still applies.
    assert _actions("IN_REVIEW", "me", ("ADMIN",), approvals=1)["approve"]["reason"] == "own_question"
    assert _actions("IN_REVIEW", "other", ("ADMIN",), approvals=1)["approve"]["allowed"]


def test_revision_starts_from_a_finished_version() -> None:
    assert _actions("APPROVED", "other")["revise"]["allowed"]
    assert _actions("IN_REVIEW", "other")["revise"]["reason"] == "not_finished"


def test_work_buckets_follow_roles() -> None:
    assert work_buckets_for(["QUESTION_AUTHOR"]) == ["drafts", "changes"]
    assert work_buckets_for(["ADMIN"]) == ["drafts", "changes", "review", "approve"]
    assert work_buckets_for(["CANDIDATE"]) == []


def test_work_filters_exclude_ones_own_questions_from_review_and_approval() -> None:
    assert "qv.created_by = :me" in work_filter("drafts")
    assert "qv.created_by <> :me" in work_filter("review") and "r.reviewer_id = :me" in work_filter("review")
    assert "r.decision = 'APPROVE'" in work_filter("approve")
    assert work_filter(None) is None
