from src.modules.interview_questions.router import _parse_questions


def test_parse_questions_supports_json_and_numbered_text() -> None:
    assert _parse_questions('{"questions":["One?","Two?"]}', 1) == ["One?"]
    assert _parse_questions("1. One?\n2. Two?", 5) == ["One?", "Two?"]


def test_generate_questions_requires_authentication(client) -> None:
    response = client.post("/ai/session/s1/questions/generate", json={})
    assert response.status_code == 401
