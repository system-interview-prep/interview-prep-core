def test_admin_routes_require_authentication(client) -> None:
    assert client.get("/admin/overview").status_code == 401
    assert client.get("/admin/sessions").status_code == 401


def test_monthly_trend_keeps_year_order_across_new_year():
    from datetime import datetime

    from src.modules.admin.router import monthly_trend_items

    rows = [
        {"bucket": datetime(2027, 1, 1), "count": 4},
        {"bucket": datetime(2026, 11, 1), "count": 2},
        {"bucket": datetime(2026, 12, 1), "count": 3},
        {"bucket": None, "count": 9},
    ]
    assert monthly_trend_items(rows) == [
        {"month": "11/2026", "key": "2026-11", "count": 2},
        {"month": "12/2026", "key": "2026-12", "count": 3},
        {"month": "01/2027", "key": "2027-01", "count": 4},
    ]
