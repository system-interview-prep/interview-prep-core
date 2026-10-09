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
