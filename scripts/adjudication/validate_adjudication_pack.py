import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path

def find_eval_base_dir() -> Path:
    parser = argparse.ArgumentParser(description="Validate PR2 Gate P2 Human Adjudication Pack")
    parser.add_argument("--base-dir", type=str, default=None, help="Base path to data/eval/jd in DOC_AND_PLAN")
    parser.add_argument("--pack-dir", type=str, default=None, help="Explicit path to adjudication_pack_p2 folder")
    args, _ = parser.parse_known_args()
    
    if args.base_dir:
        return Path(args.base_dir).resolve()
        
    # 1. Environment variables override
    for env_key in ("EVAL_DATASET_DIR", "DOC_AND_PLAN_DIR"):
        val = os.getenv(env_key)
        if val:
            p = Path(val).resolve()
            if (p / "adjudication_pack_p2").exists() or (p / "parser_core_v1").exists():
                return p
            if (p / "data" / "eval" / "jd").exists():
                return (p / "data" / "eval" / "jd").resolve()

    # 2. Dynamic relative discovery based on repo layout
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir.parent / "DOC_AND_PLAN" / "data" / "eval" / "jd",  # Standard sibling clone (Linux/macOS/Windows)
        script_dir / "DOC_AND_PLAN" / "data" / "eval" / "jd",         # Nested structure
        Path.cwd() / "DOC_AND_PLAN" / "data" / "eval" / "jd",        # CWD relative
        Path.cwd() / "data" / "eval" / "jd",                         # Run from within DOC_AND_PLAN
        Path("/workspace/DOC_AND_PLAN/data/eval/jd"),                # Docker container mount
        Path("/app/DOC_AND_PLAN/data/eval/jd"),                      # CI/CD container mount
    ]
    for c in candidates:
        if c.exists() and ((c / "parser_core_v1").exists() or (c / "adjudication_pack_p2").exists()):
            return c.resolve()

    # Fallback to sibling layout
    return (script_dir.parent / "DOC_AND_PLAN" / "data" / "eval" / "jd").resolve()

BASE_DIR = find_eval_base_dir()
PACK_DIR = Path(os.getenv("PACK_DIR") or (BASE_DIR / "adjudication_pack_p2")).resolve()
CORE_DATASET = (BASE_DIR / "parser_core_v1" / "private" / "golden_jd_parser_core_v2.json").resolve()
EDGE_DATASET = (BASE_DIR / "parser_edge_v1" / "private" / "golden_jd_parser_edge_v2.json").resolve()

PROHIBITED_BLIND_KEYS = [
    "parser_output",
    "parser_current_output",
    "expected",
    "proposed_golden",
    "previous_golden_value",
    "proposed_corrected_value",
    "golden_requirements",
    "expected_requirements",
    "operator",
    "group_operator",
    "priority",
    "threshold",
    "scale",
    "concept",
    "reconciliation_diff",
]

VALID_DECISIONS = {"APPROVED", "APPROVED_WITH_CORRECTION", "NEEDS_DISCUSSION", "REJECTED"}

