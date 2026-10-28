from datetime import UTC, datetime, timedelta

from src.modules.interviews.application.pipeline_view import assemble_pipeline, summarize_ai_calls

T0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def ev(
    minute: float, event: str, stage: str, status: str = "info", turn_index=None, duration_ms=None, **payload
):
    return {
        "id": int(minute * 100),
        "created_at": T0 + timedelta(minutes=minute),
        "stage": stage,
        "event": event,
        "status": status,
        "turn_index": turn_index,
        "duration_ms": duration_ms,
        "payload": payload,
    }


def audit(status="OPEN", turns=None, plan_status="LOCKED", ended=None, end_reason=None):
    return {
        "session": {
            "id": "s1",
            "status": status,
            "startedAt": T0.isoformat(),
            "endedAt": ended,
            "endReason": end_reason,
        },
        "plan": {
            "status": plan_status,
            "difficulty": {"level": "advanced", "jobLevel": "intermediate", "practiceTier": "challenge"},
            "questionBudget": 3,
            "targets": [],
            "questionSources": {},
            "uncoveredTargets": [],
        },
        "turns": turns
        if turns is not None
        else [
            {
                "turnId": "t0",
                "turnIndex": 0,
                "status": "ANSWERED",
                "stage": "WARM_UP",
                "source": "~closing_template",
            },
            {
                "turnId": "t1",
                "turnIndex": 1,
                "status": "ASKED",
                "stage": "DEEP_DIVE",
                "source": "question_bank",
            },
            {
                "turnId": "t2",
                "turnIndex": 2,
                "status": "PLANNED",
                "stage": "CLOSING",
                "source": "~closing_template",
            },
        ],
    }


CONTEXT = {"matchPercent": 74, "fitBand": "strong", "practiceTier": "challenge", "entryWarnings": []}

PREP = [
    ev(0, "session_created", "session", duration_ms=40),
    ev(0.1, "plan_started", "plan", "started"),
    ev(0.2, "plan_completed", "plan", "completed", duration_ms=1800),
    ev(0.3, "question_selection_started", "questions", "started"),
    ev(
        0.4,
        "spoken_questions_saved",
        "questions",
        "completed",
        saved=1,
        rejected=1,
        total=2,
        rejected_turn_ids=["t1"],
    ),
    ev(0.5, "question_selection_completed", "questions", "completed", duration_ms=6400, fallback_count=0),
]


def statuses(result):
    return {stage["key"]: stage["status"] for stage in result["stages"]}


def build(audit_value, events, job=None, evaluation=None, turn_evals=None, ai_calls=None, context=CONTEXT):
    return assemble_pipeline(
        audit=audit_value,
        context=context,
        events=events,
        job=job,
        evaluation=evaluation,
        turn_evals=turn_evals or [],
        ai_calls=ai_calls or [],
    )


def test_live_interview_shows_running_stage_and_turn_decisions():
    events = PREP + [
        ev(
            2,
            "turn_spoken",
            "interview",
            turn_index=0,
            duration_ms=900,
            action="NEXT_QUESTION",
            intent="ANSWER",
            sufficiency="SUFFICIENT",
            ack_source="llm",
            interviewer="Cảm ơn bạn. Câu tiếp theo...",
        ),
        ev(3, "probe_guardrail_fallback_triggered", "interview", "warning", reason="leaks_answer"),
    ]
    result = build(audit(), events)
    assert statuses(result) == {
        "created": "done",
        "entry": "done",
        "plan": "done",
        "questions": "done",
        "interview": "running",
        "evaluation": "pending",
        "report": "pending",
    }
    assert result["live"] is True
    assert result["currentTurnIndex"] == 1
    turn0 = result["turns"][0]
    assert turn0["exchanges"][0]["intent"] == "ANSWER"
    assert turn0["exchanges"][0]["coreMs"] == 900
    assert result["turns"][1]["spokenRejected"] is True
    assert result["questions"]["spoken"] == {"saved": 1, "rejected": 1, "total": 2, "rejectedTurnIds": ["t1"]}
    assert [w["event"] for w in result["warnings"]] == ["probe_guardrail_fallback_triggered"]
    assert result["entry"]["practiceTier"] == "challenge"
    assert result["entry"]["policy"]["challengePercent"] >= 1


def test_grading_in_progress_reports_turns_graded_since_the_claim():
    turns = [
        {
            "turnId": "t0",
            "turnIndex": 0,
            "status": "ANSWERED",
            "stage": "DEEP_DIVE",
            "source": "question_bank",
        },
        {
            "turnId": "t1",
            "turnIndex": 1,
            "status": "ANSWERED",
            "stage": "DEEP_DIVE",
            "source": "question_bank",
        },
    ]
    events = PREP + [
        # A failed first attempt, then a new claim: progress counts from the latest claim.
        ev(10, "evaluation_job_claimed", "evaluation", turns_total=2),
        ev(10.5, "turn_graded", "evaluation", turn_index=0, score=7.0, outcome="ANSWERED", graded=True),
        ev(11, "evaluation_pipeline_failed", "evaluation", "error", error_type="Timeout"),
        ev(12, "evaluation_job_claimed", "evaluation", turns_total=2),
        ev(12.5, "turn_graded", "evaluation", turn_index=1, score=5.0, outcome="ANSWERED", graded=True),
    ]
    job = {"status": "PROCESSING", "attempt_count": 2, "last_error": None, "created_at": T0}
    result = build(
        audit(status="CLOSED", turns=turns, ended=(T0 + timedelta(minutes=9)).isoformat()), events, job=job
    )
    assert statuses(result)["interview"] == "done"
    assert statuses(result)["evaluation"] == "running"
    assert result["live"] is True
    assert result["evaluation"]["progress"] == {"graded": 1, "total": 2}
    assert result["evaluation"]["job"]["attemptCount"] == 2


