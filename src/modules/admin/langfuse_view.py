"""Langfuse data shown inside the admin console ("coi được 2 bên").

The interview already sends every turn and every grading run to Langfuse
(``src/core/llm_tracing.py``). This module reads it back through the Langfuse
public API so an admin sees, on our own pages:

* ``trace_view``: one turn's model calls (step, model, latency, tokens, cost,
  input and output when the turn carried text) and its scores (rules, AI
  judges, reviewers);
* ``session_view``: every trace of one interview with tokens and cost, and totals;
* ``flagged_turns``: the latest turns a rule flagged, mapped to their session;
* ``record_review``: a reviewer's verdict written back to Langfuse as a
  ``human.*`` score, the same names the annotation queue and judges use.

Reads run in a worker thread (the SDK is synchronous) with a short cache.
Data reaches the API 15–30 s after ingestion, so an empty trace is reported
as ``syncing`` rather than as an error. Never raises: Langfuse off or down
gives ``{"connected": False, "reason": ...}``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core import llm_tracing

CACHE_SECONDS = 30.0
IO_MAX_CHARS = 4000
OBSERVATION_LIMIT = 200
# Reviewer verdicts written from our pages; same names as the annotation queue
# score configs (scripts/langfuse_setup.py) so Langfuse compares judge and human.
HUMAN_SCORE_NAMES = (
    "human.decision_ok",
    "human.reply_in_scope",
    "human.reply_neutral",
    "human.probe_ok",
)
_OBS_FIELDS = "core,basic,time,io,model,usage,prompt,metrics"

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, build: Any) -> Any:
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = build()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def clear_cache(prefix: str = "") -> None:
    with _cache_lock:
        for key in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(key, None)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _clip(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= IO_MAX_CHARS else text[:IO_MAX_CHARS] + " …"


def _client() -> tuple[Any, dict[str, Any] | None]:
    if not llm_tracing.enabled():
        return None, {"connected": False, "reason": "not_configured"}
    lf = llm_tracing.client()
    if lf is None:
        return None, {"connected": False, "reason": "client_unavailable"}
    return lf, None


def _level(obs: Any) -> str | None:
    level = getattr(obs, "level", None)
    return str(getattr(level, "value", level) or "").upper() or None


def observation_view(obs: Any) -> dict[str, Any]:
    """One model call or span, flattened for the admin page."""
    usage = getattr(obs, "usage_details", None) or {}
    latency = getattr(obs, "latency", None)
    return {
        "id": obs.id,
        "parentId": getattr(obs, "parent_observation_id", None),
        "type": getattr(obs, "type", None),
        "name": getattr(obs, "name", None),
        "level": _level(obs),
        "statusMessage": getattr(obs, "status_message", None),
        "model": getattr(obs, "model", None),
        "startTime": _iso(getattr(obs, "start_time", None)),
        "latencyMs": round(float(latency) * 1000) if latency is not None else None,
        "inputTokens": usage.get("input"),
        "outputTokens": usage.get("output"),
        "totalTokens": usage.get("total")
        or ((usage.get("input") or 0) + (usage.get("output") or 0) if usage else None),
        "cost": getattr(obs, "total_cost", None),
        "prompt": (f"{obs.prompt_name}@{obs.prompt_version}" if getattr(obs, "prompt_name", None) else None),
        "input": _clip(getattr(obs, "input", None)),
        "output": _clip(getattr(obs, "output", None)),
    }


def score_view(score: Any) -> dict[str, Any]:
    source = getattr(score, "source", None)
    return {
        "name": score.name,
        "value": getattr(score, "value", None),
        "dataType": getattr(score, "data_type", None),
        "source": getattr(source, "value", source),
        "comment": getattr(score, "comment", None),
        "at": _iso(getattr(score, "timestamp", None)),
    }


def score_kind(name: str) -> str:
    """rule / judge / human / other, from the score name prefix."""
    head = name.split(".", 1)[0]
    return head if head in {"rule", "flag", "judge", "human"} else "other"


def _trace(trace_id: str) -> dict[str, Any]:
    lf, off = _client()
    if off:
        return off
    observations = lf.api.observations.get_many(
        trace_id=trace_id, fields=_OBS_FIELDS, limit=OBSERVATION_LIMIT
    ).data
    scores = lf.api.scores.get_many(trace_id=trace_id, fields="details", limit=100).data
    observations = sorted(
        observations, key=lambda o: getattr(o, "start_time", None) or datetime.min.replace(tzinfo=UTC)
    )
    project = llm_tracing.project_url()
    views = [observation_view(o) for o in observations]
    calls = [v for v in views if v["type"] == "GENERATION"]
    return {
        "connected": True,
        "traceId": trace_id,
        "traceUrl": f"{project}/traces/{trace_id}" if project else None,
        # Ingestion lags 15-30 s: a turn that just happened is not there yet.
        "syncing": not observations,
        "observations": views,
        "totals": {
            "calls": len(calls),
            "tokens": sum(v["totalTokens"] or 0 for v in calls),
            "latencyMs": sum(v["latencyMs"] or 0 for v in calls),
            "cost": round(sum(float(v["cost"] or 0) for v in calls), 6),
        },
        "scores": [{**score_view(s), "kind": score_kind(s.name)} for s in scores],
        "humanScoreNames": list(HUMAN_SCORE_NAMES),
    }


def _session(session_id: str) -> dict[str, Any]:
    lf, off = _client()
    if off:
        return off
    observations = lf.api.observations.get_many(
        session_id=session_id, fields="core,basic,usage,metrics", limit=OBSERVATION_LIMIT
    ).data
    scores = lf.api.scores.get_many(session_id=session_id, fields="details", limit=100).data
    traces: dict[str, dict[str, Any]] = {}
    for obs in observations:
        item = traces.setdefault(
            obs.trace_id,
            {
                "traceId": obs.trace_id,
                "name": None,
                "startTime": None,
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
                "warnings": 0,
            },
        )
        if getattr(obs, "is_root_observation", False) or getattr(obs, "parent_observation_id", None) is None:
            item["name"] = item["name"] or getattr(obs, "name", None)
        start = _iso(getattr(obs, "start_time", None))
        if start and (item["startTime"] is None or start < item["startTime"]):
            item["startTime"] = start
        if getattr(obs, "type", None) == "GENERATION":
            item["calls"] += 1
            usage = getattr(obs, "usage_details", None) or {}
            item["tokens"] += usage.get("total") or (usage.get("input") or 0) + (usage.get("output") or 0)
            item["cost"] = round(item["cost"] + float(getattr(obs, "total_cost", None) or 0), 6)
        if _level(obs) in {"WARNING", "ERROR"}:
            item["warnings"] += 1
    project = llm_tracing.project_url()
    return {
        "connected": True,
        "sessionUrl": f"{project}/sessions/{session_id}" if project else None,
        "traces": sorted(traces.values(), key=lambda t: t["startTime"] or ""),
        "totals": {
            "calls": sum(t["calls"] for t in traces.values()),
            "tokens": sum(t["tokens"] for t in traces.values()),
            "cost": round(sum(t["cost"] for t in traces.values()), 6),
        },
        "scores": [{**score_view(s), "kind": score_kind(s.name)} for s in scores],
        "syncing": not observations,
    }


def _flagged(days: int, limit: int) -> dict[str, Any]:
    lf, off = _client()
    if off:
        return off
    since = datetime.now(UTC) - timedelta(days=days)
    # Filter the value here: value filters on BOOLEAN scores are rejected by
    # some Langfuse versions, and a week of turns fits in one page.
    page = lf.api.scores.get_many(name="rule.flagged", from_timestamp=since, fields="subject", limit=100)
    items: list[dict[str, Any]] = []
    for score in page.data:
        if getattr(score, "value", None) not in (1, 1.0, True, "1", "true"):
            continue
        if len(items) >= limit:
            break
        subject = getattr(score, "subject", None)
        kind = getattr(subject, "kind", None)
        trace_id = getattr(subject, "trace_id", None) or (
            getattr(subject, "id", None) if kind == "trace" else None
        )
        if trace_id:
            items.append({"traceId": trace_id, "at": _iso(getattr(score, "timestamp", None))})
    return {"connected": True, "items": items}


async def _run(key: str, build: Any) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_cached, key, build)
    except Exception as exc:  # noqa: BLE001 - Langfuse down must not break the page
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        detail = f"HTTP {status}: {body}" if status else f"{type(exc).__name__}: {exc}"
        return {"connected": False, "reason": "unreachable", "error": detail[:300]}


async def trace_view(trace_id: str) -> dict[str, Any]:
    return await _run(f"trace:{trace_id}", lambda: _trace(trace_id))


async def session_view(session_id: str) -> dict[str, Any]:
    return await _run(f"session:{session_id}", lambda: _session(session_id))


async def flagged_turns(days: int = 7, limit: int = 20) -> dict[str, Any]:
    return await _run(f"flagged:{days}:{limit}", lambda: _flagged(days, limit))


class ReviewScoreError(ValueError):
    pass


async def record_review(trace_id: str, name: str, value: bool, comment: str | None) -> None:
    """Write a reviewer verdict on a turn as a Langfuse ``human.*`` score."""
    if name not in HUMAN_SCORE_NAMES:
        raise ReviewScoreError("unknown_score")
    if not llm_tracing.enabled():
        raise ReviewScoreError("langfuse_off")
    await asyncio.to_thread(
        llm_tracing.score_trace, trace_id, name, 1.0 if value else 0.0, comment=(comment or None)
    )
    clear_cache(f"trace:{trace_id}")
