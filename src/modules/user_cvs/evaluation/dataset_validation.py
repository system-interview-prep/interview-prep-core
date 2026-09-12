"""Integrity checks for privacy-preserving CV evaluation fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[5] / "DOC_AND_PLAN" / "data" / "eval" / "cv"
FIELDS = ("skills", "employment", "education", "projects", "certifications", "languages")
PII_PATTERNS = (
    re.compile(r"[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?\d[ .()-]?){8,}\d(?!\d)"),
    re.compile(r"https?://|\b(?:linkedin|github)\.com/", re.IGNORECASE),
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_no_pii(value: str, *, case_id: str) -> None:
    for pattern in PII_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"{case_id}: fixture text contains a forbidden PII/contact pattern")


def _validate_case(case: dict[str, Any]) -> None:
    case_id = case["case_id"]
    raw_text = case["raw_text"]
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError(f"{case_id}: raw_text must be non-empty")
    _assert_no_pii(raw_text, case_id=case_id)
    expected = case["expected"]
    if set(expected) != set(FIELDS):
        raise ValueError(f"{case_id}: expected fields must exactly match the CV evaluator contract")
    anchors = case["gold_evidence"]
    anchor_ids = [anchor["evidence_id"] for anchor in anchors]
    if len(anchor_ids) != len(set(anchor_ids)):
        raise ValueError(f"{case_id}: gold evidence IDs must be unique")
    anchored_fields = set()
    for anchor in anchors:
        start, end = anchor["char_start"], anchor["char_end"]
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(raw_text):
            raise ValueError(f"{case_id}: invalid gold evidence offsets")
        if raw_text[start:end] != anchor["text"]:
            raise ValueError(f"{case_id}: gold evidence does not round-trip")
        anchored_fields.add(anchor["field"])
    for field in FIELDS:
        if expected[field] and field not in anchored_fields:
            raise ValueError(f"{case_id}: non-empty {field} requires gold evidence")
    expected_skill_ids = {item["concept"]["conceptId"] for item in expected["skills"]}
    anchored_skill_ids = {item.get("concept_id") for item in anchors if item["field"] == "skills"}
    if expected_skill_ids != anchored_skill_ids:
        raise ValueError(f"{case_id}: each expected skill must map to one grounded taxonomy concept")


def validate_cv_datasets(dataset_dir: Path = DEFAULT_DATASET_DIR) -> dict[str, int]:
    """Validate all gold/silver artifacts without calling a model or source corpus."""
    totals: dict[str, int] = {}
    case_ids: set[str] = set()
    for name in (
        "golden_cv_skill_evidence_v1.json",
        "golden_cv_parser_core_v1.json",
        "golden_cv_parser_edge_v1.json",
    ):
        cases = _load(dataset_dir / name)
        for case in cases:
            _validate_case(case)
            if case["case_id"] in case_ids:
                raise ValueError(f"duplicate CV fixture ID: {case['case_id']}")
            case_ids.add(case["case_id"])
        totals[name] = len(cases)
    silver = _load(dataset_dir / "datasetmaster_resume_silver_v1.json")
    for item in silver:
        if item.get("gating") is not False or "raw_text" in item:
            raise ValueError("silver data must be non-gating and must not retain resume text")
        # Hash values are opaque identifiers, not source text. Exclude them
        # from contact-pattern checks because a random SHA-256 may contain a
        # run of digits that resembles a phone number.
        safe_item = {key: value for key, value in item.items() if key != "source_record_sha256"}
        serialized = json.dumps(safe_item, ensure_ascii=False)
        _assert_no_pii(serialized, case_id=item["case_id"])
    totals["datasetmaster_resume_silver_v1.json"] = len(silver)
    return totals
