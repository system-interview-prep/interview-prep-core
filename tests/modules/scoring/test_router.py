from src.modules.scoring.router import ScoreRequest, _score_value


def test_score_value_is_normalized() -> None:
    assert _score_value({"overall_score": 0.75}) == 0.75
    assert _score_value({"score": 4}) == 1.0
    assert _score_value({}) == 0.0


def test_scoring_requires_authentication(client) -> None:
    payload = ScoreRequest(cvId="cv1", jobDescriptionId="jd1").model_dump()
    assert client.post("/ai/score-cv-jp", json=payload).status_code == 401
