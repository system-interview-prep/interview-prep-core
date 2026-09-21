import asyncio
import json
import subprocess
import sys

import pytest

from src.modules.user_cvs.evaluation.dataset_validation import DEFAULT_DATASET_DIR, validate_cv_datasets
from src.modules.user_cvs.evaluation.runner import _load_cases, evaluate_cases
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser


def test_cv_golden_artifacts_are_private_grounded_and_unique() -> None:
    assert validate_cv_datasets() == {
        "skill_evidence_v1/candidates/pre_gold_cv_skill_evidence_v1.json": 300,
        "parser_core_v1/candidates/pre_gold_cv_parser_core_v1.json": 100,
        "parser_edge_v1/candidates/pre_gold_cv_parser_edge_v1.json": 40,
        "vi_translation_v1/candidates/pre_gold_cv_vi_translation_v1.json": 100,
    }


def test_cv_golden_builder_is_reproducible() -> None:
    workspace = DEFAULT_DATASET_DIR.parents[3]
    script = DEFAULT_DATASET_DIR / "code" / "build_cv_goldens.py"
    if not script.exists():
        pytest.skip("Golden builder script not found")
    completed = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 and "FileNotFoundError" in completed.stderr:
        pytest.skip("External raw dataset not present on host machine")
    assert completed.returncode == 0, completed.stderr


def test_all_cv_manifests_resolve_the_numbered_golden_files() -> None:
    expected_counts = {
        "parser_core_v1/manifests/manifest.json": 100,
        "parser_edge_v1/manifests/manifest.json": 40,
        "skill_evidence_v1/manifests/manifest.json": 300,
        "vi_translation_v1/manifests/manifest.json": 100,
    }
    for manifest_name, expected_count in expected_counts.items():
        cases, gates = _load_cases(DEFAULT_DATASET_DIR, manifest_name)
        assert len(cases) == expected_count
        assert gates


def test_vietnamese_cases_are_separately_addressable_but_pair_grouped() -> None:
    core = json.loads((DEFAULT_DATASET_DIR / "parser_core_v1/candidates/pre_gold_cv_parser_core_v1.json").read_text(encoding="utf-8"))
    translated = json.loads(
        (DEFAULT_DATASET_DIR / "vi_translation_v1/candidates/pre_gold_cv_vi_translation_v1.json").read_text(encoding="utf-8")
    )
    source_ids = {case["case_id"] for case in core}
    translated_ids = {case["case_id"] for case in translated}
    pair_ids = [case["metadata"]["paired_case_id"] for case in translated]
    group_ids = [case["metadata"]["translation_group_id"] for case in translated]

    assert len(source_ids) == len(translated_ids) == 100
    assert source_ids.isdisjoint(translated_ids)
    assert set(pair_ids) == source_ids
    assert len(pair_ids) == len(set(pair_ids))
    assert len(group_ids) == len(set(group_ids))
    assert all(case["metadata"]["language"] == "vi" for case in translated)
    assert all(case["metadata"]["statistically_independent_from_source"] is False for case in translated)
    assert all("Kỹ năng" in case["raw_text"] for case in translated)


def test_skill_evidence_gold_passes_deterministic_quality_gate() -> None:
    cases, gates = _load_cases(DEFAULT_DATASET_DIR, "skill_evidence_v1/manifests/manifest.json")
    report = asyncio.run(evaluate_cases(cases, DeterministicResumeParser(), quality_gates=gates))
    assert report["quality_gate"]["passed"]
