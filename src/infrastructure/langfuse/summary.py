"""The evaluation summary of the Langfuse project (LLM monitoring design, tiers 2-4).

Judge and reviewer pass rates, flagged turns, the review queue and the
golden-set runs, with links into Langfuse. Datasets, experiments, regressions
and prompt versions live in Langfuse itself.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.config import get_settings
from src.infrastructure.langfuse import connection as lf_client

GOLDEN_DATASET = "interview-turns-golden"
JUDGE_SCORES = ("judge.decision_ok", "judge.reply_in_scope", "judge.reply_neutral", "judge.probe_ok")
HUMAN_SCORES = (
    "human.decision_ok",
    "human.reply_in_scope",
    "human.reply_neutral",
    "human.probe_ok",
    "human.guard_correct",
)
RULE_SCORES = ("rule.flagged",)
SCORE_PAGE = 100
SCORE_MAX = 1000

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 60.0




def _section(fn, *args: Any) -> dict[str, Any]:
    """One Langfuse read; an error is reported in the section instead of failing the page."""
    try:
        return {"ok": True, **fn(*args)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}


def score_rates(scores: list[Any]) -> dict[str, dict[str, Any]]:
    """Share of true (or mean value) per score name: ``{name: {count, passed, rate}}``."""
    by_name: dict[str, list[float]] = {}
    for score in scores:
        value = getattr(score, "value", None)
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            by_name.setdefault(str(score.name), []).append(float(value))
    return {
        name: {
            "count": len(values),
            "passed": round(sum(values), 4),
            "rate": round(sum(values) / len(values), 4),
        }
        for name, values in by_name.items()
    }


def _scores(
    lf: Any, *, since: datetime, name: str | None = None, experiment_id: str | None = None
) -> list[Any]:
    found: list[Any] = []
    cursor = None
    while len(found) < SCORE_MAX:
        page = lf.api.scores.get_many(
            name=name,
            experiment_id=experiment_id,
            from_timestamp=since if not experiment_id else None,
            limit=SCORE_PAGE,
            cursor=cursor,
        )
        found.extend(page.data)
        cursor = getattr(page.meta, "cursor", None)
        if not cursor or not page.data:
            break
    return found


def _quality(lf: Any, since: datetime) -> dict[str, Any]:
    rates: dict[str, Any] = {}
    for name in (*JUDGE_SCORES, *HUMAN_SCORES, *RULE_SCORES):
        rates.update(score_rates(_scores(lf, since=since, name=name)))
    # Agreement of the judge with a reviewer on the same criterion.
    pairs = []
    for judge in JUDGE_SCORES:
        human = judge.replace("judge.", "human.")
        pairs.append(
            {"criterion": judge.removeprefix("judge."), "judge": rates.get(judge), "human": rates.get(human)}
        )
    return {"rates": rates, "criteria": pairs}


def _experiments(lf: Any) -> dict[str, Any]:
    datasets = lf.api.datasets.list(limit=50).data
    golden = next((d for d in datasets if d.name == GOLDEN_DATASET), None)
    runs: list[dict[str, Any]] = []
    if golden is not None:
        experiments = lf.api.experiments.list(
            from_start_time=datetime.now(UTC) - timedelta(days=365), dataset_id=golden.id, limit=10
        ).data
        for experiment in experiments:
            rates = score_rates(_scores(lf, since=experiment.start_time, experiment_id=experiment.id))
            runs.append(
                {
                    "id": experiment.id,
                    "name": experiment.name,
                    "startTime": experiment.start_time.isoformat() if experiment.start_time else None,
                    "items": experiment.item_count,
                    "model": (experiment.metadata or {}).get("model"),
                    "rates": rates,
                }
            )
    return {
        "datasets": [{"id": d.id, "name": d.name, "description": d.description} for d in datasets],
        "goldenDatasetId": golden.id if golden else None,
        "runs": runs,
    }


def _review(lf: Any) -> dict[str, Any]:
    queue_id = get_settings().langfuse_review_queue_id
    if not queue_id:
        return {"configured": False, "pending": None, "completed": None}
    pending = lf.api.annotation_queues.list_queue_items(queue_id, status="PENDING", limit=1).meta.total_items
    done = lf.api.annotation_queues.list_queue_items(queue_id, status="COMPLETED", limit=1).meta.total_items
    return {"configured": True, "queueId": queue_id, "pending": pending, "completed": done}


def _prompts(lf: Any) -> dict[str, Any]:
    return {"count": lf.api.prompts.list(limit=1).meta.total_items}


def _evaluation(days: int) -> dict[str, Any]:
    if not lf_client.enabled():
        return {"connected": False, "reason": "not_configured"}
    lf = lf_client.client()
    if lf is None:
        return {"connected": False, "reason": "client_unavailable"}
    project = lf_client.project_url()
    if not project:
        return {"connected": False, "reason": "unreachable"}
    since = datetime.now(UTC) - timedelta(days=days)
    return {
        "connected": True,
        "projectUrl": project,
        "links": {
            name: f"{project}/{path}"
            for name, path in (
                ("traces", "traces"),
                ("sessions", "sessions"),
                ("scores", "scores"),
                ("datasets", "datasets"),
                ("prompts", "prompts"),
                ("queues", "annotation-queues"),
                ("evaluators", "evals"),
                ("dashboards", "dashboards"),
            )
        },
        "quality": _section(_quality, lf, since),
        "experiments": _section(_experiments, lf),
        "review": _section(_review, lf),
        "prompts": _section(_prompts, lf),
    }


async def evaluation_view(*, days: int = 7, refresh: bool = False) -> dict[str, Any]:
    """The Langfuse summary, cached for a minute (each call makes about a dozen API reads)."""
    key = f"evaluation:{days}"
    with _cache_lock:
        cached = _cache.get(key)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    view = await asyncio.to_thread(_evaluation, days)
    view = {"windowDays": days, **view, "generatedAt": datetime.now(UTC).isoformat()}
    with _cache_lock:
        _cache[key] = (time.monotonic(), view)
    return view
