import json

from src.core import pipeline_events
from src.modules.interviews.application import pipeline_live


def test_own_notifications_are_skipped_and_others_relayed():
    own = json.dumps({"o": pipeline_events.INSTANCE_ID, "sessionId": "s1"})
    other = json.dumps({"o": "worker", "sessionId": "s1", "events": []})
    assert pipeline_live.parse_notification(own, own_instance=pipeline_events.INSTANCE_ID) is None
    assert (
        pipeline_live.parse_notification(other, own_instance=pipeline_events.INSTANCE_ID)["sessionId"] == "s1"
    )
    assert pipeline_live.parse_notification("not json", own_instance="x") is None
    assert pipeline_live.parse_notification("[1]", own_instance="x") is None


def test_listen_dsn_drops_the_sqlalchemy_driver():
    assert pipeline_live._listen_dsn("postgresql+asyncpg://u:p@db:5432/x") == "postgresql://u:p@db:5432/x"
    assert pipeline_live._listen_dsn("postgres://u:p@db/x") == "postgresql://u:p@db/x"
    assert pipeline_live._listen_dsn("postgresql://u:p@db/x") == "postgresql://u:p@db/x"


def test_namespace_is_registered():
    from src.infrastructure.socketio import sio

    assert pipeline_live.NAMESPACE in sio.namespace_handlers


def test_publish_sends_once_to_every_admin_without_the_origin(monkeypatch):
    import asyncio

    sent = []

    async def emit(event, payload, room=None, namespace=None):
        sent.append((event, room, namespace, payload))

    monkeypatch.setattr(pipeline_live.sio, "emit", emit)
    asyncio.run(pipeline_live.publish({"o": "x", "sessionId": "s1", "events": []}))
    assert [(e, r, n) for e, r, n, _p in sent] == [("pipeline.event", "all", "/admin-pipeline")]
    assert "o" not in sent[0][3]
