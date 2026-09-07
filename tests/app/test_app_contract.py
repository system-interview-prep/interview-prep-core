def test_openapi_contains_every_public_business_route(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/health",
        "/auth/register",
        "/auth/login",
        "/auth/google",
        "/users/me",
        "/users/me/cvs",
        "/admin/job-categories",
        "/admin/job-descriptions",
        "/admin/job-descriptions/uploads",
        "/admin/job-descriptions/uploads/{upload_id}",
        "/admin/job-descriptions/uploads/{upload_id}/finalize",
        "/ai/session",
        "/ai/sessions",
        "/ai/chat",
        "/ai/history",
        "/ai/chat-voice",
        "/ai/score-cv-jp",
        "/ai/session/{session_id}/questions/generate",
        "/interview/video-calls",
        "/interview/video-calls/start",
        "/interview/video-calls/{call_id}/chat-voice",
        "/notifications",
        "/notifications/{notification_id}/read",
        "/api/v1/matching/match",
    }
    assert expected.issubset(paths)


def test_unknown_api_returns_404(client) -> None:
    assert client.get("/api/v1/not-found").status_code == 404