def test_finished_session_with_report():
    turns = [
        {
            "turnId": "t0",
            "turnIndex": 0,
            "status": "ANSWERED",
            "stage": "DEEP_DIVE",
            "source": "question_bank",
        }
    ]
    evaluation = {"id": "e1", "overall_score": 6.5, "decision_recommendation": "CONSIDER", "evaluated_at": T0}
    turn_evals = [{"turn_id": "t0", "score": 6.5, "outcome": "ANSWERED", "rubric_points": [{"key": "a"}]}]
    result = build(
        audit(status="CLOSED", turns=turns, ended=(T0 + timedelta(minutes=20)).isoformat()),
        PREP + [ev(25, "evaluation_pipeline_completed", "evaluation", "completed", duration_ms=30000)],
        job={"status": "DONE", "attempt_count": 1},
        evaluation=evaluation,
        turn_evals=turn_evals,
    )
    assert statuses(result)["evaluation"] == "done" and statuses(result)["report"] == "done"
    assert result["live"] is False
    assert result["turns"][0]["grade"]["score"] == 6.5
    assert result["evaluation"]["decision"] == "CONSIDER"
    interview = next(s for s in result["stages"] if s["key"] == "interview")
    assert interview["durationMs"] == 20 * 60 * 1000


def test_plan_failure_and_entry_block():
    result = build(
        audit(turns=[], plan_status="DRAFT"),
        [
            ev(0, "session_created", "session"),
            ev(0.1, "plan_started", "plan", "started"),
            ev(0.2, "plan_blocked_by_match_threshold", "plan", "warning", reason="match_below_threshold"),
            ev(0.3, "plan_failed", "plan", "error", error_type="InterviewEntryBlocked", duration_ms=300),
        ],
        context={},
    )
    s = statuses(result)
    assert s["entry"] == "error" and s["plan"] == "error" and s["questions"] == "pending"
    assert result["entry"]["blocked"] is True
    assert result["entry"]["blockedReason"] == "match_below_threshold"


def test_closed_without_answers_skips_grading():
    turns = [{"turnId": "t0", "turnIndex": 0, "status": "PLANNED", "stage": "WARM_UP", "source": "x"}]
    result = build(audit(status="CLOSED", turns=turns, end_reason="TECHNICAL_FAILURE"), PREP)
    s = statuses(result)
    assert s["interview"] == "error"
    assert s["evaluation"] == "skipped" and s["report"] == "skipped"


def test_ai_call_summary():
    calls = [
        {
            "purpose": "engine.evaluate",
            "status": "ok",
            "duration_ms": 800,
            "input_tokens": 100,
            "output_tokens": 20,
        },
        {"purpose": "engine.evaluate", "status": "error", "duration_ms": 3000},
        {
            "purpose": "spoken.rewrite",
            "status": "ok",
            "duration_ms": 1200,
            "input_tokens": 50,
            "output_tokens": 10,
        },
    ]
    summary = summarize_ai_calls(calls)
    assert summary["total"] == 3 and summary["errors"] == 1
    assert summary["inputTokens"] == 150 and summary["outputTokens"] == 30
    assert summary["p50Ms"] == 1200
    top = summary["byPurpose"][0]
    assert top["purpose"] == "engine.evaluate" and top["count"] == 2 and top["errors"] == 1
    assert summarize_ai_calls([])["p95Ms"] is None


# --- live state, clocks, voice room ------------------------------------------------

from src.modules.interviews.application.pipeline_view import live_state, timing, voice_summary  # noqa: E402

NOW = T0 + timedelta(minutes=10)


def build_at(audit_value, events, now=NOW, **kwargs):
    return assemble_pipeline(
        audit=audit_value,
        context=CONTEXT,
        events=events,
        job=kwargs.get("job"),
        evaluation=kwargs.get("evaluation"),
        turn_evals=[],
        ai_calls=[],
        turn_times=kwargs.get("turn_times"),
        last_message=kwargs.get("last_message"),
        now=now,
    )


def test_voice_room_state_is_reported_with_its_start_time():
    events = PREP + [
        ev(2, "turn_spoken", "interview", turn_index=0),
        ev(9.5, "voice_state", "interview", state="ai_speaking", kind="question"),
        ev(9.8, "voice_state", "interview", state="candidate_speaking"),
    ]
    result = build_at(audit(), events)
    assert result["liveState"] == {
        "state": "candidate_speaking",
        "kind": None,
        "since": (T0 + timedelta(minutes=9.8)).isoformat(),
        "source": "voice",
    }


