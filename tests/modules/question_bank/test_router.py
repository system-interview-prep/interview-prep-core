def test_question_bank_authoring_requires_authentication(client) -> None:
    response = client.post("/admin/question-bank/questions/drafts", json={})

    assert response.status_code == 401
