"""Semantic evaluation for independently annotated JD datasets.

Unlike the hand-authored regression fixtures, these labels may paraphrase the
source job description.  This evaluator deliberately does *not* require text
or evidence-span equality.  It measures token-level semantic coverage while
still checking that parser-produced evidence offsets are valid.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.evaluation.runner import DEFAULT_DATASET_DIR, _evidence_is_valid, _source
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.job_descriptions.parsing.hybrid import (
    PARSER_VERSION as HYBRID_PARSER_VERSION,
)
from src.modules.job_descriptions.parsing.hybrid import (
    HybridJobDescriptionParser,
)
from src.modules.job_descriptions.parsing.llm_candidate import JD_EXTRACTION_INSTRUCTIONS

logger = logging.getLogger(__name__)


# Keep meaningful domain terms (including C++, C#, Node.js and ISO-like terms)
# and discard only words which add little meaning in a JD summary.
_TOKEN = re.compile(r"[\w][\w+#.\-/]*", re.UNICODE)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "you",
        "your",
        "will",
        "this",
        "that",
        "their",
        "our",
        "experience",
        "ability",
        "skills",
        "skill",
        "work",
        "working",
        "job",
        "role",
        "team",
    }
)


def _tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return {
        token.lower()
        for token in _TOKEN.findall(value)
        if token.lower() not in _STOP_WORDS and len(token) > 1
    }


def _similarity(left: str | None, right: str | None) -> float:
    """Token Dice score; 1 when two non-empty normalized texts match."""
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return float(left_tokens == right_tokens)
    return 2 * len(left_tokens & right_tokens) / (len(left_tokens) + len(right_tokens))


def _requirement_text(item: dict[str, Any]) -> str:
    concept = item.get("concept") or {}
    return " ".join(filter(None, [item.get("rawLabel"), concept.get("label")]))


def _field_score(expected: list[str], observed: list[str]) -> dict[str, Any]:
    """One-to-one greedy matching prevents one broad output claiming all gold facts."""
    pairs = sorted(
        (
            (_similarity(gold, actual), gold_index, actual_index)
            for gold_index, gold in enumerate(expected)
            for actual_index, actual in enumerate(observed)
        ),
        reverse=True,
    )
    matched_gold: set[int] = set()
    matched_actual: set[int] = set()
    scores: list[float] = []
    for score, gold_index, actual_index in pairs:
        if gold_index not in matched_gold and actual_index not in matched_actual:
            matched_gold.add(gold_index)
            matched_actual.add(actual_index)
            scores.append(score)

    # Unmatched facts contribute zero.  This makes both omissions and noisy
    # parser additions visible, unlike simply taking a best-match average.
    recall = sum(scores) / len(expected) if expected else (1.0 if not observed else 0.0)
    precision = sum(scores) / len(observed) if observed else (1.0 if not expected else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "applicable": bool(expected or observed),
        "expected_count": len(expected),
        "observed_count": len(observed),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _case_score(raw_text: str, gold: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "responsibilities": _field_score(
            [item["text"] for item in gold["responsibilities"]],
            [item["text"] for item in actual["responsibilities"]],
        ),
        "requirements": _field_score(
            [_requirement_text(item) for item in gold["requirements"]],
            [_requirement_text(item) for item in actual["requirements"]],
        ),
        "benefits": _field_score(
            [item["text"] for item in gold["benefits"]],
            [item["text"] for item in actual["benefits"]],
        ),
    }
    title_similarity = _similarity(gold.get("jobTitle"), actual.get("jobTitle"))
    applicable_metrics = [metric["f1"] for metric in fields.values() if metric["applicable"]]
    semantic_score = (title_similarity + sum(applicable_metrics)) / (1 + len(applicable_metrics))
    return {
        "title_similarity": round(title_similarity, 4),
        "fields": fields,
        "semantic_score": round(semantic_score, 4),
        "evidence_valid": _evidence_is_valid(actual, raw_text),
        "expected_title": gold.get("jobTitle"),
        "actual_title": actual.get("jobTitle"),
    }


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    fields = ("responsibilities", "requirements", "benefits")
    return {
        "total": total,
        "macro_semantic_score": round(sum(item["semantic_score"] for item in cases) / total, 4)
        if total
        else 0.0,
        "mean_title_similarity": round(sum(item["title_similarity"] for item in cases) / total, 4)
        if total
        else 0.0,
        "valid_evidence_cases": sum(item["evidence_valid"] for item in cases),
        "invalid_evidence_cases": sum(not item["evidence_valid"] for item in cases),
        "fields": {
            field: {
                "evaluated_cases": len(
                    applicable := [item for item in cases if item["fields"][field]["applicable"]]
                ),
                "non_applicable_cases": total - len(applicable),
                **{
                    metric: round(
                        sum(item["fields"][field][metric] for item in applicable) / len(applicable), 4
                    )
                    if applicable
                    else 0.0
                    for metric in ("precision", "recall", "f1")
                },
            }
            for field in fields
        },
    }


def _load_cases(dataset_dir: Path, manifest_name: str, limit: int | None = None) -> list[dict[str, Any]]:
    manifest = json.loads((dataset_dir / manifest_name).read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    for descriptor in manifest["files"]:
        path = dataset_dir / descriptor["path"]
        if not path.is_file():
            raise FileNotFoundError(f"Semantic manifest references missing fixture: {path}")
        file_cases = json.loads(path.read_text(encoding="utf-8"))
        if len(file_cases) != descriptor["case_count"]:
            raise ValueError(
                f"{path.name}: expected {descriptor['case_count']} cases, found {len(file_cases)}"
            )
        cases.extend(file_cases)
    if manifest.get("total_cases") is not None and len(cases) != manifest["total_cases"]:
        raise ValueError(f"Semantic manifest declares {manifest['total_cases']} cases, found {len(cases)}")
    case_ids = [case["case_id"] for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Semantic fixture case_id values must be unique")
    for case in cases:
        CanonicalJobDescription.model_validate(case["expected"])
    return cases[:limit] if limit is not None else cases


def _report(
    dataset_dir: Path, manifest_name: str, parser_mode: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "evaluation": f"jd-parser-semantic-{parser_mode}-{Path(manifest_name).stem}",
        "metric_contract": {
            "matching": "one-to-one token Dice similarity; unmatched expected/observed facts score zero",
            "semantic_score": (
                "mean of title similarity and F1 for applicable responsibilities, "
                "requirements, benefits"
            ),
            "method_limit": "offline lexical paraphrase proxy, not an embedding or LLM judge",
            "not_a_pass_fail_gate": True,
            "gold_evidence_available": True,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_dir": str(dataset_dir),
        "manifest": manifest_name,
        "parser_mode": parser_mode,
        "summary": _aggregate(results),
        "lowest_scoring_cases": sorted(results, key=lambda item: item["semantic_score"])[:25],
        "cases": results,
    }


def _hybrid_cache_path(dataset_dir: Path, case_id: str, raw_text: str) -> Path:
    """Key cached parser output by every input that can affect extraction."""
    settings = get_settings()
    identity = json.dumps(
        {
            "case_id": case_id,
            "raw_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
            "model": settings.llm_model,
            "max_output_tokens": settings.jd_parser_max_output_tokens,
            "parser_version": HYBRID_PARSER_VERSION,
            "instructions_sha256": hashlib.sha256(JD_EXTRACTION_INSTRUCTIONS.encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    key = hashlib.sha256(identity.encode()).hexdigest()
    return dataset_dir / "reports" / "semantic_cache" / f"{key}.json"


def _read_hybrid_cache(path: Path, raw_text: str) -> CanonicalJobDescription | None:
    if not path.is_file():
        return None
    try:
        parsed = CanonicalJobDescription.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if _evidence_is_valid(json.loads(parsed.model_dump_json(by_alias=True)), raw_text) else None


def _write_hybrid_cache(path: Path, parsed: CanonicalJobDescription) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(parsed.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")


def run_semantic(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    manifest_name: str = "manifest.json",
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run the fast, offline deterministic baseline."""
    parser = DeterministicJobDescriptionParser()
    cases = _load_cases(dataset_dir, manifest_name, limit)

    results = []
    for case in cases:
        raw_text = case["raw_text"]
        parsed = parser.parse(_source(case["case_id"], raw_text), extraction_version="semantic-eval-v1")
        actual = json.loads(parsed.model_dump_json(by_alias=True))
        results.append({"case_id": case["case_id"], **_case_score(raw_text, case["expected"], actual)})

    return _report(dataset_dir, manifest_name, "deterministic", results)