def test_stale_voice_state_falls_back_to_the_stored_messages():
    events = PREP + [
        ev(2, "turn_spoken", "interview", turn_index=0),
        ev(3, "voice_state", "interview", state="candidate_speaking"),
    ]
    last = {"role": "assistant", "created_at": T0 + timedelta(minutes=4)}
    result = build_at(audit(), events, last_message=last)
    assert result["liveState"]["state"] == "waiting_answer"
    assert result["liveState"]["source"] == "messages"


def test_waiting_for_an_answer_is_trusted_however_long_it_lasts():
    events = PREP + [
        ev(2, "turn_spoken", "interview", turn_index=0),
        ev(3, "voice_state", "interview", state="waiting_answer"),
    ]
    assert build_at(audit(), events)["liveState"]["state"] == "waiting_answer"


def test_chat_session_reads_state_from_the_last_message():
    events = PREP + [ev(2, "turn_spoken", "interview", turn_index=0)]
    last = {"role": "user", "created_at": T0 + timedelta(minutes=9.9)}
    assert build_at(audit(), events, last_message=last)["liveState"]["state"] == "processing"


def test_grading_and_preparing_and_closed_states():
    grading = build_at(
        audit(status="CLOSED", ended=(T0 + timedelta(minutes=8)).isoformat()),
        PREP + [ev(2, "turn_spoken", "interview", turn_index=0)],
        job={"status": "PROCESSING", "attempt_count": 1, "updated_at": T0 + timedelta(minutes=8.5)},
    )
    assert grading["liveState"]["state"] == "grading"
    preparing = build_at(audit(plan_status="DRAFT", turns=[]), [ev(0.1, "plan_started", "plan", "started")])
    assert preparing["liveState"]["state"] == "preparing"
    closed = build_at(
        audit(status="CLOSED", ended=(T0 + timedelta(minutes=8)).isoformat()),
        PREP + [ev(2, "turn_spoken", "interview", turn_index=0)],
        evaluation={"id": "e1", "overall_score": 7.0},
    )
    assert closed["liveState"] is None


def test_timing_counts_against_the_budget():
    running = timing({"startedAt": T0.isoformat(), "durationMinutes": 25}, now=NOW)
    assert running["elapsedMs"] == 600_000 and running["remainingMs"] == 900_000
    over = timing({"startedAt": T0.isoformat(), "durationMinutes": 5}, now=NOW)
    assert over["remainingMs"] == -300_000
    ended = timing(
        {
            "startedAt": T0.isoformat(),
            "endedAt": (T0 + timedelta(minutes=7)).isoformat(),
            "durationMinutes": 25,
        },
        now=NOW,
    )
    assert ended["elapsedMs"] == 420_000 and ended["remainingMs"] is None
    assert timing({"startedAt": None}, now=NOW)["elapsedMs"] is None


def test_voice_summary_and_incidents_on_their_question():
    events = PREP + [
        ev(1, "voice_session_started", "interview", stt_model="assemblyai/x", tts_provider="vieneu"),
        ev(2, "voice_tts_metrics", "interview", turn_index=1, ttfb_ms=400),
        ev(2.1, "voice_tts_metrics", "interview", turn_index=1, ttfb_ms=900),
        ev(3, "voice_not_heard", "interview", "warning", turn_index=1),
        ev(3.5, "voice_question_replayed", "interview", turn_index=1, reason="repeat_request"),
        ev(4, "voice_core_completed", "interview", turn_index=1, core_ms=2100),
    ]
    result = build_at(audit(), events)
    voice = result["voice"]
    assert voice["notHeard"] == 1 and voice["replayed"] == 1
    assert voice["ttsTtfbP50Ms"] in {400, 900} and voice["ttsTtfbP95Ms"] == 900
    assert voice["coreP50Ms"] == 2100 and voice["ttsProvider"] == "vieneu"
    turn1 = next(t for t in result["turns"] if t["turnIndex"] == 1)
    assert [i["event"] for i in turn1["voiceIncidents"]] == ["voice_not_heard", "voice_question_replayed"]
    assert turn1["voiceIncidents"][1]["reason"] == "repeat_request"
    assert voice_summary(PREP) is None


def test_turn_times_and_warm_up_counts():
    times = {"t0": {"asked_at": T0, "last_answer_at": T0 + timedelta(minutes=1), "answers": 1}}
    result = build_at(audit(), PREP + [ev(2, "turn_spoken", "interview", turn_index=0)], turn_times=times)
    turn0 = result["turns"][0]
    assert turn0["askedAt"] == T0.isoformat() and turn0["answerCount"] == 1
    assert result["turns"][1]["askedAt"] is None
    # The answered turn is the greeting: nothing the grader can score yet.
    assert result["turnCounts"] == {"total": 3, "warmup": 1, "main": 2, "gradable": 0}


def test_live_state_is_none_before_anything_runs():
    assert (
        live_state(
            session={"status": "OPEN"}, stages=[], turns=[], events=[], job=None, last_message=None, now=NOW
        )
        is None
    )
