def test_health_returns_service_identity(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "interview-prep-fastapi-backend",
    }


def test_health_rejects_unsupported_method(client) -> None:
    response = client.post("/health")

    assert response.status_code == 405