async def run_semantic_hybrid(
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    manifest_name: str = "manifest.json",
    *,
    progress_every: int = 10,
    limit: int | None = None,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    """Evaluate the configured OpenAI model with evidence grounding."""
    if progress_every < 1:
        raise ValueError("progress_every must be at least 1")
    parser = HybridJobDescriptionParser()
    cases = _load_cases(dataset_dir, manifest_name, limit)
    results = []
    fallbacks = 0
    cache_hits = 0
    started = time.monotonic()
    logger.info(
        "OpenAI hybrid evaluation started: %d cases; progress every %d case(s)", len(cases), progress_every
    )
    for index, case in enumerate(cases, start=1):
        raw_text = case["raw_text"]
        cache_path = _hybrid_cache_path(dataset_dir, case["case_id"], raw_text)
        parsed = None if refresh_cache else _read_hybrid_cache(cache_path, raw_text)
        cache_hit = parsed is not None
        if parsed is None:
            parsed = await parser.parse(
                _source(case["case_id"], raw_text), extraction_version="semantic-hybrid-eval-v1"
            )
            _write_hybrid_cache(cache_path, parsed)
        else:
            cache_hits += 1
        actual = json.loads(parsed.model_dump_json(by_alias=True))
        result = {
            "case_id": case["case_id"],
            "cache_hit": cache_hit,
            **_case_score(raw_text, case["expected"], actual),
        }
        parser_warnings = [warning.model_dump(by_alias=True) for warning in parsed.parsing.warnings]
        if parser_warnings:
            result["parser_warnings"] = parser_warnings
        results.append(result)
        fallback_messages = [
            warning.message for warning in parsed.parsing.warnings if warning.code == "llm_fallback"
        ]
        fallbacks += len(fallback_messages)
        if fallback_messages:
            logger.warning("Hybrid case=%s fallback: %s", case["case_id"], " | ".join(fallback_messages))
        if index == 1 or index % progress_every == 0 or index == len(cases):
            elapsed = time.monotonic() - started
            rate = index / elapsed if elapsed else 0.0
            remaining = (len(cases) - index) / rate if rate else 0.0
            logger.info(
                "OpenAI hybrid evaluation %d/%d (%.1f%%) case=%s fallback=%d "
                "cache_hits=%d elapsed=%.0fs eta=%.0fs",
                index,
                len(cases),
                index * 100 / len(cases),
                case["case_id"],
                fallbacks,
                cache_hits,
                elapsed,
                remaining,
            )
    return _report(dataset_dir, manifest_name, "hybrid", results)


def write_semantic_report(report: dict[str, Any], dataset_dir: Path) -> Path:
    reports_dir = dataset_dir / "reports" / "semantic"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"jd_parser_semantic_{timestamp}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_path.write_text(payload, encoding="utf-8")
    (reports_dir / "latest.json").write_text(payload, encoding="utf-8")
    history = {
        "generated_at": report["generated_at"],
        "evaluation": report["evaluation"],
        "summary": report["summary"],
        "report": report_path.name,
    }
    with (reports_dir / "evaluation_history.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(history, ensure_ascii=False, separators=(",", ":")) + "\n")
    return report_path
