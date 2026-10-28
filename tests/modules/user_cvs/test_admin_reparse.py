"""Admin "Phân tích lại" for any user's CV."""

import importlib
from types import SimpleNamespace

import pytest

module = importlib.import_module("src.modules.user_cvs.application.admin_reparse")


class _Db:
    def __init__(self, row) -> None:
        self.row = row

    async def execute(self, statement, params):
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: self.row))


class _Service:
    calls: list[tuple[str, str]] = []
    error: Exception | None = None

    def __init__(self, repository, publisher) -> None:
        pass

    async def reparse(self, user_id: str, cv_id: str) -> None:
        if _Service.error:
            raise _Service.error
        _Service.calls.append((user_id, cv_id))


@pytest.fixture(autouse=True)
def _fake_service(monkeypatch):
    _Service.calls, _Service.error = [], None
    monkeypatch.setattr(module, "CvReviewService", _Service)
    monkeypatch.setattr(module, "SqlAlchemyCvRepository", lambda db: None)
    monkeypatch.setattr(module, "CeleryCvParsePublisher", lambda: None)


async def test_unknown_cv_is_404() -> None:
    with pytest.raises(module.CvAdminReparseError) as missing:
        await module.reparse_cv_as_admin(_Db(None), "cv-x")
    assert missing.value.status_code == 404


@pytest.mark.parametrize(("status", "stale"), [("PENDING", True), ("PARSING", False)])
async def test_a_parse_in_flight_is_left_alone(status, stale) -> None:
    with pytest.raises(module.CvAdminReparseError) as busy:
        await module.reparse_cv_as_admin(_Db({"user_id": "u1", "status": status, "stale": stale}), "cv-1")
    assert (busy.value.status_code, busy.value.code) == (409, "in_progress")
    assert _Service.calls == []


@pytest.mark.parametrize(("status", "stale"), [("FAILED", False), ("done", False), ("PARSING", True)])
async def test_reparse_runs_as_the_owner(status, stale) -> None:
    result = await module.reparse_cv_as_admin(
        _Db({"user_id": "u1", "status": status, "stale": stale}), "cv-1"
    )
    assert _Service.calls == [("u1", "cv-1")]
    assert result == {"cvId": "cv-1", "previousStatus": status.upper(), "status": "PENDING"}


async def test_queue_down_is_503() -> None:
    _Service.error = module.CvReviewConflictError("queue")
    with pytest.raises(module.CvAdminReparseError) as down:
        await module.reparse_cv_as_admin(_Db({"user_id": "u1", "status": "FAILED", "stale": False}), "cv-1")
    assert down.value.status_code == 503
