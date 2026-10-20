"""Development-only structured traces for parser and matching workflows.

Trace records deliberately omit document text and parsed payloads because they
can contain candidate PII.  The records are JSON Lines so they can be tailed
locally or loaded into a log viewer.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.core.pipeline_events import record_event

logger = logging.getLogger(__name__)

_WORKFLOW_DIRECTORIES = {
    "cv_parser": "cv",
    "cv_upload": "cv",
    "jd_parser": "jd",
    "matching": "matching",
    "clarification": "hoilai",
    "interviewer": "interview",
    "interview": "interview",
    # One record per interviewer turn, with the spoken text, for the live
    # monitor (scripts/watch_interviewer.py). Development only, like every trace.
    "naturalness": "naturalness",
    # Voice-room state changes and incidents reported by the LiveKit agent.
    "voice": "voice",
}


# Interview turns are followed live in the backend terminal; the app configures no
# logging handlers, so this logger owns its stdout handler.
_CONSOLE_WORKFLOWS = {"interviewer", "interview"}
_console = logging.getLogger("interview.trace")
if not _console.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s TRACE %(message)s", "%H:%M:%S"))
    _console.addHandler(_handler)
    _console.setLevel(logging.INFO)
    _console.propagate = False


def _echo_to_console(workflow: str, event: str, fields: dict[str, Any]) -> None:
    parts = [f"[{workflow}] {event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value)
        if key.endswith("_id") and len(text) > 8:
            text = text[:8]
        parts.append(f"{key}={text}")
    _console.info(" ".join(parts))


def trace_event(workflow: str, event: str, **fields: Any) -> None:
    """Append one local-development trace event without interrupting work.

    Interview events that name a session are also stored for the admin
    pipeline view in every environment (src/core/pipeline_events.py).
    """
    record_event(workflow, event, fields)
    settings = get_settings()
    enabled = settings.trace_logs_enabled
    if enabled is None:
        enabled = settings.app_env.casefold() in {"development", "local"}
    if not enabled:
        return

    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "workflow": workflow,
        "event": event,
        **fields,
    }
    if workflow in _CONSOLE_WORKFLOWS:
        _echo_to_console(workflow, event, fields)
    try:
        path = Path(settings.trace_logs_dir) / _WORKFLOW_DIRECTORIES.get(workflow, "system")
        path.mkdir(parents=True, exist_ok=True)
        with (path / "trace.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.exception("Unable to write local trace event")
