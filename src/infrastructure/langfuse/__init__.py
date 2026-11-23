"""Everything this system does with Langfuse, in one place.

Nothing outside this package imports the ``langfuse`` SDK or calls its REST API.
The rest of the code talks to Langfuse only through the names below, and every
one of them is a no-op when Langfuse is not configured.

    connection.py is there a client at all; the project URL for admin links
    privacy.py   what is stripped from a payload, and how much is sent
    tracing.py   a trace per interview turn and per grading run, plus scores
    reader.py    reading traces, sessions, scores and flagged turns back out
    metrics.py   LLM cost, as Langfuse prices it
    summary.py   the evaluation summary of the project (judges, reviewers, runs)
    health.py    whether the service answers
    links.py     deep links into the Langfuse UI
    router.py    the admin HTTP endpoints that serve the pages above
"""

from src.infrastructure.langfuse.connection import (
    client,
    enabled,
    flush,
    project_url,
    use_client,
)
from src.infrastructure.langfuse.privacy import (
    hashed_user,
    mask,
    register_names,
    share,
    text_sampled,
)
from src.infrastructure.langfuse.tracing import (
    GRADING_TRACE,
    STEP_BY_CALLER,
    TURN_TRACE,
    Generation,
    TraceState,
    add_flag,
    current,
    current_trace_id,
    finish,
    prompt_version,
    score_session,
    score_trace,
    step_of,
    trace,
)

__all__ = [
    "GRADING_TRACE",
    "STEP_BY_CALLER",
    "TURN_TRACE",
    "Generation",
    "TraceState",
    "add_flag",
    "client",
    "current",
    "current_trace_id",
    "enabled",
    "finish",
    "flush",
    "hashed_user",
    "mask",
    "project_url",
    "prompt_version",
    "register_names",
    "score_session",
    "score_trace",
    "share",
    "step_of",
    "text_sampled",
    "trace",
    "use_client",
]
