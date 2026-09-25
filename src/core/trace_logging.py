"""Development-only structured traces for parser and matching workflows.

Trace records deliberately omit document text and parsed payloads because they
can contain candidate PII.  The records are JSON Lines so they can be tailed
locally or loaded into a log viewer.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.config import get_settings

logger = logging.getLogger(__name__)

_WORKFLOW_DIRECTORIES = {
    "cv_parser": "cv",
    "jd_parser": "jd",
    "matching": "matching",
}


def trace_event(workflow: str, event: str, **fields: Any) -> None:
    """Append one local-development trace event without interrupting work."""
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
    try:
        path = Path(settings.trace_logs_dir) / _WORKFLOW_DIRECTORIES.get(workflow, "system")
        path.mkdir(parents=True, exist_ok=True)
        with (path / "trace.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.exception("Unable to write local trace event")
