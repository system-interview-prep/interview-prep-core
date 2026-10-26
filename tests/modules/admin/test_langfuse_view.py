"""Langfuse data read back into the admin console."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.modules.admin import langfuse_view

T0 = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)


def _obs(id_, *, type_="GENERATION", start=T0, parent="root", trace="t1", **extra):
    base = {
        "id": id_,
        "trace_id": trace,
        "type": type_,
        "name": extra.pop("name", "evaluate_answer"),
        "parent_observation_id": parent,
        "start_time": start,
        "level": SimpleNamespace(value=extra.pop("level", "DEFAULT")),
        "usage_details": extra.pop("usage", {"input": 100, "output": 20}),
        "latency": extra.pop("latency", 1.5),
        "model": "gpt-5.4-mini",
        "total_cost": 0.001,
        "prompt_name": None,
        "input": extra.pop("input", None),
        "output": "ok",
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _score(name, value, trace="t1"):
    return SimpleNamespace(
        name=name,
        value=value,
        data_type="BOOLEAN",
        source=SimpleNamespace(value="API"),
        comment=None,
        timestamp=T0,
        subject=SimpleNamespace(kind="trace", id=trace),
    )


class _Api:
    def __init__(self, observations, scores):
        self.observations = SimpleNamespace(get_many=lambda **kw: SimpleNamespace(data=observations, kw=kw))
        self.scores = SimpleNamespace(get_many=lambda **kw: SimpleNamespace(data=scores))


@pytest.fixture(autouse=True)
def _langfuse(monkeypatch):
    langfuse_view.clear_cache()
    monkeypatch.setattr(langfuse_view.llm_tracing, "enabled", lambda: True)
    monkeypatch.setattr(langfuse_view.llm_tracing, "project_url", lambda: "https://lf/project/p1")


def _use(monkeypatch, observations, scores):
    monkeypatch.setattr(
        langfuse_view.llm_tracing, "client", lambda: SimpleNamespace(api=_Api(observations, scores))
    )


def test_trace_view_lists_calls_in_order_with_totals_and_scores(monkeypatch) -> None:
    later = datetime(2026, 10, 26, 5, 0, 2, tzinfo=UTC)
    _use(
        monkeypatch,
        [
            _obs("b", start=later, name="probe"),
            _obs("a", input="x" * 5000),
            _obs("root", type_="SPAN", parent=None),
        ],
        [_score("rule.flagged", 0), _score("judge.probe_ok", 1), _score("human.decision_ok", 1)],
    )
    view = asyncio.run(langfuse_view.trace_view("t1"))
    assert view["connected"] and not view["syncing"]
    assert view["traceUrl"] == "https://lf/project/p1/traces/t1"
    assert [o["id"] for o in view["observations"]] == ["a", "root", "b"]
    assert view["observations"][0]["input"].endswith(" …") and len(view["observations"][0]["input"]) < 4100
    assert view["totals"] == {"calls": 2, "tokens": 240, "latencyMs": 3000, "cost": 0.002}
    assert [s["kind"] for s in view["scores"]] == ["rule", "judge", "human"]


def test_a_turn_not_ingested_yet_is_syncing_not_an_error(monkeypatch) -> None:
    _use(monkeypatch, [], [])
    assert asyncio.run(langfuse_view.trace_view("t2"))["syncing"] is True


def test_langfuse_off_or_down_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(langfuse_view.llm_tracing, "enabled", lambda: False)
    assert asyncio.run(langfuse_view.trace_view("t3")) == {"connected": False, "reason": "not_configured"}

    def _boom():
        raise RuntimeError("503")

    monkeypatch.setattr(langfuse_view.llm_tracing, "enabled", lambda: True)
    monkeypatch.setattr(
        langfuse_view.llm_tracing,
        "client",
        lambda: SimpleNamespace(
            api=SimpleNamespace(observations=SimpleNamespace(get_many=lambda **kw: _boom()))
        ),
    )
    view = asyncio.run(langfuse_view.trace_view("t4"))
    assert view["connected"] is False and view["reason"] == "unreachable"


def test_session_view_groups_observations_by_trace(monkeypatch) -> None:
    _use(
        monkeypatch,
        [
            _obs("r1", type_="SPAN", parent=None, name="interview_turn", trace="t1"),
            _obs("g1", trace="t1"),
            _obs("r2", type_="SPAN", parent=None, name="session_grading", trace="t2", level="WARNING"),
            _obs("g2", trace="t2"),
            _obs("g3", trace="t2"),
        ],
        [],
    )
    view = asyncio.run(langfuse_view.session_view("s1"))
    traces = {t["traceId"]: t for t in view["traces"]}
    assert traces["t1"]["name"] == "interview_turn" and traces["t1"]["calls"] == 1
    assert traces["t2"]["calls"] == 2 and traces["t2"]["warnings"] == 1 and traces["t2"]["tokens"] == 240
    assert view["sessionUrl"] == "https://lf/project/p1/sessions/s1"


def test_flagged_turns_keep_trace_subjects_only(monkeypatch) -> None:
    session_score = _score("rule.flagged", 1)
    session_score.subject = SimpleNamespace(kind="session", id="s9")
    _use(monkeypatch, [], [_score("rule.flagged", 1, trace="t7"), session_score])
    assert [i["traceId"] for i in asyncio.run(langfuse_view.flagged_turns())["items"]] == ["t7"]


def test_review_writes_only_known_human_scores(monkeypatch) -> None:
    written = []
    monkeypatch.setattr(
        langfuse_view.llm_tracing,
        "score_trace",
        lambda trace_id, name, value, comment=None: written.append((trace_id, name, value, comment)),
    )
    asyncio.run(langfuse_view.record_review("t1", "human.probe_ok", False, "hỏi lệch ý thiếu"))
    assert written == [("t1", "human.probe_ok", 0.0, "hỏi lệch ý thiếu")]
    with pytest.raises(langfuse_view.ReviewScoreError):
        asyncio.run(langfuse_view.record_review("t1", "judge.probe_ok", True, None))
