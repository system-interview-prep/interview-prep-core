import json

from src.modules.matching.evaluation.runner import run_golden, run_production, write_report


def test_matching_golden_runner_reports_integrity_without_claiming_model_accuracy() -> None:
    report = run_golden()

    assert report["summary"]["total"] == 50
    assert report["summary"]["passed"] == 50
    assert report["summary"]["failed"] == 0
    assert report["quality_gate"]["passed"] is True
    assert report["metric_contract"]["production_benchmark"] is False
    assert len(report["dataset"]["sha256"]) == 64


def test_matching_report_writer_keeps_latest_immutable_runs_and_history(tmp_path) -> None:
    report = {
        "generated_at": "2026-09-12T00:00:00+00:00",
        "evaluation": "matching-golden-integrity-v1",
        "summary": {"total": 1, "passed": 1, "failed": 0},
        "quality_gate": {"passed": True, "checks": {"all_cases_valid": True}},
    }

    first = write_report(report, tmp_path)
    second = write_report(report, tmp_path)

    assert first != second
    assert first.is_file() and second.is_file()
    assert json.loads((tmp_path / "reports" / "latest.json").read_text(encoding="utf-8")) == report
    history = (tmp_path / "reports" / "evaluation_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 2
    assert all(json.loads(item)["quality_gate_passed"] is True for item in history)


def test_production_evaluation_runs_the_facade_for_all_golden_cases() -> None:
    report = run_production(semantic_mode="disabled")

    assert report["metric_contract"]["production_benchmark"] is True
    assert report["summary"]["total_cases"] == 50
    assert report["summary"]["total_requirements"] == 150
    assert report["summary"]["requirement"]["macro_f1"] == 1.0
    assert report["summary"]["eligibility"]["accuracy"] == 1.0
    assert report["summary"]["evidence"]["f1"] == 1.0
    assert report["summary"]["actionable_coverage"] == 1.0
    assert report["summary"]["unexpected_abstentions"] == 0
    assert report["quality_gate"]["checks"]["actionable_coverage"] is True
    assert report["quality_gate"]["passed"] is True
