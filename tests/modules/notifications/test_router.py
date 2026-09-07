from datetime import UTC, datetime

from src.modules.notifications.router import _notification


def test_notification_mapper() -> None:
    now = datetime.now(UTC)
    value = _notification(
        {
            "id": "n1",
            "type": "cv",
            "title": "Done",
            "message": "Parsed",
            "data": None,
            "read_at": None,
            "created_at": now,
        }
    )
    assert value["read"] is False
    assert value["data"] == {}


def test_notification_routes_require_authentication(client) -> None:
    assert client.get("/notifications").status_code == 401
    assert client.patch("/notifications/n1/read").status_code == 401
