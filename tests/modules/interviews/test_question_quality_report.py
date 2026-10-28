from src.modules.interviews.evaluation.question_quality import summarize_question_quality


def _turn(**overrides):
    row = {
        "session_id": "s1",
        "user_id": "u1",
        "turn_id": "t1",
        "status": "EVALUATED",
        "stage": "DEEP_DIVE",
        "source": "question_bank",
        "stable_key": "q-1",
        "difficulty": "intermediate",
        "plan_difficulty": "intermediate",
        "plan_difficulty_source": "job_min_experience_years",
        "question_version_id": "qv-1",
        "question_text": "Bạn tối ưu truy vấn PostgreSQL chậm như thế nào?",
        "locale": "vi-VN",
        "score": 6.0,
        "answer_seconds": 120,
        "hard_answer_seconds": 180,
    }
    row.update(overrides)
    return row


def _group(report, stage, source):
    return next(g for g in report["groups"] if g["stage"] == stage and g["source"] == source)


def test_question_signals_per_stage_and_source():
    turns = [
        _turn(),
        _turn(turn_id="t2", score=2.0, answer_seconds=200, difficulty="advanced", question_version_id="qv-2"),
        _turn(turn_id="t3", status="SKIPPED", score=None, answer_seconds=None, question_version_id="qv-3"),
    ]
    messages = [
        {"turn_id": "t1", "message_type": "PROBE", "content": "Vì sao chọn index đó?", "generated_by": "llm"},
        {"turn_id": "t2", "message_type": "PROBE", "content": "Vì sao bạn chọn cách đó?", "generated_by": "fixed_fallback"},
        {"turn_id": "t2", "message_type": "CLARIFY", "content": "Ý câu hỏi là...", "generated_by": "llm"},
    ]

    group = _group(summarize_question_quality(turns, messages), "DEEP_DIVE", "question_bank")

    assert group["asked"] == 3
    assert group["answeredRate"] == round(2 / 3, 4)
    assert group["skippedRate"] == round(1 / 3, 4)
    assert (group["meanScore"], group["scoreStdDev"]) == (4.0, 2.0)
    assert group["overHardTimeRate"] == 0.5
    assert group["probesPerTurn"] == round(2 / 3, 4)
    assert group["llmProbeShare"] == 0.5
    assert group["clarifiesPerTurn"] == round(1 / 3, 4)
    assert group["difficultyMatchRate"] == round(2 / 3, 4)
    assert group["duplicateInSessionCount"] == 0


def test_flags_repeats_wrong_language_and_leaks():
    turns = [
        _turn(),
        _turn(session_id="s2", turn_id="t9", question_text="Explain closures in JavaScript."),
        _turn(session_id="s3", turn_id="t10", question_version_id="qv-7", question_text="Câu này được 3 điểm nếu đúng ý."),
    ]
    group = _group(summarize_question_quality(turns), "DEEP_DIVE", "question_bank")

    assert group["repeatedAcrossSessionsRate"] == round(2 / 3, 4)
    assert group["wrongLanguageCount"] == 1
    assert group["leakCount"] == 1


def test_legacy_turns_get_an_inferred_source():
    report = summarize_question_quality(
        [
            _turn(source=None, stable_key="warmup-intro", stage="WARM_UP", question_version_id=None),
            _turn(turn_id="t2", source=None, stable_key="cv-validation", stage="VALIDATE", question_version_id=None),
            _turn(turn_id="t3", source=None, stable_key=None, stage="CLOSING", question_version_id=None),
        ]
    )
    assert report["sourceMix"] == {"~warmup_template": 1, "~cv_template": 1, "~closing_template": 1}
