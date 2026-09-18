"""Field-level resume extraction evaluation with operational quality gates."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.modules.user_cvs.domain.schemas import ParsedResume
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.hybrid import HybridResumeParser
from src.modules.user_cvs.parsing.domain.source import build_source_document

DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[5] / "DOC_AND_PLAN" / "data" / "eval" / "cv"
DEFAULT_GATES = {
    "min_macro_f1": 0.75,
    "min_evidence_valid_rate": 1.0,
    "max_fallback_rate": 0.05,
    "max_review_required_rate": 1.0,
    "max_mean_latency_ms": 5_000.0,
    "max_estimated_cost_usd": 1.0,
}


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _facts(payload: dict[str, Any], field: str) -> set[str]:
    items = payload.get(field, [])
    if field == "skills":
        result = set()
        for item in items:
            cid = (item.get("concept") or {}).get("conceptId") or (item.get("concept") or {}).get("concept_id")
            val = (
                cid
                or item.get("taxonomy_concept")
                or item.get("rawLabel")
                or item.get("raw_label")
                or item.get("surface")
            )
            norm = _normalized(val)
            if norm.startswith("skill-"):
                norm = norm[6:]
            if norm:
                result.add(norm)
        return result
    if field == "employment":
        return {
            "|".join(
                _normalized(item.get(k) or item.get(alt))
                for k, alt in (("jobTitle", "title"), ("organization", "organization"))
            )
            for item in items
        }
    if field == "education":
        return {
            "|".join(
                _normalized(item.get(k) or item.get(alt))
                for k, alt in (
                    ("institution", "institution"),
                    ("degree", "degree"),
                    ("fieldOfStudy", "field_of_study"),
                )
            )
            for item in items
        }
    if field == "languages":
        result = set()
        _MAP = {
            "english": "en", "tiếng anh": "en",
            "vietnamese": "vi", "tiếng việt": "vi",
            "chinese": "zh", "tiếng trung": "zh", "mandarin": "zh",
            "japanese": "ja", "tiếng nhật": "ja",
            "spanish": "es", "tiếng tây ban nha": "es",
            "french": "fr", "tiếng pháp": "fr",
            "german": "de", "tiếng đức": "de",
            "korean": "ko", "tiếng hàn": "ko",
            "arabic": "ar", "tiếng ả rập": "ar",
            "russian": "ru", "tiếng nga": "ru",
        }
        for item in items:
            raw_code = str(item.get("code") or item.get("language") or "").casefold().strip()
            code = _MAP.get(raw_code, raw_code)
            level = item.get("level") or item.get("proficiency")
            if code:
                result.add(f"{_normalized(code)}|{_normalized(level)}")
        return result
    keys = {
        "projects": ("name",),
        "certifications": ("name", "issuer"),
    }[field]
    return {"|".join(_normalized(item.get(key)) for key in keys) for item in items}


def _evidence_valid(parsed: dict[str, Any], raw_text: str) -> bool:
    evidence = parsed.get("evidence", [])
    ids = {item.get("evidenceId") for item in evidence}
    if len(ids) != len(evidence):
        return False
    for item in evidence:
        start, end = item.get("charStart"), item.get("charEnd")
        if not isinstance(start, int) or not isinstance(end, int):
            return False
        if raw_text[start:end] != item.get("text"):
            return False
    owners = [
        *parsed.get("skills", []),
        *parsed.get("employment", []),
        *parsed.get("education", []),
        *parsed.get("projects", []),
        *parsed.get("certifications", []),
        *parsed.get("languages", []),
        *parsed.get("careerClassifications", []),
    ]
    return all(set(owner.get("evidenceRefs", [])).issubset(ids) for owner in owners)


def _load_cases(dataset_dir: Path, manifest_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest_path = dataset_dir / manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_dir = manifest_path.parent
    cases: list[dict[str, Any]] = []
    for descriptor in manifest["files"]:
        items = json.loads((manifest_dir / descriptor["path"]).read_text(encoding="utf-8"))
        if len(items) != descriptor["case_count"]:
            raise ValueError(f"{descriptor['path']}: case count does not match manifest")
        cases.extend(items)
    if manifest.get("total_cases") != len(cases):
        raise ValueError("CV evaluation manifest total_cases does not match fixtures")
    if len({item["case_id"] for item in cases}) != len(cases):
        raise ValueError("CV evaluation case_id values must be unique")
    return cases, {**DEFAULT_GATES, **manifest.get("quality_gates", {})}


async def evaluate_cases(
    cases: list[dict[str, Any]],
    parser,
    *,
    quality_gates: dict[str, float] | None = None,
    input_cost_per_million_tokens: float = 0.0,
    output_cost_per_million_tokens: float = 0.0,
    concurrency: int = 5,
) -> dict[str, Any]:
    fields = ("skills", "employment", "education", "projects", "certifications", "languages")
    totals = {field: defaultdict(int) for field in fields}
    results = []
    total_input_tokens = 0
    total_output_tokens = 0
    sem = asyncio.Semaphore(concurrency)

    async def _evaluate_single_case(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            raw_text = case["raw_text"]
            artifacts = DocumentArtifacts(
                markdown=raw_text,
                content_list=case.get("content_list") or [{"type": "text", "text": raw_text, "page_idx": 0}],
                extractor_version="evaluation-fixture-v1",
            )
            source = build_source_document(
                artifacts,
                document_id=case["case_id"],
                document_sha256=case.get("document_sha256", "e" * 64),
            )
            started = time.perf_counter()
            parsed_or_awaitable = parser.parse(
                source, extraction_version="evaluation-fixture-v1", source_artifact_key=None
            )
            parsed: ParsedResume = (
                await parsed_or_awaitable if inspect.isawaitable(parsed_or_awaitable) else parsed_or_awaitable
            )
            latency_ms = (time.perf_counter() - started) * 1_000
            actual = json.loads(parsed.resume.model_dump_json(by_alias=True))
            expected = case["expected"]
            field_results = {}
            case_counts = {}
            for field in fields:
                gold, observed = _facts(expected, field), _facts(actual, field)
                tp, fp, fn = len(gold & observed), len(observed - gold), len(gold - observed)
                case_counts[field] = (tp, fp, fn)
                precision = tp / (tp + fp) if tp + fp else float(not gold)
                recall = tp / (tp + fn) if tp + fn else float(not observed)
                field_results[field] = {
                    "precision": round(precision, 4),
                    "recall": round(recall, 4),
                    "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
                }
            warnings = actual.get("parsing", {}).get("warnings", [])
            output_json = json.dumps(actual, ensure_ascii=False)
            input_tokens = max(1, len(raw_text) // 4)
            output_tokens = max(1, len(output_json) // 4)
            return {
                "case_id": case["case_id"],
                "fields": field_results,
                "evidence_valid": _evidence_valid(actual, source.text),
                "fallback": any(item.get("code") == "llm_fallback" for item in warnings),
                "review_required": actual.get("parsing", {}).get("status") == "review_required",
                "latency_ms": round(latency_ms, 3),
                "warnings": [w.get("message") for w in warnings if w.get("code") == "llm_fallback"],
                "_case_counts": case_counts,
                "_input_tokens": input_tokens,
                "_output_tokens": output_tokens,
            }

    case_results = await asyncio.gather(*(_evaluate_single_case(c) for c in cases))
    for res in case_results:
        counts = res.pop("_case_counts")
        for field, (tp, fp, fn) in counts.items():
            totals[field]["tp"] += tp
            totals[field]["fp"] += fp
            totals[field]["fn"] += fn
        total_input_tokens += res.pop("_input_tokens")
        total_output_tokens += res.pop("_output_tokens")
        results.append(res)
    aggregates = {}
    for field, counts in totals.items():
        precision = counts["tp"] / (counts["tp"] + counts["fp"]) if counts["tp"] + counts["fp"] else 1.0
        recall = counts["tp"] / (counts["tp"] + counts["fn"]) if counts["tp"] + counts["fn"] else 1.0
        aggregates[field] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
        }
    total = len(results)
    estimated_cost = (
        total_input_tokens * input_cost_per_million_tokens
        + total_output_tokens * output_cost_per_million_tokens
    ) / 1_000_000
    summary = {
        "total": total,
        "macro_f1": round(sum(item["f1"] for item in aggregates.values()) / len(aggregates), 4),
        "evidence_valid_rate": round(sum(item["evidence_valid"] for item in results) / total, 4)
        if total
        else 0.0,
        "fallback_rate": round(sum(item["fallback"] for item in results) / total, 4) if total else 0.0,
        "review_required_rate": round(sum(item["review_required"] for item in results) / total, 4)
        if total
        else 0.0,
        "mean_latency_ms": round(sum(item["latency_ms"] for item in results) / total, 3) if total else 0.0,
        "estimated_cost_usd": round(estimated_cost, 6),
        "estimated_input_tokens": total_input_tokens,
        "estimated_output_tokens": total_output_tokens,
        "fields": aggregates,
    }
    gates = {**DEFAULT_GATES, **(quality_gates or {})}
    checks = {
        "macro_f1": summary["macro_f1"] >= gates["min_macro_f1"],
        "evidence_valid_rate": summary["evidence_valid_rate"] >= gates["min_evidence_valid_rate"],
        "fallback_rate": summary["fallback_rate"] <= gates["max_fallback_rate"],
        "review_required_rate": summary["review_required_rate"] <= gates["max_review_required_rate"],
        "mean_latency_ms": summary["mean_latency_ms"] <= gates["max_mean_latency_ms"],
        "estimated_cost_usd": summary["estimated_cost_usd"] <= gates["max_estimated_cost_usd"],
    }
    return {
        "evaluation": "cv-parser-evidence-grounded-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "metric_contract": {
            "fields": "micro exact-match precision/recall/F1 over normalized canonical entity keys",
            "evidence": "all offsets round-trip and every evidenceRef resolves",
            "cost": "explicit character/4 token estimate; not provider billing usage",
        },
        "summary": summary,
        "quality_gate": {"passed": all(checks.values()), "checks": checks, "thresholds": gates},
        "cases": results,
    }


async def run_evaluation(
    dataset_dir: Path, manifest_name: str, parser_mode: str, concurrency: int = 5, limit: int | None = None, **costs
) -> dict[str, Any]:
    cases, gates = _load_cases(dataset_dir, manifest_name)
    if limit is not None and limit > 0:
        cases = cases[:limit]
    parser = HybridResumeParser() if parser_mode == "hybrid" else DeterministicResumeParser()
    return await evaluate_cases(cases, parser, quality_gates=gates, concurrency=concurrency, **costs)


def main() -> None:
    command = argparse.ArgumentParser(description="Evaluate evidence-grounded CV parsing.")
    command.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    command.add_argument("--manifest", default="parser_core_v1/manifests/manifest.json")
    command.add_argument("--parser", choices=("deterministic", "hybrid"), default="deterministic")
    command.add_argument("--concurrency", type=int, default=5)
    command.add_argument("--limit", type=int, default=None)
    command.add_argument("--input-cost-per-million-tokens", type=float, default=0.0)
    command.add_argument("--output-cost-per-million-tokens", type=float, default=0.0)
    command.add_argument("--output", type=Path)
    args = command.parse_args()
    if args.limit is not None and args.limit < 1:
        command.error("--limit must be at least 1")
    report = asyncio.run(
        run_evaluation(
            args.dataset_dir,
            args.manifest,
            args.parser,
            concurrency=args.concurrency,
            limit=args.limit,
            input_cost_per_million_tokens=args.input_cost_per_million_tokens,
            output_cost_per_million_tokens=args.output_cost_per_million_tokens,
        )
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "quality_gate": report["quality_gate"]}))
    raise SystemExit(0 if report["quality_gate"]["passed"] else 1)


if __name__ == "__main__":
    main()
