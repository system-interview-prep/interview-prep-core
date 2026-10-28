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


def test_review_work_tells_admins_what_waits_for_them():
    submitted = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    items = notifier.review_items(
        questions=[
            {
                "version_id": "v1",
                "question_id": "q1",
                "stable_key": "be.http.idempotency",
                "text": "Giải thích tính idempotent của phương thức HTTP.",
                "created_by": "author-1",
                "submitted_at": submitted,
                "author": "Trần B",
            }
        ],
        generated={"count": 3, "latest": submitted},
        lessons=[
            {"id": "p1", "text": "Hỏi thêm ví dụ khi câu trả lời chung chung.", "origin": "reflection"},
            {
                "id": "p2",
                "text": "Nhắc ứng viên thời gian còn lại.",
                "origin": "manual",
                "created_by": "admin-1",
            },
        ],
        suggestions={"count": 0, "latest": None},
    )
    assert [i["data"]["kind"] for i in items] == [
        "review_question",
        "review_generated",
        "review_playbook",
        "review_playbook",
    ]
    question = items[0]
    assert question["dedupe_key"] == f"review:question:v1:{submitted.isoformat()}"
    assert question["title"] == "Câu hỏi chờ duyệt · be.http.idempotency"
    assert question["message"].startswith("Trần B gửi duyệt:")
    assert question["data"]["link"] == "/admin/question-bank/q1"
    # The author cannot approve their own question, so they are not notified.
    assert question["exclude"] == "author-1"
    assert items[1]["title"] == "3 câu do AI sinh đang chờ duyệt"
    assert items[2]["message"].startswith("AI đề xuất:") and items[2]["exclude"] == ""
    assert items[3]["message"].startswith("Một admin đề xuất:") and items[3]["exclude"] == "admin-1"
    assert items[3]["data"]["link"] == "/admin/ai/playbook"


def test_a_resubmitted_question_notifies_again_and_long_text_is_cut():
    row = {"version_id": "v1", "question_id": "q1", "text": "x " * 200}
    first = notifier.review_items(
        questions=[{**row, "submitted_at": datetime(2026, 10, 27, tzinfo=UTC)}],
        generated=None,
        lessons=[],
        suggestions={"count": 4, "latest": datetime(2026, 10, 28, tzinfo=UTC)},
    )
    again = notifier.review_items(
        questions=[{**row, "submitted_at": datetime(2026, 10, 28, tzinfo=UTC)}],
        generated=None,
        lessons=[],
        suggestions=None,
    )
    assert first[0]["dedupe_key"] != again[0]["dedupe_key"]
    assert "…" in first[0]["message"] and len(first[0]["message"]) < 200
    assert first[1]["title"] == "4 đề xuất cho ngân hàng câu hỏi"
    assert first[1]["data"]["link"] == "/admin/question-bank/insights"
