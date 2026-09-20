def test_question_bank_authoring_requires_authentication(client) -> None:
    response = client.post("/admin/question-bank/questions/drafts", json={})

    assert response.status_code == 401


def test_question_bank_admin_read_models_require_authentication(client) -> None:
    assert client.get("/admin/question-bank/questions").status_code == 401
    assert client.get("/admin/question-bank/rubrics").status_code == 401


def test_question_import_template_requires_authentication(client) -> None:
    assert client.get("/admin/question-bank/imports/template").status_code == 401
