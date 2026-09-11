import json

from src.modules.job_descriptions.evaluation.runner import _source
from src.modules.job_descriptions.evaluation.semantic_runner import (
    _field_score,
    _hybrid_cache_path,
    _read_hybrid_cache,
    _similarity,
    _write_hybrid_cache,
)
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser


def test_similarity_is_paraphrase_tolerant_but_not_an_exact_match() -> None:
    assert _similarity("Build Python APIs", "Develop APIs using Python") > 0.5
    assert _similarity("Build Python APIs", "Manage payroll") == 0.0


def test_field_score_penalizes_missing_and_extra_facts() -> None:
    score = _field_score(["Python API development", "Docker deployment"], ["Python API development", "Java"])
    assert score["recall"] == 0.5
    assert score["precision"] == 0.5
    assert score["f1"] == 0.5


def test_hybrid_cache_round_trip_is_stable_and_grounded(tmp_path) -> None:
    raw_text = "Job Title: Engineer\nResponsibilities\n- Build APIs"
    parsed = DeterministicJobDescriptionParser().parse(_source("case-1", raw_text), extraction_version="test")
    path = _hybrid_cache_path(tmp_path, "case-1", raw_text)

    _write_hybrid_cache(path, parsed)
    cached = _read_hybrid_cache(path, raw_text)

    assert cached is not None
    assert json.loads(cached.model_dump_json(by_alias=True))["responsibilities"][0]["text"] == "Build APIs"
    assert path == _hybrid_cache_path(tmp_path, "case-1", raw_text)
    assert path != _hybrid_cache_path(tmp_path, "case-1", raw_text + " changed")
