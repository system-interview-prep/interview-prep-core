def test_admin_user_routes_require_authentication(client) -> None:
    assert client.get("/admin/users").status_code == 401
    assert client.post("/admin/users", json={}).status_code == 401
