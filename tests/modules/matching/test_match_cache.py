# tests/modules/matching/test_match_cache.py
"""The same CV and JD reuse one stored match; any change to the request recomputes."""

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from src.modules.matching.application import match_cache


class _FakeDb:
    def __init__(self, store: dict, fail: bool = False):
        self.store = store
        self.fail = fail

    async def scalar(self, _sql, params):
        if self.fail:
            raise ConnectionError("database down")
        return self.store.get(params["key"])

    async def execute(self, _sql, params):
        if self.fail:
            raise ConnectionError("database down")
        self.store[params["key"]] = params["result"]

    async def commit(self):
        pass


def _factory(store, fail=False):
    @asynccontextmanager
    async def factory():
        yield _FakeDb(store, fail)

    return factory


def _request(raw_text: str):
    payload = MagicMock()
    payload.model_dump_json.return_value = f'{{"resume": "{raw_text}"}}'
    payload.resume.resume_id = "cv-1"
    payload.job.job_id = "jd-1"
    return payload


class _StubResult:
    """Stands in for MatchResult: carries its JSON text."""

    def __init__(self, raw: str):
        self.raw = raw

    def model_dump_json(self, **_kwargs) -> str:
        return self.raw

    @classmethod
    def model_validate_json(cls, raw: str) -> "_StubResult":
        return cls(raw)

    @classmethod
    def model_validate(cls, raw: str) -> "_StubResult":
        return cls(raw)


def _result(score: float) -> _StubResult:
    return _StubResult(f'{{"score": {score}}}')


@pytest.fixture(autouse=True)
def stub_result_model(monkeypatch):
    monkeypatch.setattr(match_cache, "MatchResult", _StubResult)
    monkeypatch.setattr(match_cache, "_PIPELINE_VERSION", "test")


@pytest.fixture
def stored_results(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(match_cache, "SessionFactory", _factory(store))
    return store


@pytest.mark.asyncio
async def test_second_match_for_same_request_is_served_from_cache(stored_results):
    calls = []

    async def compute(payload):
        calls.append(payload)
        return _result(0.72)

    first = await match_cache.cached_match(_request("same cv"), compute)
    second = await match_cache.cached_match(_request("same cv"), compute)
    assert len(calls) == 1
    assert first.model_dump_json() == second.model_dump_json() == '{"score": 0.72}'


@pytest.mark.asyncio
async def test_changed_cv_is_matched_again(stored_results):
    calls = []

    async def compute(payload):
        calls.append(payload)
        return _result(0.5)

    await match_cache.cached_match(_request("cv v1"), compute)
    await match_cache.cached_match(_request("cv v2"), compute)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_degraded_match_is_not_cached(stored_results):
    calls = []

    async def compute(payload):
        calls.append(payload)
        # The LLM evidence search failed inside matching.
        match_cache.evidence_search_failed.set(True)
        return _result(0.4)

    await match_cache.cached_match(_request("cv"), compute)
    await match_cache.cached_match(_request("cv"), compute)
    assert len(calls) == 2
    assert stored_results == {}
    assert match_cache.evidence_search_failed.get() is False


@pytest.mark.asyncio
async def test_matching_still_works_when_the_cache_is_down(monkeypatch):
    monkeypatch.setattr(match_cache, "SessionFactory", _factory({}, fail=True))

    async def compute(payload):
        return _result(0.6)

    result = await match_cache.cached_match(_request("cv"), compute)
    assert result.model_dump_json() == '{"score": 0.6}'
