from datetime import UTC, datetime

from src.modules.interviews.application import admin_notifier as notifier

NOW = datetime(2026, 10, 19, 14, 5, tzinfo=UTC)


def test_interview_problems_notify_once_per_interview():
    alerts = [
        {
            "kind": "grading_failed",
            "severity": "error",
            "count": 2,
            "sessions": [
                {"id": "s1", "candidate": "Nguyễn Văn A", "jobTitle": "Backend"},
                {"id": "s2", "candidate": None, "jobTitle": None},
            ],
        }
    ]
    items = notifier.notification_items(alerts, now=NOW)
    assert [i["dedupe_key"] for i in items] == ["ai_alert:grading_failed:s1", "ai_alert:grading_failed:s2"]
    assert items[0]["title"] == "Chấm điểm lỗi · Nguyễn Văn A"
    assert items[0]["message"].startswith("Backend: chưa có báo cáo")
    assert items[1]["title"] == "Chấm điểm lỗi · s2"
    assert items[1]["message"].startswith("chưa có báo cáo")
    assert items[0]["data"] == {
        "kind": "grading_failed",
        "severity": "error",
        "sessionId": "s1",
        "link": "/admin/interviews/s1",
    }


def test_rate_problems_notify_at_most_once_per_hour():
    alert = {
        "kind": "ack_fallback_rate",
        "severity": "warning",
        "count": 8,
        "total": 20,
        "rate": 0.4,
        "threshold": 0.3,
        "minutes": 60,
        "sessions": [],
    }
    first = notifier.notification_items([alert], now=NOW)[0]
    same_hour = notifier.notification_items([alert], now=NOW.replace(minute=55))[0]
    next_hour = notifier.notification_items([alert], now=NOW.replace(hour=15))[0]
    assert first["dedupe_key"] == same_hour["dedupe_key"] == "ai_alert:ack_fallback_rate:2026101914"
    assert next_hour["dedupe_key"] != first["dedupe_key"]
    assert first["title"] == "Câu ghi nhận dự phòng 40%"
    assert first["message"] == "8/20 lượt trong 60 phút qua (ngưỡng 30%)."
    assert first["data"]["link"] == "/admin/ai"


def test_every_alert_kind_has_words_and_unknown_kinds_are_skipped():
    from src.modules.interviews.application.pipeline_board import build_alerts

    session = {"id": "s1", "candidate_name": "An", "job_title": "Backend", "updated_at": NOW}
    alerts = build_alerts(
        grading_failed=[session],
        grading_stuck=[session],
        plan_stuck=[session],
        idle=[session],
        live_errors=[{**session, "errors": 1, "last_at": NOW}],
        degraded=[{**session, "last_at": NOW}],
        turn_stats={
            "turns": 20,
            "ack_total": 20,
            "ack_fallback": 10,
            "probes": 10,
            "probe_fallback": 5,
            "p95": 9500,
        },
        ai_stats={"calls": 10, "errors": 4},
    )
    items = notifier.notification_items(alerts, now=NOW)
    assert {i["data"]["kind"] for i in items} == set(notifier._TEXT)
    assert all("{" not in i["title"] + i["message"] for i in items)
    turn = next(i for i in items if i["data"]["kind"] == "turn_latency")
    assert turn["title"] == "AI phản hồi chậm: p95 9.5 s"
    assert next(i for i in items if i["data"]["kind"] == "ai_errors")["data"]["link"] == "/admin/ai/errors"
    assert notifier.notification_items([{"kind": "something_new", "sessions": []}], now=NOW) == []


def test_only_problem_summaries_wake_the_notifier():
    import asyncio

    async def run():
        n = notifier.AdminNotifier()
        n._loop = asyncio.get_running_loop()
        n._wake = asyncio.Event()
        n._on_summaries([{"sessionId": "s1", "problems": 0}])
        await asyncio.sleep(0)
        quiet = n._wake.is_set()
        n._on_summaries([{"sessionId": "s1", "problems": 1}])
        await asyncio.sleep(0.01)
        return quiet, n._wake.is_set()

    assert asyncio.run(run()) == (False, True)


def test_new_notifications_are_announced_to_admin_pages(monkeypatch):
    import asyncio

    from src.infrastructure.socketio import sio

    sent = []

    async def emit(event, payload, room=None, namespace=None):
        sent.append((event, room, namespace, payload))

    monkeypatch.setattr(sio, "emit", emit)
    items = notifier.notification_items(
        [
            {
                "kind": "plan_stuck",
                "severity": "warning",
                "minutes": 5,
                "sessions": [{"id": "s3", "candidate": "B"}],
            }
        ],
        now=NOW,
    )
    asyncio.run(notifier.publish_notifications(items))
    event, room, namespace, payload = sent[0]
    assert (event, room, namespace) == ("notification.new", "all", "/admin-pipeline")
    assert payload == {
        "count": 1,
        "items": [
            {
                "title": "Kế hoạch bị kẹt · B",
                "message": "chưa lập xong sau 5 phút, ứng viên đang chờ.",
                "kind": "plan_stuck",
                "severity": "warning",
                "link": "/admin/interviews/s3",
            }
        ],
    }
