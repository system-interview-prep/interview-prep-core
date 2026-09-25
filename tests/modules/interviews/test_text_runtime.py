from datetime import UTC, datetime

from src.modules.interviews.text_runtime import _turn_payload


def test_turn_payload_exposes_frozen_question_and_answer_lifecycle():
    started = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    completed = datetime(2026, 9, 25, 8, 2, tzinfo=UTC)
    payload = _turn_payload(
        {
            "id": "turn-1",
            "turn_index": 0,
            "status": "ANSWERED",
            "question_version_id": None,
            "rubric_version_id": None,
            "question_snapshot": {
                "questionText": "Explain dependency injection.",
                "objective": "Assess DI fundamentals.",
            },
            "answer_text": "It separates construction from use.",
            "started_at": started,
            "completed_at": completed,
        }
    )

    assert payload["turnId"] == "turn-1"
    assert payload["status"] == "ANSWERED"
    assert payload["question"]["questionText"] == "Explain dependency injection."
    assert payload["answerText"] == "It separates construction from use."
    assert payload["startedAt"] == started.isoformat()
    assert payload["completedAt"] == completed.isoformat()