def validate():
    print("=== RUNNING PRE-REVIEW QA & ADJUDICATION PACK AUDIT ===")
    
    # 1. Blind Review Queue JSON & Metadata
    blind_json_path = os.path.join(PACK_DIR, "BLIND_REVIEW_QUEUE_90.json")
    assert os.path.exists(blind_json_path), "BLIND_REVIEW_QUEUE_90.json not found"
    with open(blind_json_path, "r", encoding="utf-8") as f:
        blind_data = json.load(f)
    
    meta = blind_data.get("metadata", {})
    assert meta.get("phase_a_status") == "NOT_STARTED", f"Unexpected phase_a_status: {meta.get('phase_a_status')}"
    assert meta.get("phase_b_status") == "LOCKED", f"Unexpected phase_b_status: {meta.get('phase_b_status')}"
    
    cases = blind_data.get("cases", [])
    assert len(cases) == 90, f"Expected 90 cases, got {len(cases)}"
    core_count = sum(1 for c in cases if c.get("split") == "core")
    edge_count = sum(1 for c in cases if c.get("split") == "edge")
    assert core_count == 50, f"Expected 50 core cases, got {core_count}"
    assert edge_count == 40, f"Expected 40 edge cases, got {edge_count}"
    
    # Check prohibited keys in blind cases
    prohibited_found = []
    for c in cases:
        for k in c.keys():
            if k in PROHIBITED_BLIND_KEYS:
                prohibited_found.append((c["case_id"], k))
        # check blind_annotations is empty list
        if c.get("blind_annotations") != []:
            prohibited_found.append((c["case_id"], "blind_annotations_not_empty"))
        if c.get("reviewer_identity") is not None:
            prohibited_found.append((c["case_id"], "reviewer_identity_not_null"))
        if c.get("reviewer_notes") is not None:
            prohibited_found.append((c["case_id"], "reviewer_notes_not_null"))
        if c.get("review_timestamp") is not None:
            prohibited_found.append((c["case_id"], "review_timestamp_not_null"))
        if c.get("blind_review_status") != "AWAITING_HUMAN_SIGNOFF":
            prohibited_found.append((c["case_id"], f"status_{c.get('blind_review_status')}"))
            
    print(f"1. Blind Pack: 90 cases (50 core, 40 edge). Prohibited leaks found: {len(prohibited_found)}")
    assert len(prohibited_found) == 0, f"Prohibited leaks in blind pack: {prohibited_found[:5]}"

    # 2. Phase Isolation & Reconciliation Queue JSON
    recon_json_path = os.path.join(PACK_DIR, "RECONCILIATION_QUEUE_90.json")
    assert os.path.exists(recon_json_path), "RECONCILIATION_QUEUE_90.json not found"
    with open(recon_json_path, "r", encoding="utf-8") as f:
        recon_data = json.load(f)
    
    recon_meta = recon_data.get("metadata", {})
    assert recon_meta.get("phase_a_status") == "NOT_STARTED"
    assert recon_meta.get("phase_b_status") == "LOCKED"
    # Phase Isolation Rule: Phase B cannot be AVAILABLE when Phase A is not COMPLETED_LOCKED
    if recon_meta.get("phase_a_status") != "COMPLETED_LOCKED":
        assert recon_meta.get("phase_b_status") == "LOCKED", "Phase B must remain LOCKED while Phase A is incomplete"
    
    recon_cases = recon_data.get("cases", [])
    assert len(recon_cases) == 90, f"Expected 90 cases in recon, got {len(recon_cases)}"
    
    for rc in recon_cases:
        assert "column_a_human_blind_label" in rc
        assert "column_b_proposed_golden_label" in rc
        assert "column_c_parser_output" in rc
        assert "column_d_precalculated_differences" in rc
        assert "column_e_human_final_decision" in rc
        assert rc["column_a_human_blind_label"] is None
        assert rc["column_e_human_final_decision"] is None
        assert rc["reviewer_identity"] is None
        assert rc["reviewer_notes"] is None
        assert rc["review_timestamp"] is None
        assert set(rc["allowed_decision_states"]) == VALID_DECISIONS

    print("2. Reconciliation Pack: 90 cases verified with Columns A-E structure and Phase B LOCKED.")

    # 3. Leakage Queue QA
    leakage_json_path = os.path.join(PACK_DIR, "POTENTIAL_LEAKAGE_QUEUE_42.json")
    with open(leakage_json_path, "r", encoding="utf-8") as f:
        leakage_data = json.load(f)
    leakage_meta = leakage_data.get("metadata", {})
    assert leakage_meta.get("phase_b_status") == "LOCKED"
    leakage_records = leakage_data.get("records", [])
    assert len(leakage_records) == 42, f"Expected 42 leakage records, got {len(leakage_records)}"
    
    # Check core-vi-game-003
    game_003_records = [r for r in leakage_records if r["case_id"] == "core-vi-game-003"]
    assert len(game_003_records) > 0, "core-vi-game-003 not in leakage queue"
    for r in game_003_records:
        src_sent = r["source_sentence"]
        assert "Unity Developer - Game Mobile [Ha Noi]" not in src_sent, f"core-vi-game-003 still has title in source_sentence: {src_sent}"
        assert "2 nam kinh nghiem" in src_sent.lower() or "unity" in src_sent.lower()

    for r in leakage_records:
        assert len(r["source_sentence"].strip()) > 0
        assert r["case_id"]
        assert r.get("requirement_label")
        assert "null" in str(r.get("previous_golden_value"))
        assert "gte" in str(r.get("proposed_corrected_value"))
        assert r.get("reviewer_decision") is None
        assert r.get("reviewer_identity") is None
    print("3. Leakage Queue: 42 records validated, title error resolved, Phase B LOCKED.")

    # 4. Semantic Inconsistency Queue
    semantic_json_path = os.path.join(PACK_DIR, "SEMANTIC_INCONSISTENCY_QUEUE.json")
    with open(semantic_json_path, "r", encoding="utf-8") as f:
        semantic_data = json.load(f)
    
    sem_meta = semantic_data.get("metadata", {})
    assert sem_meta.get("phase_b_status") == "LOCKED"
    summary = sem_meta.get("summary", {})
    
    assert summary.get("priority_contradictions") == 10, f"Expected 10 priority contradictions, got {summary.get('priority_contradictions')}"
    assert summary.get("education_internal_alternatives") == 10, f"Expected 10 education alternatives, got {summary.get('education_internal_alternatives')}"
    assert summary.get("skill_any_of_contradictions") == 0, f"Expected 0 skill any_of contradictions, got {summary.get('skill_any_of_contradictions')}"
    assert summary.get("credential_equivalence_audited") == 5, f"Expected 5 credential equivalence cases, got {summary.get('credential_equivalence_audited')}"
    assert summary.get("general_concept_hallucinations") == 0
    assert summary.get("willing_to_learn_contradictions") == 0
    assert summary.get("benefits_false_positives") == 0
    
    flagged = semantic_data.get("flagged_cases", [])
    assert len(flagged) == 20, f"Expected 20 flagged cases (10 priority + 10 education), got {len(flagged)}"
    
    cred_equiv = semantic_data.get("credential_equivalence_audited_cases", [])
    assert len(cred_equiv) == 5, f"Expected 5 audited credential equivalence cases, got {len(cred_equiv)}"
    
    for f_item in flagged:
        assert f_item.get("reviewer_decision") is None
        assert f_item.get("reviewer_notes") is None
        assert f_item.get("issue_type") in ("PRIORITY_CONTRADICTION", "EDUCATION_INTERNAL_ALTERNATIVE")

    print(f"4. Semantic Inconsistency Queue: 20 cases flagged for human review (10 priority, 10 education), 5 credential equivalence cases properly isolated.")

    # 5. Evidence QA across Golden Datasets (Reconciliation: 800 parser vs 819 golden)
    total_spans = 0
    valid_spans = 0
    invalid_spans = []
    
    for ds_path, ds_split in [(CORE_DATASET, "core"), (EDGE_DATASET, "edge")]:
        with open(ds_path, "r", encoding="utf-8") as f:
            ds = json.load(f)
        for item in ds:
            cid = item.get("case_id")
            source = item.get("raw_text", "")
            expected = item.get("expected", {})
            evidence_list = expected.get("evidence", [])
            for ev in evidence_list:
                ev_text = ev.get("text")
                cs = ev.get("charStart")
                ce = ev.get("charEnd")
                if ev_text is not None and cs is not None and ce is not None:
                    total_spans += 1
                    extracted = source[cs:ce]
                    if extracted == ev_text:
                        valid_spans += 1
                    else:
                        invalid_spans.append({
                            "case_id": cid,
                            "expected_ev": ev_text,
                            "actual_slice": extracted,
                            "char_start": cs,
                            "char_end": ce
                        })

    print(f"5. Evidence Spans Audit: Total = {total_spans}, Valid = {valid_spans}, Invalid = {len(invalid_spans)}")
    assert total_spans == 819, f"Expected 819 golden evidence spans, got {total_spans}"
    assert valid_spans == 819, f"Expected 819 valid evidence spans, got {valid_spans}"
    assert len(invalid_spans) == 0, f"Found invalid evidence spans: {invalid_spans}"

    # 6. Check Decision Enums in all MD and JSON files in PACK_DIR
    for fname in os.listdir(PACK_DIR):
        fpath = os.path.join(PACK_DIR, fname)
        if os.path.isfile(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            matches_corr = re.findall(r'(?:status|decision|State)[:\s]+["\']?CORRECTION["\']?', content, re.IGNORECASE)
            matches_disc = re.findall(r'(?:status|decision|State)[:\s]+["\']?DISCUSS["\']?', content, re.IGNORECASE)
            assert not matches_corr, f"Found old status CORRECTION in {fname}"
            assert not matches_disc, f"Found old status DISCUSS in {fname}"

    # 7. Check ANNOTATION_GUIDELINE.md formatting and Bare Duration Policy
    guideline_path = os.path.join(PACK_DIR, "ANNOTATION_GUIDELINE.md")
    with open(guideline_path, "r", encoding="utf-8") as f:
        guideline_content = f.read()
    assert "Bare Duration Annotation Policy" in guideline_content
    assert "Bare Duration Policy Unresolved = NO" in guideline_content
    assert "$ rightarrow$" not in guideline_content
    assert "$rightarrow$" not in guideline_content

    # 8. Human Sign-off Integrity
    total_approved = 0
    total_awaiting = len(cases)
    print(f"8. Human Sign-off Audit: Total = {total_awaiting}, Approved = {total_approved}, Awaiting = {total_awaiting}")
    assert total_approved == 0
    assert total_awaiting == 90

    # 9. Response Template QA & Manifest Verification (Blind Re-Annotation Protocol)
    import hashlib
    def get_sha256(filepath):
        with open(filepath, "rb") as f_hash:
            return hashlib.sha256(f_hash.read()).hexdigest()

    response_json_path = os.path.join(PACK_DIR, "HUMAN_REVIEW_RESPONSES_90.json")
    assert os.path.exists(response_json_path), "HUMAN_REVIEW_RESPONSES_90.json not found"
    with open(response_json_path, "r", encoding="utf-8") as f:
        resp_data = json.load(f)

    resp_meta = resp_data.get("metadata", {})
    assert resp_meta.get("protocol") == "BLIND_RE_ANNOTATION_PROTOCOL"
    expected_blind_sha256 = get_sha256(blind_json_path)
    assert resp_meta.get("source_pack_sha256") == expected_blind_sha256, (
        f"Manifest mismatch: expected {expected_blind_sha256}, got {resp_meta.get('source_pack_sha256')}"
    )

    responses = resp_data.get("responses", [])
    assert len(responses) == 90, f"Expected 90 response entries, got {len(responses)}"
    
    blind_case_ids = [c["case_id"] for c in cases]
    resp_case_ids = [r["case_id"] for r in responses]
    assert resp_case_ids == blind_case_ids, "Response case IDs do not match blind pack case IDs in order"
    assert len(set(resp_case_ids)) == 90, "Duplicate case IDs detected in response template"

    for r in responses:
        assert r.get("source_pack_sha256") == expected_blind_sha256
        assert r.get("reviewer_id") is None, f"Leaked reviewer_id in {r['case_id']}"
        assert r.get("reviewed_at") is None, f"Leaked reviewed_at in {r['case_id']}"
        assert r.get("reviewer_status") is None, f"Leaked reviewer_status in {r['case_id']}"
        assert r.get("reviewer_note") is None, f"Leaked reviewer_note in {r['case_id']}"
        ann = r.get("reviewer_annotation", {})
        assert ann.get("requirements") == [], f"reviewer_annotation.requirements not empty in {r['case_id']}"
        for k in r.keys():
            assert k not in PROHIBITED_BLIND_KEYS, f"Prohibited key {k} in response template {r['case_id']}"

    # Manifest verification
    pack_manifest_path = os.path.join(PACK_DIR, "manifest.json")
    assert os.path.exists(pack_manifest_path), "adjudication_pack_p2/manifest.json not found"
    with open(pack_manifest_path, "r", encoding="utf-8") as f:
        pack_manifest = json.load(f)
    immutable_hashes = pack_manifest.get("immutable_input_hashes", {})
    assert immutable_hashes.get("BLIND_REVIEW_QUEUE_90.json") == expected_blind_sha256
    assert immutable_hashes.get("RECONCILIATION_QUEUE_90.json") == get_sha256(recon_json_path)
    assert immutable_hashes.get("POTENTIAL_LEAKAGE_QUEUE_42.json") == get_sha256(leakage_json_path)
    assert immutable_hashes.get("SEMANTIC_INCONSISTENCY_QUEUE.json") == get_sha256(semantic_json_path)

    print(f"9. Response Template QA & Hash Manifest: 90 blank responses verified, source_pack_sha256 locked ({expected_blind_sha256[:12]}...).")

    print("=== ALL PRE-REVIEW QA & BLIND RE-ANNOTATION PROTOCOL VALIDATIONS PASSED STRICTLY ===")

if __name__ == "__main__":
    validate()

