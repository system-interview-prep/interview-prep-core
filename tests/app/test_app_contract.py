def test_openapi_contains_every_public_business_route(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/health",
        "/auth/register",
        "/auth/login",
        "/auth/google",
        "/user/profile",
        "/users/me/cvs",
        "/admin/job-categories",
        "/admin/job-profiles",
        "/admin/job-profiles/uploads",
        "/ai/session",
        "/ai/sessions",
        "/interview/video-calls",
        "/interview/video-calls/start",
        "/api/v1/matching/match",
    }
    assert expected.issubset(paths)


def test_unknown_api_returns_404(client) -> None:
    assert client.get("/api/v1/not-found").status_code == 404
