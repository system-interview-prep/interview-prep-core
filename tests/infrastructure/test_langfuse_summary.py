"""Tóm tắt dự án Langfuse: tỉ lệ đạt của giám khảo và người soát, hàng đợi, bộ đề."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from src.infrastructure.langfuse import connection as lf_client
from src.infrastructure.langfuse import summary


def test_score_rates_handle_boolean_and_numeric_scores() -> None:
    scores = [
        NS(name="judge.probe_ok", value=True),
        NS(name="judge.probe_ok", value=False),
        NS(name="rule.flagged", value=1.0),
        NS(name="note", value="text"),
    ]
    rates = summary.score_rates(scores)
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
    monkeypatch.setattr(lf_client, "enabled", lambda: True)
    monkeypatch.setattr(lf_client, "client", lambda: NS(api=_FakeApi()))
    monkeypatch.setattr(lf_client, "project_url", lambda: "https://lf/project/p1")
    monkeypatch.setattr(summary.get_settings(), "langfuse_review_queue_id", "q1")
    view = await summary.evaluation_view(days=7, refresh=True)
    assert view["connected"] and view["links"]["datasets"] == "https://lf/project/p1/datasets"
    scope = next(c for c in view["quality"]["criteria"] if c["criterion"] == "reply_in_scope")
    assert scope["judge"]["rate"] == 0.75 and scope["human"]["rate"] == 1.0
    run = view["experiments"]["runs"][0]
    assert run["name"] == "moc-25-10" and run["rates"]["intent_match"]["rate"] == 0.5
    assert view["review"] == {"ok": True, "configured": True, "queueId": "q1", "pending": 3, "completed": 7}


@pytest.mark.asyncio
async def test_evaluation_view_without_langfuse(monkeypatch) -> None:
    monkeypatch.setattr(lf_client, "enabled", lambda: False)
    view = await summary.evaluation_view(days=7, refresh=True)
    assert view["connected"] is False and view["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_a_failing_section_does_not_break_the_page(monkeypatch) -> None:
    api = _FakeApi()
    api.datasets = NS(list=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(lf_client, "enabled", lambda: True)
    monkeypatch.setattr(lf_client, "client", lambda: NS(api=api))
    monkeypatch.setattr(lf_client, "project_url", lambda: "https://lf/project/p1")
    view = await summary.evaluation_view(days=7, refresh=True)
    assert view["experiments"]["ok"] is False and "boom" in view["experiments"]["error"]
    assert view["quality"]["ok"] is True
