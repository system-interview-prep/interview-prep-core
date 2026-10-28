"""Admin "Danh sách mô hình AI" and "Đánh giá AI" pages.

* ``models_view``: which provider and model each AI feature uses, read from the
  backend's configuration (what will be used), next to what ``ai_call_logs``
  recorded in the last 30 days (what was actually used). Before 25/10 the page
  showed a hard-coded Gemini list that the backend never used.
* ``cost_view``: LLM cost as Langfuse prices it (Metrics API): total, per model,
  per feature, per day. Only calls traced to Langfuse are priced (interview
  turns, grading, judges, experiments), not OCR, embeddings or voice.
* ``evaluation_view``: a summary of the Langfuse project (LLM monitoring design,
  tiers 2–4): judge and reviewer pass rates, flagged turns, the review queue,
  the golden-set experiment runs, with links into Langfuse. Datasets,
  experiments, regressions and prompt versions live in Langfuse itself; the
  former placeholder pages for them redirect here.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import llm_tracing
from src.core.config import get_settings

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
CACHE_SECONDS = 60.0


# --------------------------------------------------------------------------- models


def configured_models() -> list[dict[str, Any]]:
    """Each AI feature with the provider and model the configuration selects."""
    s = get_settings()
    llm = {"provider": s.llm_provider or "openai", "model": s.llm_model, "source": "LLM_MODEL"}
    stt = os.getenv("LIVEKIT_STT_MODEL", "assemblyai/universal-3-6-pro").strip()
    stt_provider, _, stt_model = stt.partition("/")
    tts_provider = os.getenv("VOICE_LAB_TTS_PROVIDER", "elevenlabs").strip().lower()
    tts_model = {
        "vieneu": os.getenv("VIENEU_TTS_MODEL", "") or "VieNeu (máy chủ riêng)",
        "elevenlabs": os.getenv("ELEVENLABS_TTS_MODEL", "") or "ElevenLabs",
        "cartesia": os.getenv("CARTESIA_TTS_MODEL", "") or "Cartesia",
    }.get(tts_provider, tts_provider)
    ocr_model = s.mineru_model_version if s.ocr_provider == "mineru" else s.paddleocr_model
    rows: list[dict[str, Any]] = [
        {"capability": "INTERVIEW_TURN", **llm, "active": True},
        {"capability": "QUESTION_PREP", **llm, "active": True},
        {"capability": "INTERVIEW_SCORING", **llm, "active": True},
        {
            "capability": "VOICE_STT",
            "provider": stt_provider if stt_model else "livekit",
            "model": stt_model or stt,
            "source": "LIVEKIT_STT_MODEL",
            "active": True,
        },
        {
            "capability": "VOICE_TTS",
            "provider": tts_provider,
            "model": tts_model,
            "source": "VOICE_LAB_TTS_PROVIDER",
            "active": True,
        },
        {
            "capability": "DOCUMENT_OCR",
            "provider": s.ocr_provider,
            "model": ocr_model,
            "source": "OCR_PROVIDER",
            "active": True,
        },
        {
            "capability": "CV_PARSING",
            **llm,
            "source": "CV_PARSER_MODE",
            "mode": s.cv_parser_mode,
            "active": s.cv_parser_mode.casefold() == "hybrid",
        },
        {
            "capability": "JD_PARSING",
            **llm,
            "source": "JD_PARSER_MODE",
            "mode": s.jd_parser_mode,
            "active": s.jd_parser_mode.casefold() == "hybrid",
        },
        {
            "capability": "MATCHING_EMBEDDING",
            "provider": s.embedding_provider,
            "model": s.embedding_model,
            "source": "EMBEDDING_MODEL",
            "active": True,
        },
        {
            "capability": "MATCHING_EVIDENCE",
            **llm,
            "source": "MATCHING_LLM_EVIDENCE_SEARCH_ENABLED",
            "active": bool(s.matching_llm_evidence_search_enabled and s.openai_api_key),
        },
        {
            "capability": "MATCHING_CLARIFICATION",
            "provider": "openai",
            "model": s.matching_clarification_model,
            "source": "MATCHING_CLARIFICATION_MODEL",
            "active": bool(s.matching_clarification_questions_enabled),
        },
        {
            "capability": "MATCHING_JEV",
            "provider": "typesafe",
            "model": s.typesafe_default_model,
            "source": "JEV_CLARIFICATION_ENABLED",
            "active": bool(s.jev_clarification_enabled and s.typesafe_api_key),
        },
    ]
    return rows


async def models_view(db: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    try:
        result = await db.execute(
            text(
                """
                SELECT purpose, provider, model, COUNT(*) AS calls,
                       COUNT(*) FILTER (WHERE status = 'error') AS errors, MAX(created_at) AS last_at
                FROM ai_call_logs WHERE created_at >= :since
                GROUP BY purpose, provider, model ORDER BY calls DESC LIMIT 60
                """
            ),
            {"since": since},
        )
        observed = [
            {
                "purpose": row["purpose"],
                "provider": row["provider"],
                "model": row["model"],
                "calls": int(row["calls"]),
                "errors": int(row["errors"]),
                "lastAt": row["last_at"].isoformat() if row.get("last_at") else None,
            }
            for row in result.mappings().all()
        ]
    except Exception:  # noqa: BLE001 - ai_call_logs missing before migration 0029
        await db.rollback()
        observed = []
    return {
        "windowDays": days,
        "configured": configured_models(),
        "observed": observed,
        "generatedAt": datetime.now(UTC).isoformat(),
    }


# --------------------------------------------------------------------------- Langfuse

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


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
    if not llm_tracing.enabled():
        return {"connected": False, "reason": "not_configured"}
    lf = llm_tracing.client()
    if lf is None:
        return {"connected": False, "reason": "client_unavailable"}
    project = llm_tracing.project_url()
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
    if not llm_tracing.enabled():
        return {"connected": False, "reason": "not_configured"}
    lf = llm_tracing.client()
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
        "dashboardUrl": f"{project}/dashboards" if (project := llm_tracing.project_url()) else None,
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
