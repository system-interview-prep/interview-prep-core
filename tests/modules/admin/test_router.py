def test_admin_routes_require_authentication(client) -> None:
    assert client.get("/admin/overview").status_code == 401
    assert client.get("/admin/sessions").status_code == 401
