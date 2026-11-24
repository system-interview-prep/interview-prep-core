from fastapi import HTTPException
from fastapi.testclient import TestClient


def test_structured_error_detail_reaches_the_client() -> None:
    """The interview frontend reads `detail`; a dict detail must survive intact.

    The global handler used to send only `message`, so the
    `question_bank_insufficient` error code never reached the browser and the
    candidate saw "Request failed with status code 409".
    """
    from src.main import create_app

    app = create_app()
    payload = {"errorCode": "question_bank_insufficient", "message": "Bank too small", "details": {}}

    @app.get("/__test__/structured-error")
    async def _raise() -> None:
        raise HTTPException(status_code=409, detail=payload)

    client = TestClient(app)
    try:
        response = client.get("/__test__/structured-error")
    finally:
        client.close()

    assert response.status_code == 409
    assert response.json() == {"message": "Bank too small", "statusCode": 409, "detail": payload}


def test_ai_provider_failure_is_a_503_the_browser_can_read() -> None:
    """An exhausted OpenAI account must not surface as a CORS error.

    The 429 `insufficient_quota` raised inside question selection escaped as an
    unhandled 500, sent from outside the CORS middleware, so the browser hid the
    response and reported a missing Access-Control-Allow-Origin header.
    """
    import httpx
    from openai import RateLimitError

    from src.core.config import get_settings
    from src.main import create_app

    app = create_app()

    @app.post("/__test__/ai-quota")
    async def _raise() -> None:
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        raise RateLimitError(
            "You have no credits remaining.",
            response=httpx.Response(429, request=request),
            body={"code": "credit_balance_exhausted"},
        )

    origin = get_settings().cors_origins[0]
    client = TestClient(app)
    try:
        response = client.post("/__test__/ai-quota", headers={"Origin": origin})
    finally:
        client.close()

    assert response.status_code == 503
    assert response.headers["access-control-allow-origin"] == origin
    body = response.json()
    assert body["detail"]["errorCode"] == "ai_service_unavailable"
    assert "credits" not in response.text
