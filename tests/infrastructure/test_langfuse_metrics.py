"""Chi phí LLM theo cách Langfuse tính (Metrics API)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from src.infrastructure.langfuse import connection as lf_client
from src.infrastructure.langfuse import metrics


def test_cost_feature_groups_trace_names() -> None:
    assert metrics.cost_feature(None) == "experiment"
    assert metrics.cost_feature("Execute evaluator: judge.probe_ok") == "judge"
    assert metrics.cost_feature("interview_turn") == "interview_turn"
    assert metrics.cost_feature("reflect") == "playbook"


def _row(cost, tokens, calls, **dims):
    return {**dims, "sum_totalCost": cost, "sum_totalTokens": tokens, "count_count": calls}


class _MetricsApi:
    def __init__(self) -> None:
        self.queries: list[dict] = []
        self.metrics = NS(metrics=self._metrics)

    def _metrics(self, *, query: str):
        body = json.loads(query)
        self.queries.append(body)
        dims = [d["field"] for d in body["dimensions"]]
        if body.get("timeDimension"):
            data = [
                _row(None, 0, 0, time_dimension="2026-10-27"),
                _row(0.03, 900, 4, time_dimension="2026-10-28"),
            ]
        elif dims == ["providedModelName"]:
            data = [
                _row(0.01, 100, 1, providedModelName="gpt-4.1"),
                _row(0.02, 800, 3, providedModelName="gpt-5.6-luna"),
            ]
        elif dims == ["traceName"]:
            data = [
                _row(0.01, 50, 1, traceName="Execute evaluator: judge.a"),
                _row(0.005, 50, 1, traceName="Execute evaluator: judge.b"),
                _row(0.015, 800, 2, traceName="interview_turn"),
            ]
        else:
            data = [_row(0.03, 900, 4)]
        return NS(data=data)


@pytest.mark.asyncio
async def test_cost_view_reads_langfuse_metrics(monkeypatch) -> None:
    api = _MetricsApi()
    monkeypatch.setattr(lf_client, "enabled", lambda: True)
    monkeypatch.setattr(lf_client, "client", lambda: NS(api=api))
    monkeypatch.setattr(lf_client, "project_url", lambda: "https://lf/project/p1")
    view = await metrics.cost_view(days=7, refresh=True)
    assert view["connected"] and view["total"] == {"cost": 0.03, "tokens": 900, "calls": 4}
    assert [row["model"] for row in view["byModel"]] == ["gpt-5.6-luna", "gpt-4.1"]
    judge = next(row for row in view["byFeature"] if row["feature"] == "judge")
    assert judge == {"feature": "judge", "cost": 0.015, "tokens": 100, "calls": 2}
    assert view["daily"][0] == {"day": "2026-10-27", "cost": 0.0, "calls": 0}
    assert all(q["filters"][0]["value"] == "GENERATION" for q in api.queries)


@pytest.mark.asyncio
async def test_cost_view_when_langfuse_is_down_or_off(monkeypatch) -> None:
    monkeypatch.setattr(lf_client, "enabled", lambda: False)
    view = await metrics.cost_view(days=1, refresh=True)
    assert view["connected"] is False and view["reason"] == "not_configured"
    monkeypatch.setattr(lf_client, "enabled", lambda: True)
    broken = NS(metrics=NS(metrics=lambda **_: (_ for _ in ()).throw(RuntimeError("down"))))
    monkeypatch.setattr(lf_client, "client", lambda: NS(api=broken))
    view = await metrics.cost_view(days=1, refresh=True)
    assert view["connected"] is False and view["reason"] == "unreachable" and "down" in view["error"]
