import json

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.evaluation.runner import DEFAULT_DATASET_DIR, _evidence_is_valid, run_golden


def test_golden_runner_loads_every_case_and_returns_a_report() -> None:
    report = run_golden()

    manifest = json.loads((DEFAULT_DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    expected_total = sum(item["case_count"] for item in manifest["files"])
    assert report["summary"]["total"] == expected_total
    assert report["summary"]["passed"] + report["summary"]["failed"] == expected_total
    assert all("case_id" in item and "evidence_valid" in item for item in report["cases"])


def test_golden_fixtures_are_hand_authored_schema_valid_and_grounded() -> None:
    manifest = json.loads((DEFAULT_DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    cases = []
    for descriptor in manifest["files"]:
        file_cases = json.loads((DEFAULT_DATASET_DIR / descriptor["path"]).read_text(encoding="utf-8"))
        assert len(file_cases) == descriptor["case_count"]
        cases.extend(file_cases)

    assert len(cases) == manifest["total_cases"]
    assert len({case["case_id"] for case in cases}) == len(cases)
    for case in cases:
        assert case["metadata"].get("annotation_source") == "hand-authored" or case["metadata"].get("quality_tier") == "ai_adjudicated_golden"
        assert case["metadata"].get("review_status") == "verified" or case["metadata"].get("release_status") == "ready_for_human_signoff"
        CanonicalJobDescription.model_validate(case["expected"])
        assert _evidence_is_valid(case["expected"], case["raw_text"])
