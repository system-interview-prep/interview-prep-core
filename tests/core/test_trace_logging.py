import json
from pathlib import Path

from src.core.config import get_settings
from src.core.trace_logging import _WORKFLOW_DIRECTORIES, trace_event


def test_interviewer_workflow_directory_mapping():
    assert _WORKFLOW_DIRECTORIES.get("interviewer") == "interview"
    assert _WORKFLOW_DIRECTORIES.get("interview") == "interview"


def test_trace_event_interviewer(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "trace_logs_enabled", True)
    monkeypatch.setattr(settings, "trace_logs_dir", str(tmp_path))

    trace_event(
        "interviewer",
        "session_created",
        session_id="test-session-123",
        mode="text",
        duration_minutes=25,
    )

    log_file = tmp_path / "interview" / "trace.jsonl"
    assert log_file.exists()

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["workflow"] == "interviewer"
    assert record["event"] == "session_created"
    assert record["session_id"] == "test-session-123"
    assert record["mode"] == "text"
    assert record["duration_minutes"] == 25
    assert "timestamp" in record
