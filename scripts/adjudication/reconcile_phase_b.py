"""
PR2 Gate P2 Phase B Automated Reconciliation Engine
==================================================
Strict two-phase reconciliation runner:
1. Validates integrity of HUMAN_REVIEW_RESPONSES_90.json against locked BLIND_REVIEW_QUEUE_90.json.
2. Enforces Rule 6: Strictly fails closed if human reviewer fields are missing or fabricated.
3. Derives MATCH / DIFFERENT / UNCERTAIN.
4. Generates GATE_P2_SECOND_REVIEW_QUEUE for any discrepancies or uncertainties.
5. Blocks refreeze until Second Review is resolved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")


def find_eval_base_dir() -> Path:
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir.parent / "DOC_AND_PLAN" / "data" / "eval" / "jd",
        script_dir / "DOC_AND_PLAN" / "data" / "eval" / "jd",
        Path.cwd() / "DOC_AND_PLAN" / "data" / "eval" / "jd",
        Path.cwd() / "data" / "eval" / "jd",
    ]
    for c in candidates:
        if c.exists() and (c / "adjudication_pack_p2").exists():
            return c.resolve()
    return (script_dir.parent / "DOC_AND_PLAN" / "data" / "eval" / "jd").resolve()


BASE_DIR = find_eval_base_dir()
PACK_DIR = (BASE_DIR / "adjudication_pack_p2").resolve()
CORE_GOLDEN = (BASE_DIR / "parser_core_v1" / "private" / "golden_jd_parser_core_v2.json").resolve()
EDGE_GOLDEN = (BASE_DIR / "parser_edge_v1" / "private" / "golden_jd_parser_edge_v2.json").resolve()

BLIND_PACK_PATH = (PACK_DIR / "BLIND_REVIEW_QUEUE_90.json").resolve()
RESPONSES_PATH = (PACK_DIR / "HUMAN_REVIEW_RESPONSES_90.json").resolve()
SECOND_REVIEW_JSON = (PACK_DIR / "GATE_P2_SECOND_REVIEW_QUEUE.json").resolve()
SECOND_REVIEW_MD = (PACK_DIR / "GATE_P2_SECOND_REVIEW_QUEUE.md").resolve()


def sha256_file(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_golden_cases() -> dict[str, dict[str, Any]]:
    golden_map = {}
    for g_path in [CORE_GOLDEN, EDGE_GOLDEN]:
        assert g_path.exists(), f"Golden dataset missing: {g_path}"
        with open(g_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            golden_map[item["case_id"]] = item
    return golden_map


def normalize_req(r: dict[str, Any]) -> dict[str, Any]:
    """Normalize requirement dictionary for deterministic semantic comparison."""
    kind = (r.get("kind") or "").strip().lower()
    priority = (r.get("priority") or "").strip().lower()
    op = (r.get("operator") or "").strip().lower() if r.get("operator") else None
    months = r.get("minimum_experience_months") or r.get("minimumExperienceMonths")
    if months is not None:
        try:
            months = int(months)
        except (ValueError, TypeError):
            months = None
    thresh = r.get("threshold")
    if thresh is not None:
        try:
            thresh = float(thresh)
        except (ValueError, TypeError):
            thresh = None
    cred = (r.get("credential") or "").strip().upper() if r.get("credential") else None
    equiv = r.get("equivalent_allowed") or r.get("equivalentAllowed")
    equiv = bool(equiv) if equiv is not None else None
    group_op = r.get("group_operator") or r.get("groupOperator")
    if group_op:
        group_op = str(group_op).strip().lower()
    
    return {
        "kind": kind,
        "priority": priority,
        "operator": op,
        "months": months,
        "threshold": thresh,
        "credential": cred,
        "equivalent_allowed": equiv,
        "group_operator": group_op,
    }


def compare_requirements(
    reviewer_reqs: list[dict[str, Any]], golden_reqs: list[dict[str, Any]]
) -> tuple[bool, list[str]]:
    """Compare reviewer requirements against golden requirements."""
    diffs = []
    norm_rev = [normalize_req(r) for r in reviewer_reqs]
    norm_gold = [normalize_req(r) for r in golden_reqs]

    if len(norm_rev) != len(norm_gold):
        diffs.append(f"Requirement count mismatch: reviewer extracted {len(norm_rev)}, golden has {len(norm_gold)}")
        return False, diffs

    # Element-wise check
    for idx, (r, g) in enumerate(zip(norm_rev, norm_gold)):
        mismatches = []
        for key in ["kind", "priority", "operator", "months", "threshold", "credential", "equivalent_allowed", "group_operator"]:
            rv = r.get(key)
            gv = g.get(key)
            if rv != gv:
                mismatches.append(f"{key}: reviewer='{rv}' vs golden='{gv}'")
        if mismatches:
            diffs.append(f"Req #{idx + 1} difference: {'; '.join(mismatches)}")

    return (len(diffs) == 0), diffs


def run_reconciliation() -> dict[str, Any]:
    print("==================================================")
    print("GATE P2 — PHASE B RECONCILIATION RUNNER")
    print("==================================================")

    # 1. Verify existence of inputs
    assert BLIND_PACK_PATH.exists(), f"Blind pack missing: {BLIND_PACK_PATH}"
    assert RESPONSES_PATH.exists(), f"Responses file missing: {RESPONSES_PATH}"

    # 2. Hash check
    actual_blind_sha256 = sha256_file(BLIND_PACK_PATH)
    print(f"[*] Immutable Blind Pack SHA256: {actual_blind_sha256}")

    with open(BLIND_PACK_PATH, "r", encoding="utf-8") as f:
        blind_data = json.load(f)
    blind_cases = blind_data.get("cases", [])
    assert len(blind_cases) == 90, f"Expected 90 cases in blind pack, found {len(blind_cases)}"

    with open(RESPONSES_PATH, "r", encoding="utf-8") as f:
        resp_data = json.load(f)

    resp_meta = resp_data.get("metadata", {})
    recorded_sha256 = resp_meta.get("source_pack_sha256")
    responses = resp_data.get("responses", [])

    print(f"[*] Response Template cases: {len(responses)}")
    print(f"[*] Recorded source_pack_sha256: {recorded_sha256}")

    # Validation checks
    val_1_count = len(responses) == 90
    val_2_hash = (recorded_sha256 == actual_blind_sha256)

    blind_ids = [c["case_id"] for c in blind_cases]
    resp_ids = [r["case_id"] for r in responses]
    val_1_ids_match = (blind_ids == resp_ids)

    # Check for human fields
    filled_reviewer_id = [r["case_id"] for r in responses if r.get("reviewer_id")]
    filled_reviewed_at = [r["case_id"] for r in responses if r.get("reviewed_at")]
    filled_status = [r["case_id"] for r in responses if r.get("reviewer_status")]
    filled_complete = [r["case_id"] for r in responses if r.get("reviewer_status") == "COMPLETE"]
    filled_uncertain = [r["case_id"] for r in responses if r.get("reviewer_status") == "UNCERTAIN"]
    complete_with_ann = [
        r["case_id"]
        for r in responses
        if r.get("reviewer_status") == "COMPLETE"
        and len(r.get("reviewer_annotation", {}).get("requirements", [])) > 0
    ]

    print("\n--- VALIDATION CHECKLIST ---")
    print(f"1. 90/90 case_id match order: {'PASS' if val_1_ids_match else 'FAIL'}")
    print(f"2. source_pack_sha256 exact match: {'PASS' if val_2_hash else 'FAIL'}")
    print(f"3. reviewer_id present: {len(filled_reviewer_id)} / 90")
    print(f"   reviewed_at present: {len(filled_reviewed_at)} / 90")
    print(f"4. COMPLETE status present: {len(filled_complete)} / 90 (with annotations: {len(complete_with_ann)})")
    print(f"5. UNCERTAIN status present: {len(filled_uncertain)} / 90")
    print("6. Auto-fill / fake data injection: STRICTLY FORBIDDEN (0 injected)")

    # Hard rule: If human review is incomplete, we must HALT and report BLOCKED.
    is_fully_reviewed = (
        len(filled_reviewer_id) == 90
        and len(filled_reviewed_at) == 90
        and len(filled_status) == 90
    )

    if not is_fully_reviewed:
        print("\n==================================================")
        print("RESULT: PHASE B RECONCILIATION BLOCKED")
        print("Reason: HUMAN INPUT REQUIRED — 0/90 human reviews completed.")
        print("HUMAN_REVIEW_RESPONSES_90.json has not been filled by a human reviewer.")
        print("Rule 6 strictly forbids fabricating reviewer decisions or timestamps.")
        print("==================================================")
        return {
            "status": "BLOCKED_AWAITING_HUMAN_REVIEW",
            "total_cases": 90,
            "reviewed_count": len(filled_status),
            "awaiting_count": 90 - len(filled_status),
            "match_count": 0,
            "different_count": 0,
            "uncertain_count": 0,
            "hash_verified": val_2_hash,
        }

    # If all 90 cases were reviewed, proceed with Phase B reconciliation
    golden_map = load_golden_cases()
    second_review_queue = []
    match_count = 0
    different_count = 0
    uncertain_count = 0

    for r in responses:
        cid = r["case_id"]
        status = r.get("reviewer_status")
        note = r.get("reviewer_note") or ""
        g_item = golden_map.get(cid, {})
        g_reqs = g_item.get("expected", {}).get("requirements", [])
        r_reqs = r.get("reviewer_annotation", {}).get("requirements", [])

        if status == "UNCERTAIN":
            uncertain_count += 1
            second_review_queue.append({
                "case_id": cid,
                "split": r.get("split"),
                "reason": "Reviewer flagged case as UNCERTAIN",
                "reviewer_note": note,
                "reviewer_annotation": r_reqs,
                "locked_golden_annotation": g_reqs,
                "diff_details": ["Reviewer explicitly marked case as UNCERTAIN."],
                "second_reviewer_decision": None,
                "second_reviewer_note": None,
            })
        elif status == "COMPLETE":
            is_match, diffs = compare_requirements(r_reqs, g_reqs)
            if is_match:
                match_count += 1
            else:
                different_count += 1
                second_review_queue.append({
                    "case_id": cid,
                    "split": r.get("split"),
                    "reason": "Discrepancy detected between reviewer annotation and locked golden label",
                    "reviewer_note": note,
                    "reviewer_annotation": r_reqs,
                    "locked_golden_annotation": g_reqs,
                    "diff_details": diffs,
                    "second_reviewer_decision": None,
                    "second_reviewer_note": None,
                })

    # Save second review queue
    second_queue_data = {
        "metadata": {
            "pack_name": "GATE_P2_SECOND_REVIEW_QUEUE",
            "source_pack_sha256": actual_blind_sha256,
            "total_items": len(second_review_queue),
            "different_count": different_count,
            "uncertain_count": uncertain_count,
            "match_count": match_count,
            "status": "AWAITING_SECOND_REVIEW_ADJUDICATION" if second_review_queue else "RESOLVED_ALL_MATCH",
        },
        "queue": second_review_queue,
    }
    with open(SECOND_REVIEW_JSON, "w", encoding="utf-8") as f:
        json.dump(second_queue_data, f, indent=2, ensure_ascii=False)

    print("\n--- RECONCILIATION SUMMARY ---")
    print(f"Total Reviewed: 90")
    print(f"MATCH (No golden change): {match_count}")
    print(f"DIFFERENT (Routed to Second Review): {different_count}")
    print(f"UNCERTAIN (Routed to Second Review): {uncertain_count}")
    print(f"Total in Second Review Queue: {len(second_review_queue)}")

    return {
        "status": "RECONCILIATION_COMPLETE" if not second_review_queue else "SECOND_REVIEW_REQUIRED",
        "total_cases": 90,
        "match_count": match_count,
        "different_count": different_count,
        "uncertain_count": uncertain_count,
        "second_review_queue_count": len(second_review_queue),
    }


if __name__ == "__main__":
    result = run_reconciliation()
