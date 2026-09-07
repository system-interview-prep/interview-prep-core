def test_openapi_contains_every_public_business_route(client) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert {
        "/health",
        "/auth/register",
        "/auth/login",
        "/auth/google",
        "/user/profile",
        "/users/me/cvs",
        "/users/me/cvs/{cv_id}",
        "/admin/job-categories",
        "/admin/job-categories/{category_id}",
        "/admin/job-profiles",
        "/admin/job-profiles/{profile_id}",
        "/api/v1/matching/match",
    }.issubset(paths)
    assert "get" in paths["/health"]
    assert "post" in paths["/api/v1/matching/match"]
    responses = paths["/api/v1/matching/match"]["post"]["responses"]
    assert {"200", "202", "422"}.issubset(responses)


def test_unknown_api_returns_404(client) -> None:
    response = client.get("/api/v1/not-found")

    assert response.status_code == 404
