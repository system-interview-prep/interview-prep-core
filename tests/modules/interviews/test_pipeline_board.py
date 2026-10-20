from datetime import UTC, datetime

from src.modules.interviews.application import pipeline_board as board

T = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def row(**kwargs):
    base = {"status": "OPEN", "mode": "voice", "plan_status": "LOCKED", "job_status": None, "started_at": T}
    return {**base, **kwargs}


def test_board_step_follows_the_pipeline():
    assert board.board_step(row(plan_status=None), None, None, None, None)["step"] == "planning"
    assert board.board_step(row(plan_status="READY"), None, None, None, None)["step"] == "selecting"
    assert board.board_step(row(), None, None, None, None)["step"] == "not_started"
    voice = board.board_step(
        row(),
        None,
        {"state": "candidate_speaking", "kind": None, "at": "t"},
        {"role": "assistant", "created_at": T},
        None,
    )
    assert voice == {"step": "interviewing", "state": "candidate_speaking", "kind": None, "since": "t"}
    chat = board.board_step(row(mode="chat"), None, None, {"role": "user", "created_at": T}, None)
    assert chat["state"] == "processing"
    grading = board.board_step(
        row(status="CLOSED", job_status="PROCESSING"), None, None, None, {"graded": 2, "total": 4}
    )
    assert grading["step"] == "grading" and grading["graded"] == 2
    assert (
        board.board_step(row(status="CLOSED", job_status="FAILED"), None, None, None, None)["step"]
        == "grading_failed"
    )
    assert (
        board.board_step(row(status="CLOSED", end_reason="COMPLETED"), None, None, None, None)["step"]
        == "finished"
    )


def empty(**overrides):
    base = dict(
        grading_failed=[],
        grading_stuck=[],
        plan_stuck=[],
        idle=[],
        live_errors=[],
        degraded=[],
        turn_stats={},
        ai_stats={},
    )
    return {**base, **overrides}


def test_no_alerts_when_everything_is_fine():
    stats = {"turns": 40, "ack_total": 40, "ack_fallback": 4, "probes": 12, "probe_fallback": 1, "p95": 3000}
    assert board.build_alerts(**empty(turn_stats=stats, ai_stats={"calls": 100, "errors": 2})) == []


def test_alerts_are_raised_above_thresholds_and_errors_come_first():
    session = {"id": "s1", "candidate_name": "An", "job_title": "Backend", "updated_at": T}
    alerts = board.build_alerts(
        **empty(
            grading_failed=[session],
            plan_stuck=[session],
            turn_stats={
                "turns": 20,
                "ack_total": 20,
                "ack_fallback": 8,
                "probes": 10,
                "probe_fallback": 3,
                "p95": 9500,
            },
            ai_stats={"calls": 30, "errors": 5, "last_error_at": T},
        )
    )
    kinds = [a["kind"] for a in alerts]
    assert set(kinds) == {
        "grading_failed",
        "plan_stuck",
        "ack_fallback_rate",
        "probe_fallback_rate",
        "turn_latency",
        "ai_errors",
    }
    assert [a["severity"] for a in alerts] == sorted(
        [a["severity"] for a in alerts], key=lambda s: s != "error"
    )
    failed = next(a for a in alerts if a["kind"] == "grading_failed")
    assert failed["sessions"] == [{"id": "s1", "candidate": "An", "jobTitle": "Backend"}]
    assert failed["lastAt"] == T.isoformat()
    rate = next(a for a in alerts if a["kind"] == "ack_fallback_rate")
    assert rate["count"] == 8 and rate["total"] == 20 and rate["rate"] == 0.4
    assert next(a for a in alerts if a["kind"] == "turn_latency")["p95Ms"] == 9500


def test_rates_need_enough_turns():
    stats = {"turns": 4, "ack_total": 4, "ack_fallback": 4, "probes": 2, "probe_fallback": 2, "p95": 20000}
    assert board.build_alerts(**empty(turn_stats=stats)) == []


def test_live_errors_sum_and_list_sessions():
    rows = [
        {"id": "s1", "errors": 2, "last_at": T, "candidate_email": "a@x"},
        {"id": "s2", "errors": 1, "last_at": T, "candidate_name": "B"},
    ]
    alert = board.build_alerts(**empty(live_errors=rows))[0]
    assert alert["kind"] == "live_errors" and alert["count"] == 3
    assert [s["candidate"] for s in alert["sessions"]] == ["a@x", "B"]
