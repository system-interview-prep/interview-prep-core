"""Validate the matching golden set and persist a report for every run.

Usage from ``interview-prep-core``::

    python -m src.modules.matching.evaluation.runner

This runner deliberately reports golden-data integrity only. The current golden
proficiency labels do not yet share the production matcher's duration-based
contract, so presenting these checks as model accuracy would be misleading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.modules.matching.facade import MatchingFacade
from src.modules.matching.schemas import MatchRequest

DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[5] / "DOC_AND_PLAN" / "data" / "eval" / "matching"
DEFAULT_DATASET_FILE = "pair_core_v1/legacy/golden_matching_1to1_v1.json"
DEFAULT_MANIFEST_FILE = "pair_core_v1/legacy/manifest_conformance_v1.json"
FIT_LABELS = {0: "very_low", 1: "low", 2: "moderate", 3: "high", 4: "very_high"}
STATUS_LABELS = ("met", "not_met", "unknown")
ELIGIBILITY_LABELS = ("eligible", "ineligible", "review_required")


class _DisabledEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("semantic scoring disabled for reproducible structured evaluation")


def _case_errors(case: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    evidence_ids: dict[str, set[str]] = {}
    for side in ("cv", "jd"):
        document = case.get(side, {})
        raw_text = document.get("text", "")
        evidence = document.get("evidence", [])
        ids = [item.get("evidence_id") for item in evidence]
        evidence_ids[side] = set(ids)
        if len(ids) != len(set(ids)):
            errors.append(f"{side}:duplicate_evidence_id")
        for item in evidence:
            start, end = item.get("char_start"), item.get("char_end")
            offset_is_invalid = (
                not isinstance(start, int)
                or not isinstance(end, int)
                or raw_text[start:end] != item.get("text")
            )
            if offset_is_invalid:
                errors.append(f"{side}:invalid_evidence_offset:{item.get('evidence_id')}")

    expected = case.get("expected", {})
    for result in expected.get("requirement_results", []):
        requirement_id = result.get("requirement_id", "unknown")
        status = result.get("status")
        if status not in {"met", "not_met", "unknown"}:
            errors.append(f"requirement:{requirement_id}:invalid_status")
        if not set(result.get("evidence_refs", [])).issubset(evidence_ids.get("cv", set())):
            errors.append(f"requirement:{requirement_id}:unresolved_cv_evidence")
        if not set(result.get("jd_evidence_refs", [])).issubset(evidence_ids.get("jd", set())):
            errors.append(f"requirement:{requirement_id}:unresolved_jd_evidence")
        if status in {"met", "not_met"} and not result.get("evidence_refs"):
            errors.append(f"requirement:{requirement_id}:ungrounded_decision")

    must_statuses = {
        item.get("status")
        for item in expected.get("requirement_results", [])
        if item.get("priority") == "must_have"
    }
    derived_eligibility = (
        "ineligible"
        if "not_met" in must_statuses
        else "review_required"
        if "unknown" in must_statuses
        else "eligible"
    )
    if expected.get("eligibility") != derived_eligibility:
        errors.append("eligibility:tri_state_policy_mismatch")

    ordinal = expected.get("overall_fit_ordinal")
    if ordinal not in FIT_LABELS or expected.get("overall_fit_label") != FIT_LABELS.get(ordinal):
        errors.append("overall_fit:ordinal_label_mismatch")
    return sorted(set(errors))


def _load_cases(dataset_dir: Path, dataset_file: str, manifest_file: str = DEFAULT_MANIFEST_FILE) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cases = json.loads((dataset_dir / dataset_file).read_text(encoding="utf-8"))
    manifest = json.loads((dataset_dir / manifest_file).read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("matching golden dataset must be a JSON array")
    if len(cases) != manifest["case_count"]:
        raise ValueError("matching golden case count does not match manifest")
    case_ids = [case.get("case_id") for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("matching golden case_id values must be unique")
    return cases, manifest


def run_golden(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    dataset_file: str = DEFAULT_DATASET_FILE,
    manifest_file: str = DEFAULT_MANIFEST_FILE,
) -> dict[str, Any]:
    cases, manifest = _load_cases(dataset_dir, dataset_file, manifest_file)
    results = []
    languages: Counter[str] = Counter()
    eligibility: Counter[str] = Counter()
    requirement_statuses: Counter[str] = Counter()
    for case in cases:
        errors = _case_errors(case)
        evidence_valid = not any(
            "evidence" in error or "ungrounded_decision" in error for error in errors
        )
        results.append(
            {
                "case_id": case["case_id"],
                "passed": not errors,
                "evidence_valid": evidence_valid,
                "errors": errors,
            }
        )
        languages[case.get("metadata", {}).get("language", "unknown")] += 1
        eligibility[case.get("expected", {}).get("eligibility", "unknown")] += 1
        requirement_statuses.update(
            item.get("status", "unknown")
            for item in case.get("expected", {}).get("requirement_results", [])
        )

    passed = sum(item["passed"] for item in results)
    dataset_bytes = (dataset_dir / dataset_file).read_bytes()
    distributions_match_manifest = (
        dict(sorted(languages.items())) == manifest.get("language_distribution")
        and dict(sorted(eligibility.items())) == manifest.get("eligibility_distribution")
        and dict(sorted(requirement_statuses.items())) == manifest.get("requirement_status_distribution")
    )
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "case_valid_rate": round(passed / len(results), 4) if results else 0.0,
        "evidence_valid_rate": (
            round(sum(item["evidence_valid"] for item in results) / len(results), 4)
            if results
            else 0.0
        ),
        "language_distribution": dict(sorted(languages.items())),
        "eligibility_distribution": dict(sorted(eligibility.items())),
        "requirement_status_distribution": dict(sorted(requirement_statuses.items())),
    }
    checks = {
        "all_cases_valid": passed == len(results),
        "manifest_distributions_match": distributions_match_manifest,
        "no_obvious_contact_pii": re.search(
            rb"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:https?://|www\.)\S+", dataset_bytes, re.I
        )
        is None,
    }
    return {
        "evaluation": "matching-golden-integrity-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(dataset_dir / dataset_file),
            "version": manifest["dataset_version"],
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        },
        "metric_contract": {
            "scope": "golden integrity, evidence grounding, label-policy consistency and privacy smoke check",
            "production_benchmark": False,
            "limitation": "This mode validates fixtures and does not invoke MatchingFacade.",
        },
        "summary": summary,
        "quality_gate": {"passed": all(checks.values()), "checks": checks},
        "cases": results,
    }


def _evidence_payload(
    case: dict[str, Any], side: str, document_id: str, digest: str
) -> list[dict[str, Any]]:
    return [
        {
            "evidenceId": item["evidence_id"],
            "documentId": document_id,
            "documentSha256": digest,
            "section": item["section"],
            "text": item["text"],
            "charStart": item["char_start"],
            "charEnd": item["char_end"],
        }
        for item in case[side]["evidence"]
    ]


def _match_request(case: dict[str, Any]) -> MatchRequest:
    """Build production input without consulting any expected decision fields."""
    case_id = case["case_id"]
    cv_document_id, jd_document_id = f"{case_id}-cv", f"{case_id}-jd"
    cv_digest = hashlib.sha256(case["cv"]["text"].encode()).hexdigest()
    jd_digest = hashlib.sha256(case["jd"]["text"].encode()).hexdigest()
    matching_input = case["matching_input"]
    payload = {
        "schemaVersion": "2.1",
        "asyncProcessing": False,
        "resume": {
            "schemaVersion": "2.1",
            "resumeId": f"{case_id}-resume",
            "documentId": cv_document_id,
            "documentSha256": cv_digest,
            "documentLanguages": [case["metadata"]["language"]],
            "skills": [
                {
                    "claimId": f"claim-{index:03d}",
                    "concept": {
                        "conceptId": claim["concept_id"],
                        "scheme": "internal",
                        "taxonomyVersion": "matching-golden-v1.2",
                        "label": claim["label"],
                    },
                    "rawLabel": claim["raw_label"],
                    "proficiencyLevel": claim["proficiency_level"],
                    "evidenceRefs": claim["evidence_refs"],
                    "assertionSource": claim["assertion_source"],
                    "confidence": claim["confidence"],
                }
                for index, claim in enumerate(matching_input["resume_skill_claims"], 1)
            ],
            "evidence": _evidence_payload(case, "cv", cv_document_id, cv_digest),
        },
        "job": {
            "schemaVersion": "2.1",
            "jobId": f"{case_id}-job",
            "documentId": jd_document_id,
            "documentSha256": jd_digest,
            "requirements": [
                {
                    "requirementId": requirement["requirement_id"],
                    "type": "skill",
                    "priority": requirement["priority"],
                    "sourceEvidenceRef": requirement["source_evidence_ref"],
                    "skill": {
                        "conceptId": requirement["concept_id"],
                        "scheme": "internal",
                        "taxonomyVersion": "matching-golden-v1.2",
                        "label": requirement["label"],
                    },
                    "operator": "proficiency_gte",
                    "minimumProficiencyLevel": requirement["minimum_proficiency_level"],
                }
                for requirement in matching_input["job_requirements"]
            ],
            "evidence": _evidence_payload(case, "jd", jd_document_id, jd_digest),
        },
        "matchingPolicy": {"policyVersion": "balanced-v1"},
    }
    return MatchRequest.model_validate(payload)


def _classification_metrics(
    expected: list[str], predicted: list[str], labels: tuple[str, ...]
) -> dict[str, Any]:
    confusion = {
        actual: {prediction: 0 for prediction in labels}
        for actual in labels
    }
    for actual, prediction in zip(expected, predicted, strict=True):
        confusion[actual][prediction] += 1
    per_class = {}
    for label in labels:
        tp = confusion[label][label]
        fp = sum(confusion[actual][label] for actual in labels if actual != label)
        fn = sum(confusion[label][prediction] for prediction in labels if prediction != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
            "support": sum(confusion[label].values()),
        }
    total = len(expected)
    return {
        "accuracy": round(
            sum(
                actual == prediction
                for actual, prediction in zip(expected, predicted, strict=True)
            )
            / total,
            4,
        )
        if total
        else 0.0,
        "macro_f1": round(sum(item["f1"] for item in per_class.values()) / len(labels), 4),
        "per_class": per_class,
        "confusion_matrix": confusion,
    }


def run_production(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    dataset_file: str = DEFAULT_DATASET_FILE,
    manifest_file: str = DEFAULT_MANIFEST_FILE,
    *,
    semantic_mode: str = "disabled",
) -> dict[str, Any]:
    cases, manifest = _load_cases(dataset_dir, dataset_file, manifest_file)
    facade = MatchingFacade(_DisabledEmbedder()) if semantic_mode == "disabled" else MatchingFacade()
    expected_statuses: list[str] = []
    predicted_statuses: list[str] = []
    expected_eligibility: list[str] = []
    predicted_eligibility: list[str] = []
    evidence_tp = evidence_fp = evidence_fn = 0
    results = []
    language_totals: Counter[str] = Counter()
    language_status_correct: Counter[str] = Counter()
    language_eligibility_correct: Counter[str] = Counter()
    started_run = time.perf_counter()
    for case in cases:
        request = _match_request(case)
        started_case = time.perf_counter()
        actual = facade.match(request)
        latency_ms = (time.perf_counter() - started_case) * 1_000
        actual_by_id = {item.requirement_id: item for item in actual.requirement_results}
        case_expected_statuses = []
        case_predicted_statuses = []
        requirement_results = []
        for expected in case["expected"]["requirement_results"]:
            predicted = actual_by_id[expected["requirement_id"]]
            expected_statuses.append(expected["status"])
            predicted_statuses.append(predicted.status)
            case_expected_statuses.append(expected["status"])
            case_predicted_statuses.append(predicted.status)
            gold_refs, predicted_refs = set(expected["evidence_refs"]), set(predicted.evidence_refs)
            evidence_tp += len(gold_refs & predicted_refs)
            evidence_fp += len(predicted_refs - gold_refs)
            evidence_fn += len(gold_refs - predicted_refs)
            requirement_results.append(
                {
                    "requirement_id": expected["requirement_id"],
                    "expected_status": expected["status"],
                    "predicted_status": predicted.status,
                    "status_correct": expected["status"] == predicted.status,
                    "expected_evidence_refs": sorted(gold_refs),
                    "predicted_evidence_refs": sorted(predicted_refs),
                }
            )
        gold_eligibility = case["expected"]["eligibility"]
        expected_eligibility.append(gold_eligibility)
        predicted_eligibility.append(actual.eligibility)
        language = case["metadata"]["language"]
        language_totals[language] += len(case_expected_statuses)
        language_status_correct[language] += sum(
            gold == prediction
            for gold, prediction in zip(case_expected_statuses, case_predicted_statuses, strict=True)
        )
        language_eligibility_correct[language] += gold_eligibility == actual.eligibility
        results.append(
            {
                "case_id": case["case_id"],
                "language": language,
                "eligibility": {"expected": gold_eligibility, "predicted": actual.eligibility},
                "decision": actual.decision,
                "fit_band": actual.fit_band,
                "suitability_score": actual.suitability_score,
                "warnings": actual.warnings,
                "latency_ms": round(latency_ms, 3),
                "requirements": requirement_results,
            }
        )

    requirement_metrics = _classification_metrics(expected_statuses, predicted_statuses, STATUS_LABELS)
    eligibility_metrics = _classification_metrics(
        expected_eligibility, predicted_eligibility, ELIGIBILITY_LABELS
    )
    evidence_precision = evidence_tp / (evidence_tp + evidence_fp) if evidence_tp + evidence_fp else 0.0
    evidence_recall = evidence_tp / (evidence_tp + evidence_fn) if evidence_tp + evidence_fn else 0.0
    evidence_f1 = (
        2 * evidence_precision * evidence_recall / (evidence_precision + evidence_recall)
        if evidence_precision + evidence_recall
        else 0.0
    )
    non_review_results = [
        item for item in results if item["eligibility"]["expected"] != "review_required"
    ]
    unexpected_abstentions = sum(
        item["decision"] == "abstained" for item in non_review_results
    )
    actionable_coverage = (
        1.0 - unexpected_abstentions / len(non_review_results) if non_review_results else 0.0
    )
    checks = {
        "requirement_macro_f1": requirement_metrics["macro_f1"] >= 0.90,
        "eligibility_accuracy": eligibility_metrics["accuracy"] >= 0.90,
        "evidence_f1": evidence_f1 >= 0.90,
        "actionable_coverage": actionable_coverage >= 0.80,
    }
    total_cases = len(results)
    return {
        "evaluation": "matching-production-golden-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {"version": manifest["dataset_version"], "case_count": total_cases},
        "configuration": {
            "pipeline": "one-to-one-evidence-fusion-v1",
            "policy": "balanced-v1",
            "semantic_mode": semantic_mode,
            "input_contract": "oracle canonical matching inputs, independent from expected decisions",
        },
        "metric_contract": {
            "scope": "production MatchingFacade requirement, eligibility, evidence and abstention outputs",
            "production_benchmark": True,
            "interpretation": "Controlled component-conformance benchmark, not a generalization estimate.",
            "limitation": "Synthetic JDs and machine-generated labels require human adjudication.",
        },
        "summary": {
            "total_cases": total_cases,
            "total_requirements": len(expected_statuses),
            "requirement": requirement_metrics,
            "eligibility": eligibility_metrics,
            "evidence": {
                "precision": round(evidence_precision, 4),
                "recall": round(evidence_recall, 4),
                "f1": round(evidence_f1, 4),
            },
            "abstention_rate": round(
                sum(item["decision"] == "abstained" for item in results) / total_cases, 4
            ),
            "unexpected_abstentions": unexpected_abstentions,
            "actionable_coverage": round(actionable_coverage, 4),
            "mean_latency_ms": round(sum(item["latency_ms"] for item in results) / total_cases, 3),
            "total_latency_ms": round((time.perf_counter() - started_run) * 1_000, 3),
            "language_slices": {
                language: {
                    "requirement_accuracy": round(
                        language_status_correct[language] / language_totals[language], 4
                    ),
                    "eligibility_accuracy": round(
                        language_eligibility_correct[language]
                        / sum(case["metadata"]["language"] == language for case in cases),
                        4,
                    ),
                }
                for language in sorted(language_totals)
            },
        },
        "quality_gate": {
            "passed": all(checks.values()),
            "checks": checks,
            "thresholds": {
                "min_requirement_macro_f1": 0.90,
                "min_eligibility_accuracy": 0.90,
                "min_evidence_f1": 0.90,
                "min_actionable_coverage": 0.80,
            },
        },
        "cases": results,
    }
def write_report(report: dict[str, Any], dataset_dir: Path) -> Path:
    """Write an immutable report, latest pointer and append-only history entry."""
    reports_dir = dataset_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    prefix = (
        "matching_production"
        if report["evaluation"].startswith("matching-production")
        else "matching_golden"
    )
    report_path = reports_dir / f"{prefix}_{timestamp}.json"
    suffix = 1
    while report_path.exists():
        report_path = reports_dir / f"{prefix}_{timestamp}_{suffix}.json"
        suffix += 1
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_path.write_text(payload, encoding="utf-8")
    (reports_dir / "latest.json").write_text(payload, encoding="utf-8")
    history_entry = {
        "generated_at": report["generated_at"],
        "evaluation": report["evaluation"],
        "summary": report["summary"],
        "quality_gate_passed": report["quality_gate"]["passed"],
        "report": report_path.name,
    }
    with (reports_dir / "evaluation_history.jsonl").open("a", encoding="utf-8", newline="\n") as history:
        history.write(json.dumps(history_entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    return report_path


def main() -> None:
    command = argparse.ArgumentParser(description="Validate matching golden fixtures and write a report.")
    command.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    command.add_argument("--dataset-file", default=DEFAULT_DATASET_FILE)
    command.add_argument("--manifest-file", default=DEFAULT_MANIFEST_FILE)
    command.add_argument("--mode", choices=("integrity", "production"), default="integrity")
    command.add_argument(
        "--semantic",
        choices=("disabled", "live"),
        default="disabled",
        help=(
            "Production mode defaults to reproducible structured evaluation; "
            "live uses the configured provider."
        ),
    )
    command.add_argument("--output", type=Path, help="Optional extra copy of the full report.")
    args = command.parse_args()
    report = (
        run_production(args.dataset_dir, args.dataset_file, args.manifest_file, semantic_mode=args.semantic)
        if args.mode == "production"
        else run_golden(args.dataset_dir, args.dataset_file, args.manifest_file)
    )
    report_path = write_report(report, args.dataset_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "summary": report["summary"],
                "quality_gate": report["quality_gate"],
                "report": str(report_path),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["quality_gate"]["passed"] else 1)


if __name__ == "__main__":
    main()
