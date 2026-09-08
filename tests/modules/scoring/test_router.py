from src.modules.scoring.calculation import score_value


def test_score_value_is_normalized() -> None:
    assert score_value({"overall_score": 0.75}) == 0.75
    assert score_value({"score": 4}) == 1.0
    assert score_value({}) == 0.0
    assert score_value({"combined_results": [{"combined_score": 0.8}]}) == 0.8


def test_scoring_requires_authentication(client) -> None:
    assert client.post("/ai/score-cv-jp", json={"cvId": "cv1", "jobDescriptionId": "jd1"}).status_code == 401
