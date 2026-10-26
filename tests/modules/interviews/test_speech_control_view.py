"""Admin page "Lời nói AI": guard outcomes, reasons, sources and style across turns."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from src.modules.interviews.application.pipeline_view import _exchange_view
from src.modules.interviews.application.speech_control_view import config_view, summarize_speech


def _row(session: str, payload: dict, turn: int = 1) -> dict:
    at = datetime(2026, 10, 25, tzinfo=UTC)
    return {"session_id": session, "turn_index": turn, "created_at": at, "payload": payload}


ROWS = [
    _row(
        "s1",
        {
            "interviewer": "À, vậy là khoá dòng nhé.\n\nGiờ mình qua phần kỹ thuật nhé. Bạn dùng index nào?",
            "acknowledgement": "À, vậy là khoá dòng nhé.",
            "ack_source": "fallback",
            "speech_plan": [
                {"act": "ACK", "source": "fallback"},
                {"act": "QUESTION", "source": "pregenerated"},
            ],
            "speech_checks": [
                {"kind": "ack", "outcome": "reject", "reason": "parroting",
                 "draft": "Bạn khoá dòng khi trừ tồn kho."},
                {"kind": "compose", "outcome": "fix", "reason": "extra_nhe",
                 "draft": "Giờ mình qua phần kỹ thuật nhé."},
            ],
            "new_terms": ["MVCC"],
        },
    ),
    _row(
        "s2",
        {
            "interviewer": "Mình hiểu hướng của bạn.\n\nVì sao bạn chọn Redis?",
            "ack_source": "llm",
            "speech_plan": [{"act": "ACK", "source": "llm"}, {"act": "QUESTION", "source": "llm"}],
            "speech_checks": [
                {"kind": "ack", "outcome": "pass"},
                {"kind": "probe", "outcome": "reject", "reason": "too_long", "draft": "Một câu rất dài?"},
                {"kind": "probe", "outcome": "pass", "reason": "generated"},
            ],
            "repeats_recent": True,
        },
    ),
    # Before 25/10: no checks recorded, payload stored as a JSON string.
    _row("s2", json.dumps({"interviewer": "Ừ, được.\n\nCâu tiếp?", "ack_source": "llm"}), turn=2),
]


def test_guard_table_counts_outcomes_per_kind() -> None:
    summary = summarize_speech(ROWS)
    assert summary["turns"] == {"count": 3, "sessions": 2, "withChecks": 2}
    guards = {g["kind"]: g for g in summary["guards"]}
    assert guards["ack"]["pass"] == 1 and guards["ack"]["reject"] == 1
    assert guards["ack"]["rejectRate"] == {"count": 1, "total": 2, "rate": 0.5}
    assert guards["probe"]["turns"] == 1 and guards["probe"]["reject"] == 1
    assert guards["compose"]["fix"] == 1 and guards["clarify"]["rejectRate"]["rate"] is None


def test_reasons_sources_style_and_recent_drafts() -> None:
    summary = summarize_speech(ROWS)
    assert {(r["kind"], r["reason"]) for r in summary["reasons"]} == {
        ("ack", "parroting"), ("compose", "extra_nhe"), ("probe", "too_long"),
    }
    assert summary["sources"]["ack"] == [{"source": "llm", "count": 2}, {"source": "fallback", "count": 1}]
    assert {"source": "pregenerated", "count": 1} in summary["sources"]["question"]
    style = summary["style"]
    assert style["overNhe"]["count"] == 1  # two "nhé" in the first reply
    assert style["interjectionOpenings"] == {"count": 2, "total": 3, "rate": 0.6667}
    assert style["repeatedAck"]["count"] == 1
    assert summary["newTerms"] == [{"term": "MVCC", "count": 1}]
    first = summary["recent"][0]
    assert first["sessionId"] == "s1" and first["draft"] == "Bạn khoá dòng khi trừ tồn kho."
    assert all(item["outcome"] != "pass" for item in summary["recent"])


def test_config_reads_the_switches(monkeypatch) -> None:
    monkeypatch.setenv("INTERVIEWER_ACK_MODE", "topic")
    monkeypatch.setenv("INTERVIEWER_VOICE_DIALECT", "nam")
    config = config_view()
    assert config["ackMode"] == "topic" and config["dialect"] == "nam"
    assert config["maxNhePerTurn"] == 1


def test_exchange_view_shows_speech_control() -> None:
    view = _exchange_view(
        {
            "created_at": datetime(2026, 10, 25, tzinfo=UTC),
            "payload": {
                "action": "PROBE",
                "ack_raw": "Câu trả lời rất tốt!",
                "speech_plan": [{"act": "ACK", "source": "fallback"}],
                "speech_checks": [{"kind": "ack", "outcome": "reject", "reason": "evaluative"}],
                "new_terms": ["TTL"],
            },
        }
    )
    assert view["ackRaw"] == "Câu trả lời rất tốt!"
    assert view["speechChecks"][0]["reason"] == "evaluative"
    assert view["speechPlan"] == [{"act": "ACK", "source": "fallback"}] and view["newTerms"] == ["TTL"]


def test_hand_checks_give_the_false_block_rate() -> None:
    from src.modules.interviews.application.speech_control_view import latest_verdicts

    rows = [{**row, "id": 10 + i} for i, row in enumerate(ROWS)]
    reviews = [
        {"payload": {"event_id": 10, "check_index": 0, "verdict": "wrong"}},
        {"payload": {"event_id": 10, "check_index": 1, "verdict": "correct"}},
        {"payload": {"event_id": 11, "check_index": 1, "verdict": "wrong"}},
        {"payload": json.dumps({"event_id": 11, "check_index": 1, "verdict": "clear"})},  # undone
    ]
    verdicts = latest_verdicts(reviews)
    assert verdicts == {(10, 0): "wrong", (10, 1): "correct"}
    summary = summarize_speech(rows, verdicts)
    assert summary["review"]["falseBlocks"] == {"count": 1, "total": 2, "rate": 0.5}
    assert {k["kind"] for k in summary["review"]["byKind"]} == {"ack", "compose"}
    first = summary["recent"][0]
    assert (first["eventId"], first["checkIndex"], first["verdict"]) == (10, 0, "wrong")
