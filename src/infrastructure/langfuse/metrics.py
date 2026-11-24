"""LLM cost, as Langfuse prices it (Metrics API).

Only calls traced to Langfuse are priced - interview turns, grading, judges
and golden-set runs - not OCR, embeddings or voice.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from src.infrastructure.langfuse import connection as lf_client

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 60.0


def cost_feature(trace_name: str | None) -> str:
    """Group a Langfuse trace name into the feature it pays for."""
    if not trace_name:
        # Golden-set runs (scripts/langfuse_experiment.py) carry no trace name.
        return "experiment"
    if trace_name.startswith("Execute evaluator:"):
        return "judge"
    return {"interview_turn": "interview_turn", "session_grading": "session_grading"}.get(
        trace_name, "playbook" if trace_name in {"reflect", "run"} else trace_name
    )


def _cost_query(
    lf: Any, since: datetime, until: datetime, dimensions: list[str], granularity: str | None = None
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {
        "view": "observations",
        "dimensions": [{"field": d} for d in dimensions],
        "metrics": [
            {"measure": "totalCost", "aggregation": "sum"},
            {"measure": "totalTokens", "aggregation": "sum"},
            {"measure": "count", "aggregation": "count"},
        ],
        "filters": [{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}],
        "fromTimestamp": since.isoformat(),
        "toTimestamp": until.isoformat(),
    }
    if granularity:
        body["timeDimension"] = {"granularity": granularity}
    rows = lf.api.metrics.metrics(query=json.dumps(body)).data
    return [
        {
            **{d: row.get(d) for d in dimensions},
            **({"day": str(row.get("time_dimension") or "")[:10]} if granularity else {}),
            "cost": round(float(row.get("sum_totalCost") or 0), 6),
            "tokens": int(row.get("sum_totalTokens") or 0),
            "calls": int(row.get("count_count") or 0),
        }
        for row in rows
    ]


def _cost(days: int) -> dict[str, Any]:
    if not lf_client.enabled():
        return {"connected": False, "reason": "not_configured"}
    lf = lf_client.client()
    if lf is None:
        return {"connected": False, "reason": "client_unavailable"}
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    total = (_cost_query(lf, since, until, []) or [{"cost": 0.0, "tokens": 0, "calls": 0}])[0]
    by_model = [
        {"model": row.pop("providedModelName"), **row}
        for row in _cost_query(lf, since, until, ["providedModelName"])
    ]
    features: dict[str, dict[str, Any]] = {}
    for row in _cost_query(lf, since, until, ["traceName"]):
        key = cost_feature(row["traceName"])
        item = features.setdefault(key, {"feature": key, "cost": 0.0, "tokens": 0, "calls": 0})
        item["cost"] = round(item["cost"] + row["cost"], 6)
        item["tokens"] += row["tokens"]
        item["calls"] += row["calls"]
    daily = [
        {"day": row["day"], "cost": row["cost"], "calls": row["calls"]}
        for row in _cost_query(lf, since, until, [], granularity="day")
    ]
    by_cost = lambda row: -row["cost"]  # noqa: E731
    return {
        "connected": True,
        "currency": "USD",
        "total": {"cost": total["cost"], "tokens": total["tokens"], "calls": total["calls"]},
        "byModel": sorted(by_model, key=by_cost),
        "byFeature": sorted(features.values(), key=by_cost),
        "daily": sorted(daily, key=lambda row: row["day"]),
        "dashboardUrl": f"{project}/dashboards" if (project := lf_client.project_url()) else None,
    }


async def cost_view(*, days: int = 7, refresh: bool = False) -> dict[str, Any]:
    """LLM cost as Langfuse prices it (Metrics API), cached for a minute. Never raises."""
    key = f"cost:{days}"
    with _cache_lock:
        cached = _cache.get(key)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        view = await asyncio.to_thread(_cost, days)
    except Exception as exc:  # noqa: BLE001 - Langfuse down must not break the page
        error = f"{type(exc).__name__}: {exc}"[:240]
        return {"windowDays": days, "connected": False, "reason": "unreachable", "error": error}
    view = {"windowDays": days, **view, "generatedAt": datetime.now(UTC).isoformat()}
    with _cache_lock:
        _cache[key] = (time.monotonic(), view)
    return view
