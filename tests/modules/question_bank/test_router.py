def test_question_bank_authoring_requires_authentication(client) -> None:
    response = client.post("/admin/question-bank/questions/drafts", json={})

    assert response.status_code == 401


def test_question_bank_admin_read_models_require_authentication(client) -> None:
    assert client.get("/admin/question-bank/questions").status_code == 401
    assert client.get("/admin/question-bank/rubrics").status_code == 401


def test_question_import_template_requires_authentication(client) -> None:
    assert client.get("/admin/question-bank/imports/template").status_code == 401


def test_active_bank_is_not_readable_by_candidates() -> None:
    """H10: the active bank exposes questions and scoring objectives."""
    from src.modules.question_bank.router import router

    route = next(r for r in router.routes if r.path.endswith("/active"))
    guards = [dep.call.__qualname__ for dep in route.dependant.dependencies]
    assert any(name.startswith("require_roles") for name in guards)
