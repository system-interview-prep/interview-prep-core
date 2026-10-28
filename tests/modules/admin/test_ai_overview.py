"""Admin "Danh sách mô hình AI" and "Đánh giá AI" (src/modules/admin/ai_overview.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from src.core import llm_tracing
from src.modules.admin import ai_overview


def test_configured_models_follow_the_settings(monkeypatch) -> None:
    monkeypatch.setenv("LIVEKIT_STT_MODEL", "assemblyai/universal-3-6-pro")
    monkeypatch.setenv("VOICE_LAB_TTS_PROVIDER", "vieneu")
    rows = {row["capability"]: row for row in ai_overview.configured_models()}
    llm = ai_overview.get_settings().llm_model
    assert rows["INTERVIEW_TURN"]["model"] == llm and rows["INTERVIEW_TURN"]["provider"] == "openai"
    assert rows["INTERVIEW_SCORING"]["model"] == llm
    assert (rows["VOICE_STT"]["provider"], rows["VOICE_STT"]["model"]) == ("assemblyai", "universal-3-6-pro")
    assert rows["VOICE_TTS"]["provider"] == "vieneu"
    assert all("gemini" not in str(row["model"]).casefold() for row in rows.values())


def test_score_rates_handle_boolean_and_numeric_scores() -> None:
    scores = [
        NS(name="judge.probe_ok", value=True),
        NS(name="judge.probe_ok", value=False),
        NS(name="rule.flagged", value=1.0),
        NS(name="note", value="text"),
    ]
    rates = ai_overview.score_rates(scores)
    assert rates["judge.probe_ok"] == {"count": 2, "passed": 1.0, "rate": 0.5}
    assert rates["rule.flagged"]["rate"] == 1.0 and "note" not in rates


class _FakeApi:
    def __init__(self) -> None:
        self.scores = NS(get_many=self._scores)
        self.datasets = NS(
            list=lambda **_: NS(data=[NS(id="ds1", name="interview-turns-golden", description="")])
        )
        self.experiments = NS(
            list=lambda **_: NS(
                data=[
                    NS(
                        id="ex1",
                        name="moc-25-10",
                        item_count=16,
                        start_time=datetime(2026, 10, 25, tzinfo=UTC),
                        metadata={"model": "gpt-5.4-mini"},
                    )
                ]
            )
        )
        self.annotation_queues = NS(
            list_queue_items=lambda queue_id, status, limit: NS(
                meta=NS(total_items=3 if status == "PENDING" else 7)
            )
        )
        self.prompts = NS(list=lambda **_: NS(meta=NS(total_items=0)))

    @staticmethod
    def _scores(*, name=None, experiment_id=None, **_):
        if experiment_id:
            data = [NS(name="intent_match", value=True), NS(name="intent_match", value=False)]
        elif name == "judge.reply_in_scope":
            data = [NS(name=name, value=True)] * 3 + [NS(name=name, value=False)]
        elif name == "human.reply_in_scope":
            data = [NS(name=name, value=True)]
        else:
            data = []
        return NS(data=data, meta=NS(cursor=None))


@pytest.mark.asyncio
async def test_evaluation_view_summarises_the_langfuse_project(monkeypatch) -> None:
    monkeypatch.setattr(llm_tracing, "enabled", lambda: True)
    monkeypatch.setattr(llm_tracing, "client", lambda: NS(api=_FakeApi()))
    monkeypatch.setattr(llm_tracing, "project_url", lambda: "https://lf/project/p1")
    monkeypatch.setattr(ai_overview.get_settings(), "langfuse_review_queue_id", "q1")
    view = await ai_overview.evaluation_view(days=7, refresh=True)
    assert view["connected"] and view["links"]["datasets"] == "https://lf/project/p1/datasets"
    scope = next(c for c in view["quality"]["criteria"] if c["criterion"] == "reply_in_scope")
    assert scope["judge"]["rate"] == 0.75 and scope["human"]["rate"] == 1.0
    run = view["experiments"]["runs"][0]
    assert run["name"] == "moc-25-10" and run["rates"]["intent_match"]["rate"] == 0.5
    assert view["review"] == {"ok": True, "configured": True, "queueId": "q1", "pending": 3, "completed": 7}


@pytest.mark.asyncio
async def test_evaluation_view_without_langfuse(monkeypatch) -> None:
    monkeypatch.setattr(llm_tracing, "enabled", lambda: False)
    view = await ai_overview.evaluation_view(days=7, refresh=True)
    assert view["connected"] is False and view["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_a_failing_section_does_not_break_the_page(monkeypatch) -> None:
    api = _FakeApi()
    api.datasets = NS(list=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(llm_tracing, "enabled", lambda: True)
    monkeypatch.setattr(llm_tracing, "client", lambda: NS(api=api))
    monkeypatch.setattr(llm_tracing, "project_url", lambda: "https://lf/project/p1")
    view = await ai_overview.evaluation_view(days=7, refresh=True)
    assert view["experiments"]["ok"] is False and "boom" in view["experiments"]["error"]
    assert view["quality"]["ok"] is True


def test_cost_feature_groups_trace_names() -> None:
    assert ai_overview.cost_feature(None) == "experiment"
    assert ai_overview.cost_feature("Execute evaluator: judge.probe_ok") == "judge"
    assert ai_overview.cost_feature("interview_turn") == "interview_turn"
    assert ai_overview.cost_feature("reflect") == "playbook"


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
    monkeypatch.setattr(llm_tracing, "enabled", lambda: True)
    monkeypatch.setattr(llm_tracing, "client", lambda: NS(api=api))
    monkeypatch.setattr(llm_tracing, "project_url", lambda: "https://lf/project/p1")
    view = await ai_overview.cost_view(days=7, refresh=True)
    assert view["connected"] and view["total"] == {"cost": 0.03, "tokens": 900, "calls": 4}
    assert [row["model"] for row in view["byModel"]] == ["gpt-5.6-luna", "gpt-4.1"]
    judge = next(row for row in view["byFeature"] if row["feature"] == "judge")
    assert judge == {"feature": "judge", "cost": 0.015, "tokens": 100, "calls": 2}
    assert view["daily"][0] == {"day": "2026-10-27", "cost": 0.0, "calls": 0}
    assert all(q["filters"][0]["value"] == "GENERATION" for q in api.queries)


@pytest.mark.asyncio
async def test_cost_view_when_langfuse_is_down_or_off(monkeypatch) -> None:
    monkeypatch.setattr(llm_tracing, "enabled", lambda: False)
    view = await ai_overview.cost_view(days=1, refresh=True)
    assert view["connected"] is False and view["reason"] == "not_configured"
    monkeypatch.setattr(llm_tracing, "enabled", lambda: True)
    broken = NS(metrics=NS(metrics=lambda **_: (_ for _ in ()).throw(RuntimeError("down"))))
    monkeypatch.setattr(llm_tracing, "client", lambda: NS(api=broken))
    view = await ai_overview.cost_view(days=1, refresh=True)
    assert view["connected"] is False and view["reason"] == "unreachable" and "down" in view["error"]
