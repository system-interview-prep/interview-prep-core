import asyncio
import subprocess
import sys
from pathlib import Path

from src.modules.user_cvs.evaluation.dataset_validation import DEFAULT_DATASET_DIR, validate_cv_datasets
from src.modules.user_cvs.evaluation.runner import _load_cases, evaluate_cases
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser


def test_cv_golden_artifacts_are_private_grounded_and_unique() -> None:
    assert validate_cv_datasets() == {
        "golden_cv_skill_evidence_v1.json": 96,
        "golden_cv_parser_core_v1.json": 42,
        "golden_cv_parser_edge_v1.json": 24,
        "datasetmaster_resume_silver_v1.json": 160,
    }


def test_cv_golden_builder_is_reproducible() -> None:
    workspace = DEFAULT_DATASET_DIR.parents[3]
    script = DEFAULT_DATASET_DIR / "build_cv_goldens.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_skill_evidence_gold_passes_deterministic_quality_gate() -> None:
    cases, gates = _load_cases(DEFAULT_DATASET_DIR, "skill_evidence_manifest.json")
    report = asyncio.run(evaluate_cases(cases, DeterministicResumeParser(), quality_gates=gates))
    assert report["quality_gate"]["passed"]
