"""Run the canonical JD parser against the repository's golden sets.

Usage from ``interview-prep-core``:
    python -m src.modules.job_descriptions.evaluation.runner
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.user_cvs.facade import SourceBlock, SourceDocument

DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[5] / "DOC_AND_PLAN" / "data" / "eval" / "jd"


def _source(case_id: str, raw_text: str) -> SourceDocument:
    cursor, blocks = 0, []
    for order, line in enumerate(raw_text.splitlines()):
        end = cursor + len(line)
        blocks.append(
            SourceBlock(f"line-{order:04d}", line, None, order, None, "text", cursor, end, "job_description")
        )
        cursor = end + 1
    return SourceDocument(case_id, hashlib.sha256(raw_text.encode()).hexdigest(), raw_text, tuple(blocks))


def _requirements(data: dict[str, Any]) -> Counter[tuple[Any, ...]]:
    result: Counter[tuple[Any, ...]] = Counter()
    for item in data["requirements"]:
        concept = item.get("concept") or {}
        result[
            (
                item["kind"],
                item["priority"],
                item["rawLabel"],
                concept.get("conceptId"),
                concept.get("label"),
                item.get("minimumExperienceMonths"),
            )
        ] += 1
    return result


def _classifications(data: dict[str, Any]) -> Counter[tuple[Any, ...]]:
    return Counter(
        (
            item["code"],
            item["label"],
            item["dimension"],
            item["taxonomyVersion"],
            item["isPrimary"],
            item["confidence"],
            item.get("assertionSource", "inferred"),
        )
        for item in data["careerClassifications"]
    )


def _counter_items(counter: Counter[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return sorted(counter.elements(), key=repr)


def _evidence_is_valid(data: dict[str, Any], raw_text: str) -> bool:
    evidence = {item["evidenceId"]: item for item in data["evidence"]}
    owners = [
        *data["responsibilities"],
        *data["requirements"],
        *data["benefits"],
        *data["careerClassifications"],
    ]
    return all(
        ref in evidence
        and raw_text[evidence[ref]["charStart"] : evidence[ref]["charEnd"]] == evidence[ref]["text"]
        for owner in owners
        for ref in owner["evidenceRefs"]
    )


def _compare(raw_text: str, gold: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    scalar_fields = ("jobTitle", "seniority", "employmentType", "workMode", "location")
    mismatches = {
        field: {"expected": gold[field], "actual": actual[field]}
        for field in scalar_fields
        if gold[field] != actual[field]
    }
    for field in ("responsibilities", "benefits"):
        expected = [item["text"] for item in gold[field]]
        observed = [item["text"] for item in actual[field]]
        if expected != observed:
            mismatches[field] = {"expected": expected, "actual": observed}
    expected_classifications, actual_classifications = _classifications(gold), _classifications(actual)
    if expected_classifications != actual_classifications:
        mismatches["careerClassifications"] = {
            "missing": _counter_items(expected_classifications - actual_classifications),
            "unexpected": _counter_items(actual_classifications - expected_classifications),
        }
    expected_requirements, actual_requirements = _requirements(gold), _requirements(actual)
    if expected_requirements != actual_requirements:
        mismatches["requirements"] = {
            "missing": _counter_items(expected_requirements - actual_requirements),
            "unexpected": _counter_items(actual_requirements - expected_requirements),
        }
    evidence_valid = _evidence_is_valid(actual, raw_text)
    if not evidence_valid:
        mismatches["evidence"] = {"error": "invalid offsets or unresolved references"}
    return {"passed": not mismatches, "evidence_valid": evidence_valid, "mismatches": mismatches}


def _load_golden_cases(dataset_dir: Path, manifest_name: str) -> list[dict[str, Any]]:
    manifest_path = dataset_dir / manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    for descriptor in manifest["files"]:
        path = dataset_dir / descriptor["path"]
        if not path.is_file():
            rel_path = (manifest_path.parent / descriptor["path"]).resolve()
            if rel_path.is_file():
                path = rel_path
            else:
                raise FileNotFoundError(f"Golden manifest references missing fixture: {path}")
        file_cases = json.loads(path.read_text(encoding="utf-8"))
        if len(file_cases) != descriptor["case_count"]:
            raise ValueError(
                f"{path.name}: expected {descriptor['case_count']} cases, found {len(file_cases)}"
            )
        cases.extend(file_cases)
    if manifest.get("total_cases") is not None and len(cases) != manifest["total_cases"]:
        raise ValueError(f"Golden manifest declares {manifest['total_cases']} cases, found {len(cases)}")
    case_ids = [case["case_id"] for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Golden fixture case_id values must be unique")
    for case in cases:
        CanonicalJobDescription.model_validate(case["expected"])
        if not _evidence_is_valid(case["expected"], case["raw_text"]):
            raise ValueError(f"{case['case_id']}: gold evidence does not resolve to raw_text")
    return cases


def run_golden(
    dataset_dir: Path = DEFAULT_DATASET_DIR, manifest_name: str = "manifest.json"
) -> dict[str, Any]:
    cases = _load_golden_cases(dataset_dir, manifest_name)
    parser = DeterministicJobDescriptionParser()
    results = []
    for case in cases:
        raw_text = case["raw_text"]
        parsed = parser.parse(_source(case["case_id"], raw_text), extraction_version="golden-eval-v1")
        actual = json.loads(parsed.model_dump_json(by_alias=True))
        result = _compare(raw_text, case["expected"], actual)
        results.append({"case_id": case["case_id"], **result})
    passed = sum(item["passed"] for item in results)
    return {
        "evaluation": f"jd-parser-{Path(manifest_name).stem}",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_dir": str(dataset_dir),
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "cases": results,
    }


def write_report(report: dict[str, Any], dataset_dir: Path) -> Path:
    """Persist an immutable report plus an append-only, compact run history."""
    reports_dir = dataset_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"jd_parser_golden_{timestamp}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_path.write_text(payload, encoding="utf-8")
    (reports_dir / "latest.json").write_text(payload, encoding="utf-8")
    history_entry = {
        "generated_at": report["generated_at"],
        "evaluation": report["evaluation"],
        "summary": report["summary"],
        "report": report_path.name,
    }
    with (reports_dir / "evaluation_history.jsonl").open("a", encoding="utf-8", newline="\n") as history:
        history.write(json.dumps(history_entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    return report_path


def main() -> None:
    command = argparse.ArgumentParser(description="Evaluate deterministic JD parser using golden fixtures.")
    command.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    command.add_argument("--manifest", default="manifest.json", help="Fixture manifest inside --dataset-dir.")
    command.add_argument(
        "--mode",
        choices=("exact", "semantic"),
        default="exact",
        help="exact compares canonical fields strictly; semantic tolerates paraphrases in the same manifest.",
    )
    command.add_argument(
        "--parser",
        choices=("deterministic", "hybrid"),
        default="deterministic",
        help="Parser under test. hybrid calls the configured OpenAI model in semantic mode.",
    )
    command.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Write hybrid evaluation progress every N cases (default: 10).",
    )
    command.add_argument(
        "--limit", type=int, help="Evaluate only the first N cases; use for model-service diagnostics."
    )
    command.add_argument(
        "--refresh-ai-cache",
        action="store_true",
        help="Force new OpenAI calls instead of reusing reproducible cached hybrid outputs.",
    )
    command.add_argument("--output", type=Path, help="Optional extra copy of the full report.")
    args = command.parse_args()
    if args.mode == "semantic":
        from src.modules.job_descriptions.evaluation.semantic_runner import (
            run_semantic,
            run_semantic_hybrid,
            write_semantic_report,
        )

        if args.limit is not None and args.limit < 1:
            command.error("--limit must be at least 1")
        report = (
            asyncio.run(
                run_semantic_hybrid(
                    args.dataset_dir,
                    args.manifest,
                    progress_every=args.progress_every,
                    limit=args.limit,
                    refresh_cache=args.refresh_ai_cache,
                )
            )
            if args.parser == "hybrid"
            else run_semantic(args.dataset_dir, args.manifest, limit=args.limit)
        )
        report_path = write_semantic_report(report, args.dataset_dir)
    else:
        if args.parser != "deterministic":
            command.error("--parser hybrid is currently supported only with --mode semantic")
        report = run_golden(args.dataset_dir, args.manifest)
        report_path = write_report(report, args.dataset_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report["summary"], "report": str(report_path)}))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    main()
