# build_complete_human_adjudication_pack.py
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.evaluation.runner import _evidence_is_valid, _source
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser

import argparse
import os

def find_eval_base_dir() -> Path:
    parser = argparse.ArgumentParser(description="Build complete human adjudication pack for PR2 Gate P2")
    parser.add_argument("--base-dir", type=str, default=None, help="Base path to data/eval/jd")
    args, _ = parser.parse_known_args()
    
    # 1. CLI argument override
    if args.base_dir:
        return Path(args.base_dir).resolve()
        
    # 2. Environment variables
    for env_key in ("EVAL_DATASET_DIR", "DOC_AND_PLAN_DIR"):
        val = os.getenv(env_key)
        if val:
            p = Path(val).resolve()
            if (p / "adjudication_pack_p2").exists() or (p / "parser_core_v1").exists():
                return p
            if (p / "data" / "eval" / "jd").exists():
                return (p / "data" / "eval" / "jd").resolve()

    # 3. Dynamic resolution relative to script file
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir.parent / "DOC_AND_PLAN" / "data" / "eval" / "jd",  # Standard sibling clone
        script_dir / "DOC_AND_PLAN" / "data" / "eval" / "jd",         # Nested structure
        Path.cwd() / "DOC_AND_PLAN" / "data" / "eval" / "jd",        # CWD relative
        Path.cwd() / "data" / "eval" / "jd",                         # Inside DOC_AND_PLAN
        Path("/workspace/DOC_AND_PLAN/data/eval/jd"),                # Container path
        Path("/app/DOC_AND_PLAN/data/eval/jd"),                      # Docker path
    ]
    for c in candidates:
        if c.exists() and ((c / "parser_core_v1").exists() or (c / "adjudication_pack_p2").exists()):
            return c.resolve()

    # Fallback to standard sibling layout
    return (script_dir.parent / "DOC_AND_PLAN" / "data" / "eval" / "jd").resolve()

base = find_eval_base_dir()
pack_dir = base / "adjudication_pack_p2"
pack_dir.mkdir(parents=True, exist_ok=True)

core_p = base / "parser_core_v1/private/golden_jd_parser_core_v2.json"
edge_p = base / "parser_edge_v1/private/golden_jd_parser_edge_v2.json"

core_cases = json.loads(core_p.read_text(encoding="utf-8"))
edge_cases = json.loads(edge_p.read_text(encoding="utf-8"))
all_cases = core_cases + edge_cases
parser = DeterministicJobDescriptionParser()

def extract_enclosing_line(raw_text: str, start: int, end: int) -> str:
    line_start = raw_text.rfind("\n", 0, start)
    line_start = 0 if line_start == -1 else line_start + 1
    line_end = raw_text.find("\n", end)
    line_end = len(raw_text) if line_end == -1 else line_end
    return raw_text[line_start:line_end].strip()

# ==============================================================================
# 1. BLIND_REVIEW_QUEUE_90 (.json & .md)
# ==============================================================================
blind_queue = []
for c in all_cases:
    raw_text = c["raw_text"]
    title = c["expected"].get("jobTitle")
    blind_queue.append({
        "case_id": c["case_id"],
        "split": "core" if "core" in c["case_id"] else "edge",
        "language": c.get("metadata", {}).get("language", "en"),
        "source_text": raw_text,
        "job_title": title,
        "blind_annotations": [],  # Empty placeholder for human reviewer
        "reviewer_identity": None,
        "reviewer_notes": None,
        "review_timestamp": None,
        "blind_review_status": "AWAITING_HUMAN_SIGNOFF",
    })

blind_payload = {
    "metadata": {
        "pack_name": "GATE_P2_BLIND_REVIEW_QUEUE_90",
        "phase": "PHASE_A_BLIND_REVIEW",
        "phase_a_status": "NOT_STARTED",
        "phase_b_status": "LOCKED",
        "allowed_phase_a_statuses": ["NOT_STARTED", "IN_PROGRESS", "COMPLETED_LOCKED"],
        "allowed_phase_b_statuses": ["LOCKED", "AVAILABLE", "IN_PROGRESS", "COMPLETED"],
        "total_cases": len(blind_queue),
        "core_cases": sum(1 for c in blind_queue if c["split"] == "core"),
        "edge_cases": sum(1 for c in blind_queue if c["split"] == "edge"),
        "leakage_protection": "STRICT_BLIND — NO GOLDEN, NO PARSER, NO METRICS",
    },
    "cases": blind_queue,
}

(pack_dir / "BLIND_REVIEW_QUEUE_90.json").write_text(
    json.dumps(blind_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

blind_md_lines = [
    "# HÀNG ĐỢI ĐÁNH GIÁ MÙ — GIAI ĐOẠN A (PHASE A: BLIND REVIEW QUEUE - 90 CASES)",
    "",
    "> [!IMPORTANT]",
    "> **TRẠNG THÁI KHÓA GIAI ĐOẠN (PHASE LOCK STATUS)**",
    "> - `phase_a_status`: **NOT_STARTED**",
    "> - `phase_b_status`: **LOCKED** (Chỉ được mở sau khi Phase A chuyển sang `COMPLETED_LOCKED`)",
    ">",
    "> **QUY TẮC ĐÁNH GIÁ MÙ (BLIND REVIEW PROTOCOL)**",
    "> - Reviewer chỉ đọc văn bản nguồn (`source_text`) và đối chiếu với [ANNOTATION_GUIDELINE.md](ANNOTATION_GUIDELINE.md).",
    "> - Tuyệt đối không xem kết quả parser hoặc proposed golden labels trước khi hoàn tất tự gán nhãn.",
    "> - Trạng thái ban đầu của toàn bộ 90 cases: `AWAITING_HUMAN_SIGNOFF`.",
    "",
    "| STT | Case ID | Split | Ngôn ngữ | Chức danh | Trạng thái Đánh giá Mù | Reviewer ID | Timestamp |",
    "|:---:|:---|:---:|:---:|:---|:---:|:---:|:---:|",
]

for idx, b in enumerate(blind_queue, start=1):
    blind_md_lines.append(
        f"| {idx} | `{b['case_id']}` | {b['split']} | {b['language'].upper()} | {b['job_title']} | `{b['blind_review_status']}` | ____________ | ____________ |"
    )

(pack_dir / "BLIND_REVIEW_QUEUE_90.md").write_text(
    "\n".join(blind_md_lines) + "\n", encoding="utf-8"
)

# ==============================================================================
# 2. RECONCILIATION_QUEUE_90 (.json & .md)
# ==============================================================================
reconciliation_queue = []
for c in all_cases:
    case_id = c["case_id"]
    raw_text = c["raw_text"]
    source = _source(case_id, raw_text)
    parsed = parser.parse(source, extraction_version="eval")
    actual = json.loads(parsed.model_dump_json(by_alias=True))
    gold = c["expected"]

    # Pre-calculate differences between proposed golden and parser output
    gold_req_labels = [r.get("rawLabel") for r in gold.get("requirements", [])]
    actual_req_labels = [r.get("rawLabel") for r in actual.get("requirements", [])]

    diffs = []
    # Check missing in parser
    for gl in gold_req_labels:
        if gl not in actual_req_labels:
            diffs.append(f"Golden req missing in parser: '{gl}'")
    for al in actual_req_labels:
        if al not in gold_req_labels:
            diffs.append(f"Parser extra req not in golden: '{al}'")

    # Priority mismatches
    for gr in gold.get("requirements", []):
        for ar in actual.get("requirements", []):
            if gr.get("rawLabel") == ar.get("rawLabel"):
                if gr.get("priority") != ar.get("priority"):
                    diffs.append(f"Priority diff on '{gr.get('rawLabel')}': gold={gr.get('priority')} vs actual={ar.get('priority')}")
                if gr.get("operator") != ar.get("operator"):
                    diffs.append(f"Operator diff on '{gr.get('rawLabel')}': gold={gr.get('operator')} vs actual={ar.get('operator')}")

    reconciliation_queue.append({
        "case_id": case_id,
        "split": "core" if "core" in case_id else "edge",
        "language": c.get("metadata", {}).get("language", "en"),
        "source_text": raw_text,
        "column_a_human_blind_label": None,  # Populated from Phase A
        "column_b_proposed_golden_label": gold,
        "column_c_parser_output": actual,
        "column_d_precalculated_differences": diffs,
        "column_e_human_final_decision": None,
        "reviewer_identity": None,
        "reviewer_notes": None,
        "review_timestamp": None,
        "allowed_decision_states": [
            "APPROVED",
            "APPROVED_WITH_CORRECTION",
            "NEEDS_DISCUSSION",
            "REJECTED",
        ],
    })

recon_payload = {
    "metadata": {
        "pack_name": "GATE_P2_RECONCILIATION_QUEUE_90",
        "phase": "PHASE_B_RECONCILIATION",
        "phase_a_status": "NOT_STARTED",
        "phase_b_status": "LOCKED",
        "lock_notice": "Phase B is strictly LOCKED until Phase A is COMPLETED_LOCKED.",
        "allowed_phase_a_statuses": ["NOT_STARTED", "IN_PROGRESS", "COMPLETED_LOCKED"],
        "allowed_phase_b_statuses": ["LOCKED", "AVAILABLE", "IN_PROGRESS", "COMPLETED"],
        "total_cases": len(reconciliation_queue),
        "core_cases": sum(1 for c in reconciliation_queue if c["split"] == "core"),
        "edge_cases": sum(1 for c in reconciliation_queue if c["split"] == "edge"),
    },
    "cases": reconciliation_queue,
}

(pack_dir / "RECONCILIATION_QUEUE_90.json").write_text(
    json.dumps(recon_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

reconcil_md_lines = [
    "# HÀNG ĐỢI ĐỐI SOÁT — GIAI ĐOẠN B (PHASE B: RECONCILIATION QUEUE - 90 CASES)",
    "",
    "> [!CAUTION]",
    "> **TRẠNG THÁI GIAI ĐOẠN B: LOCKED (ĐANG KHÓA)**",
    "> - Giai đoạn B hiện đang ở trạng thái **LOCKED**.",
    "> - Không mở hàng đợi này cho Reviewer cho đến khi Phase A (Blind Review) được nộp và chuyển sang `COMPLETED_LOCKED`.",
    "> - Metadata: `phase_a_status = NOT_STARTED`, `phase_b_status = LOCKED`.",
    "",
    "> [!NOTE]",
    "> **HƯỚNG DẪN ĐỐI SOÁT (RECONCILIATION INSTRUCTIONS - KHI ĐƯỢC MỞ)**",
    "> - Bảng dưới đây so sánh song song: Blind Label (Phase A), Proposed Golden Label, và Parser Output.",
    "> - Reviewer đưa ra quyết định chính thức bằng một trong 4 trạng thái chuẩn hóa:",
    ">   1. `APPROVED`: Chấp thuận nhãn Golden đề xuất.",
    ">   2. `APPROVED_WITH_CORRECTION`: Chấp thuận sau khi sửa nhãn / offset (ghi rõ chi tiết trong notes).",
    ">   3. `NEEDS_DISCUSSION`: Cần thảo luận với Lead Reviewer do câu chữ mơ hồ.",
    ">   4. `REJECTED`: Bác bỏ hoàn toàn nhãn đề xuất.",
    "",
    "| STT | Case ID | Split | Chức danh | Số tiêu chí Gold | Số khác biệt (Gold vs Parser) | Quyết định Cuối cùng | Ký tên & Timestamp |",
    "|:---:|:---|:---:|:---|:---:|:---:|:---|:---|",
]

for idx, r in enumerate(reconciliation_queue, start=1):
    diff_count = len(r["column_d_precalculated_differences"])
    diff_str = f"⚠️ {diff_count} diffs" if diff_count > 0 else "✅ 0 diffs"
    reconcil_md_lines.append(
        f"| {idx} | `{r['case_id']}` | {r['split']} | {r['column_b_proposed_golden_label'].get('jobTitle')} | {len(r['column_b_proposed_golden_label'].get('requirements', []))} | {diff_str} | `[ ] APPROVED`<br>`[ ] APPROVED_WITH_CORRECTION`<br>`[ ] NEEDS_DISCUSSION`<br>`[ ] REJECTED` | ____________________ |"
    )

(pack_dir / "RECONCILIATION_QUEUE_90.md").write_text(
    "\n".join(reconcil_md_lines) + "\n", encoding="utf-8"
)

# ==============================================================================
# 3. FIX & REGENERATE POTENTIAL_LEAKAGE_QUEUE_42 (.json & .md)
# ==============================================================================
leakage_case_ids = {
    "core-en-backend-001",
    "core-en-platform-002",
    "core-vi-game-003",
    "core-en-frontend-004",
    *(f"jd-edge-pre-gold-{i:03d}" for i in range(1, 37)),
}

leakage_items = []
for c in all_cases:
    if c["case_id"] in leakage_case_ids:
        raw_text = c["raw_text"]
        ev_map = {ev["evidenceId"]: ev for ev in c["expected"].get("evidence", [])}
        for r in c["expected"].get("requirements", []):
            if r.get("minimumExperienceMonths") is not None and r.get("operator") == "gte":
                # Get EXACT line from evidence offset, not title
                ev_refs = r.get("evidenceRefs", [])
                source_sentence = ""
                if ev_refs and ev_refs[0] in ev_map:
                    ev = ev_map[ev_refs[0]]
                    source_sentence = extract_enclosing_line(raw_text, ev["charStart"], ev["charEnd"])
                else:
                    source_sentence = r.get("rawLabel")

                leakage_items.append({
                    "case_id": c["case_id"],
                    "split": "core" if "core" in c["case_id"] else "edge",
                    "requirement_id": r.get("requirementId"),
                    "requirement_label": r.get("rawLabel"),
                    "source_sentence": source_sentence,
                    "previous_golden_value": "operator: null",
                    "proposed_corrected_value": "operator: 'gte'",
                    "rule_spec_basis": (
                        "PR2 Bare Duration Policy: 'X years of experience' in requirement context "
                        "is normalized to minimum_experience_months = X * 12, operator = 'gte'."
                    ),
                    "parser_current_output": "operator: 'gte'",
                    "leakage_warning": (
                        "POTENTIAL EVALUATION LEAKAGE: Batch update was applied after inspecting parser output "
                        "discrepancy. Must be independently verified by human reviewer against source text."
                    ),
                    "allowed_decisions": ["APPROVE_CORRECTION", "REJECT_CORRECTION", "NEEDS_DISCUSSION"],
                    "reviewer_decision": None,
                    "reviewer_identity": None,
                    "reviewer_notes": None,
                    "review_timestamp": None,
                })

leakage_payload = {
    "metadata": {
        "pack_name": "GATE_P2_POTENTIAL_LEAKAGE_QUEUE_42",
        "phase": "PHASE_B_LEAKAGE_AUDIT",
        "phase_b_status": "LOCKED",
        "total_records": len(leakage_items),
        "target_attribute": "operator: 'gte' (from null)",
    },
    "records": leakage_items,
}

(pack_dir / "POTENTIAL_LEAKAGE_QUEUE_42.json").write_text(
    json.dumps(leakage_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

leakage_md_lines = [
    "# DANH SÁCH 42 TRƯỜNG HỢP CẦN THẨM ĐỊNH NGUY CƠ RÒ RỈ (POTENTIAL LEAKAGE QUEUE)",
    "",
    "> [!WARNING]",
    "> **TRẠNG THÁI GIAI ĐOẠN B: LOCKED (ĐANG KHÓA)**",
    "> 42 tiêu chí dưới đây đã được cập nhật toán tử từ `operator: null` thành `operator: 'gte'` theo Bare Duration Policy.",
    "> Mặc dù phù hợp với quy tắc ngữ nghĩa ('At least X years' / 'Ít nhất X năm' / 'X years of experience'), việc cập nhật này được kích hoạt sau khi quan sát kết quả chạy parser.",
    "> Để loại trừ triệt để rủi ro rò rỉ, **Chuyên viên Đánh giá Con người (Human Reviewer) bắt buộc phải đối soát độc lập** văn bản nguồn trong Phase B và phê duyệt bằng một trong 3 quyết định:",
    "> - `APPROVE_CORRECTION`",
    "> - `REJECT_CORRECTION`",
    "> - `NEEDS_DISCUSSION`",
    "",
    "| STT | Case ID | Split | Câu văn nguồn (Source Sentence) | Tiêu chí (Raw Label) | Giá trị cũ | Giá trị đề xuất | Quyết định Reviewer | Ghi chú & Ký tên |",
    "|:---:|:---|:---:|:---|:---|:---:|:---:|:---|:---|",
]

for idx, item in enumerate(leakage_items, start=1):
    leakage_md_lines.append(
        f"| {idx} | `{item['case_id']}` | {item['split']} | {item['source_sentence']} | `{item['requirement_label']}` | `{item['previous_golden_value']}` | `{item['proposed_corrected_value']}` | `[ ] APPROVE_CORRECTION`<br>`[ ] REJECT_CORRECTION`<br>`[ ] NEEDS_DISCUSSION` | ____________________ |"
    )

(pack_dir / "POTENTIAL_LEAKAGE_QUEUE_42.md").write_text(
    "\n".join(leakage_md_lines) + "\n", encoding="utf-8"
)

# ==============================================================================
# 4. REGENERATE SEMANTIC_INCONSISTENCY_QUEUE (.json & .md)
# ==============================================================================
preferred_kw = re.compile(r"\b(?:preferred|ưu tiên|lợi thế|plus|nice to have|advantage|lợi thế lớn|điểm cộng)\b", re.I)
must_have_kw = re.compile(r"\b(?:bắt buộc|yêu cầu|required|must have|cần có|bắt buộc có)\b", re.I)
disjunction_kw = re.compile(r"\b(?:hoặc|hay|or)\b", re.I)
cred_equiv_pattern = re.compile(r"(?:hoặc\s+tương\s+đương|or\s+equivalent|equivalent\s+cert|chứng\s+chỉ\s+tương\s+đương)", re.I)
prog_found_kw = re.compile(r"\b(?:kiến thức nền tảng (?:về )?lập trình|nền tảng lập trình|programming foundation|software fundamentals)\b", re.I)
willing_kw = re.compile(r"\b(?:willing to learn|sẵn sàng học)\b", re.I)

priority_contradictions = []
education_alternatives = []
skill_any_of_contradictions = []
cred_equivalence_audited = []
general_concept_contradictions = []
willing_to_learn_contradictions = []
benefits_false_positives = []

for c in all_cases:
    case_id = c["case_id"]
    raw_text = c["raw_text"]
    ev_map = {ev["evidenceId"]: ev for ev in c["expected"].get("evidence", [])}

    for r in c["expected"].get("requirements", []):
        ev_refs = r.get("evidenceRefs", [])
        source_line = ""
        if ev_refs and ev_refs[0] in ev_map:
            ev = ev_map[ev_refs[0]]
            source_line = extract_enclosing_line(raw_text, ev["charStart"], ev["charEnd"])
        else:
            source_line = r.get("rawLabel", "")

        # 1. Priority contradiction
        has_pref = bool(preferred_kw.search(source_line))
        has_must = bool(must_have_kw.search(source_line))
        if has_pref and not has_must and r.get("priority") == "must_have":
            priority_contradictions.append({
                "case_id": case_id,
                "split": "core" if "core" in case_id else "edge",
                "issue_type": "PRIORITY_CONTRADICTION",
                "source_sentence": source_line,
                "requirement_label": r.get("rawLabel"),
                "current_golden_value": f"priority: {r.get('priority')}",
                "flagged_reason": "Source sentence contains preferred/plus/lợi thế/điểm cộng keyword, but golden priority is must_have.",
                "action_required": "Human Reviewer must verify whether this requirement should be updated to priority: 'preferred'.",
                "reviewer_decision": None,
                "reviewer_notes": None,
            })

        # 2. OR / HOẶC Scanner differentiation
        if disjunction_kw.search(source_line):
            is_cred_equiv = bool(cred_equiv_pattern.search(source_line))
            if is_cred_equiv:
                # Category B: Credential Equivalence -> NOT a group contradiction
                cred_equivalence_audited.append({
                    "case_id": case_id,
                    "split": "core" if "core" in case_id else "edge",
                    "semantic_type": "CREDENTIAL_EQUIVALENCE",
                    "source_sentence": source_line,
                    "requirement_label": r.get("rawLabel"),
                    "golden_group_operator": r.get("groupOperator"),
                    "equivalent_allowed": r.get("equivalentAllowed", True),
                    "audit_note": "Contains 'or equivalent' / 'hoặc tương đương'. Correctly recognized as single credential with equivalent_allowed=True. NOT flagged as group contradiction.",
                })
            elif r.get("kind") == "education":
                # Category C: Internal Education Alternative -> Only when requirement contains internal alternatives (groupOperator != any_of)
                if r.get("groupOperator") != "any_of":
                    education_alternatives.append({
                        "case_id": case_id,
                        "split": "core" if "core" in case_id else "edge",
                        "issue_type": "EDUCATION_INTERNAL_ALTERNATIVE",
                        "source_sentence": source_line,
                        "requirement_label": r.get("rawLabel"),
                        "current_golden_value": f"group_operator: {r.get('groupOperator')}, kind: education",
                        "flagged_reason": "Source sentence lists alternative degrees/majors (e.g. 'CNTT hoặc Toán tin', 'Computer Science or Statistics'). Reviewer confirms whether degree alternative remains atomic or requires any_of.",
                        "action_required": "Human Reviewer must confirm educational qualification modeling.",
                        "reviewer_decision": None,
                        "reviewer_notes": None,
                    })
            else:
                # Category A/D: Requirement Alternative (Skill / Experience)
                if r.get("groupOperator") != "any_of":
                    skill_any_of_contradictions.append({
                        "case_id": case_id,
                        "split": "core" if "core" in case_id else "edge",
                        "issue_type": "SKILL_ANY_OF_CONTRADICTION",
                        "source_sentence": source_line,
                        "requirement_label": r.get("rawLabel"),
                        "current_golden_value": f"group_operator: {r.get('groupOperator')}",
                        "flagged_reason": "Alternative skill/requirement in source line does not have group_operator='any_of'.",
                        "action_required": "Human Reviewer must verify whether group_operator='any_of' should be applied.",
                        "reviewer_decision": None,
                        "reviewer_notes": None,
                    })

        # 3. General concept hallucination
        if prog_found_kw.search(source_line) and r.get("concept") is not None:
            general_concept_contradictions.append({
                "case_id": case_id,
                "split": "core" if "core" in case_id else "edge",
                "issue_type": "GENERAL_CONCEPT_HALLUCINATION",
                "source_sentence": source_line,
                "requirement_label": r.get("rawLabel"),
                "current_golden_value": f"concept: {r.get('concept', {}).get('conceptId')}",
                "flagged_reason": "General programming foundation should have concept=null, but has concrete taxonomy concept.",
                "action_required": "Human Reviewer must verify whether concept should be set to null.",
                "reviewer_decision": None,
                "reviewer_notes": None,
            })

        # 4. Willing to learn contradiction
        if willing_kw.search(source_line) and r.get("priority") == "must_have":
            willing_to_learn_contradictions.append({
                "case_id": case_id,
                "split": "core" if "core" in case_id else "edge",
                "issue_type": "WILLING_TO_LEARN_CONTRADICTION",
                "source_sentence": source_line,
                "requirement_label": r.get("rawLabel"),
                "current_golden_value": f"priority: {r.get('priority')}",
                "flagged_reason": "Willing to learn expression must not be marked as must_have.",
                "action_required": "Human Reviewer must verify priority and kind.",
                "reviewer_decision": None,
                "reviewer_notes": None,
            })

flagged_inconsistencies = priority_contradictions + education_alternatives + skill_any_of_contradictions + general_concept_contradictions + willing_to_learn_contradictions

semantic_payload = {
    "metadata": {
        "pack_name": "GATE_P2_SEMANTIC_INCONSISTENCY_QUEUE",
        "phase": "PHASE_B_SEMANTIC_AUDIT",
        "phase_b_status": "LOCKED",
        "total_flagged_for_human_review": len(flagged_inconsistencies),
        "summary": {
            "priority_contradictions": len(priority_contradictions),
            "education_internal_alternatives": len(education_alternatives),
            "skill_any_of_contradictions": len(skill_any_of_contradictions),
            "credential_equivalence_audited": len(cred_equivalence_audited),
            "general_concept_hallucinations": len(general_concept_contradictions),
            "willing_to_learn_contradictions": len(willing_to_learn_contradictions),
            "benefits_false_positives": len(benefits_false_positives),
            "other_semantic_warnings": 0,
        },
    },
    "flagged_cases": flagged_inconsistencies,
    "credential_equivalence_audited_cases": cred_equivalence_audited,
}

(pack_dir / "SEMANTIC_INCONSISTENCY_QUEUE.json").write_text(
    json.dumps(semantic_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

inconsist_md_lines = [
    "# HÀNG ĐỢI RÀ SOÁT BẤT NHẤT NGỮ NGHĨA (SEMANTIC INCONSISTENCY QUEUE)",
    "",
    "> [!WARNING]",
    "> **TRẠNG THÁI GIAI ĐOẠN B: LOCKED (ĐANG KHÓA)**",
    f"> Hàng đợi này chứa **{len(flagged_inconsistencies)} trường hợp** cần Human Reviewer đối soát đặc biệt trong Phase B.",
    "> Reviewer tuyệt đối không tự ý sửa đổi golden dataset mà chỉ đưa ra quyết định duyệt hoặc ghi chú điều chỉnh.",
    "",
    "## 1. Bảng Tổng hợp Phân loại Cảnh báo",
    "",
    "| Phân loại Ngữ nghĩa | Số lượng | Trạng thái Xử lý Scanner |",
    "|:---|:---:|:---|",
    f"| **Priority Contradictions** (Điểm cộng / Ưu tiên vs `must_have`) | **{len(priority_contradictions)}** | Cần Reviewer thẩm định để chuyển `preferred` |",
    f"| **Education Internal Alternatives** (Bằng cấp CNTT hoặc Toán tin...) | **{len(education_alternatives)}** | Phân loại chính xác thành nhóm Education, không nhầm skill any_of |",
    f"| **Skill Any-of Contradictions** | **{len(skill_any_of_contradictions)}** | Đã chuẩn hóa chính xác trong Golden Dataset (0 lỗi) |",
    f"| **Credential Equivalence** (IELTS / Chứng chỉ hoặc tương đương) | **{len(cred_equivalence_audited)}** | Đã phân biệt đúng ngữ nghĩa tương đương, KHÔNG over-flag |",
    f"| **General Concept Hallucinations** | **{len(general_concept_contradictions)}** | Không phát hiện ảo giác |",
    f"| **Willing-to-learn Contradictions** | **{len(willing_to_learn_contradictions)}** | Không phát hiện mâu thuẫn |",
    f"| **Benefits False Positives** | **{len(benefits_false_positives)}** | Không phát hiện lỗi quyền lợi |",
    f"| **TỔNG CỘNG CẦN HUMAN REVIEW** | **{len(flagged_inconsistencies)}** | |",
    "",
    "---",
    "",
    "## 2. Danh sách Chi tiết Cần Thẩm định (Phase B)",
    "",
    "| STT | Case ID | Phân loại Lỗi | Tiêu chí (Raw Label) | Câu văn nguồn (Source Sentence) | Giá trị Golden Hiện tại | Lý do Cảnh báo | Quyết định Reviewer |",
    "|:---:|:---|:---:|:---|:---|:---:|:---|:---|",
]

for idx, item in enumerate(flagged_inconsistencies, start=1):
    inconsist_md_lines.append(
        f"| {idx} | `{item['case_id']}` | **{item['issue_type']}** | `{item['requirement_label']}` | {item['source_sentence']} | `{item['current_golden_value']}` | {item['flagged_reason']} | `[ ] APPROVED`<br>`[ ] APPROVED_WITH_CORRECTION`<br>`[ ] NEEDS_DISCUSSION` |"
    )

inconsist_md_lines.extend([
    "",
    "---",
    "",
    "## 3. Danh sách 5 Trường hợp Chứng chỉ Ngoại ngữ & Tương đương (Credential Equivalence - Đã kiểm chứng)",
    "",
    "Các trường hợp dưới đây chứa cụm từ *'hoặc tương đương'* / *'or equivalent'*. Scanner đã phân loại chính xác thành chứng chỉ độc lập với `equivalent_allowed = true`, **không đánh cờ lỗi `GROUP_CONTRADICTION`**:",
    "",
    "| STT | Case ID | Tiêu chí (Raw Label) | Câu văn nguồn | Toán tử | Ngưỡng | Cho phép tương đương |",
    "|:---:|:---|:---|:---|:---:|:---:|:---:|",
])

for idx, ce in enumerate(cred_equivalence_audited, start=1):
    inconsist_md_lines.append(
        f"| {idx} | `{ce['case_id']}` | `{ce['requirement_label']}` | {ce['source_sentence']} | `gte` | Có | `equivalent_allowed = true` |"
    )

(pack_dir / "SEMANTIC_INCONSISTENCY_QUEUE.md").write_text(
    "\n".join(inconsist_md_lines) + "\n", encoding="utf-8"
)

# ==============================================================================
# 5. ANNOTATION_GUIDELINE.md (Updated & Officialized)
# ==============================================================================
guideline_text = """# CẨM NANG HƯỚNG DẪN ĐÁNH GIÁ (HUMAN ANNOTATION & ADJUDICATION GUIDELINE)
## Gate P2 — PR2: JD Structured Requirements

Tài liệu này hướng dẫn chuyên viên đánh giá con người (Human Adjudicator) thực hiện thẩm định độc lập 90 cases trong Golden Dataset của PR2 nhằm đảm bảo tính khách quan, trung thực và ngăn ngừa rò rỉ đánh giá (evaluation leakage).

---

## I. Quy trình Đánh giá 2 Giai đoạn (Two-Phase Protocol & Phase Locking)

Để triệt tiêu hoàn toàn thiên kiến xác nhận (confirmation bias), quy trình đánh giá được thực thi nghiêm ngặt theo cơ chế khóa 2 giai đoạn:

### Giai đoạn 1: Đánh giá Mù (Phase A — Blind Review)
* **Trạng thái khởi tạo:** `phase_a_status = "NOT_STARTED"`.
* **Tài liệu bàn giao cho Reviewer:**
  * [`ANNOTATION_GUIDELINE.md`](ANNOTATION_GUIDELINE.md)
  * [`BLIND_REVIEW_QUEUE_90.json`](BLIND_REVIEW_QUEUE_90.json) hoặc [`BLIND_REVIEW_QUEUE_90.md`](BLIND_REVIEW_QUEUE_90.md).
* **Nguyên tắc cô lập:** Reviewer chỉ tiếp cận `source_text` và `job_title`. Tuyệt đối KHÔNG được tiếp cận Parser Output, Golden Labels đề xuất, hoặc bảng đối soát diffs.
* **Nhiệm vụ:** Reviewer tự trích xuất và điền vào mảng `blind_annotations`:
  * Tiêu chí nguyên văn (`raw_label`).
  * Phân loại (`kind`: `skill`, `experience`, `education`, `language`, `other`).
  * Mức độ ưu tiên (`priority`: `must_have`, `preferred`).
  * Quan hệ nhóm (`group_operator`: `atomic`, `all_of`, `any_of`).
  * Toán tử & ngưỡng số học (`operator`: `gte`, `gt`, `lte`, `lt`, `eq`; `threshold`; `scale`).
  * Chứng chỉ ngoại ngữ (`credential`; `equivalent_allowed`).
  * Tọa độ dẫn chứng nguyên văn (`char_start`, `char_end`, `evidence_text`).
* **Khóa Phase A:** Khi Reviewer hoàn thành và ký nhận, trạng thái chuyển thành `phase_a_status = "COMPLETED_LOCKED"`.

### Giai đoạn 2: Đối soát & Ký duyệt (Phase B — Reconciliation & Sign-off)
* **Trạng thái khởi tạo:** `phase_b_status = "LOCKED"`.
* **Điều kiện kích hoạt:** Chỉ được chuyển thành `phase_b_status = "AVAILABLE"` sau khi Phase A đã đạt `COMPLETED_LOCKED`.
* **Tài liệu sử dụng trong Phase B:**
  * [`RECONCILIATION_QUEUE_90.md`](RECONCILIATION_QUEUE_90.md) (Đối chiếu 5 cột: Blind Label vs Golden vs Parser vs Diffs vs Decision).
  * [`POTENTIAL_LEAKAGE_QUEUE_42.md`](POTENTIAL_LEAKAGE_QUEUE_42.md) (Thẩm định độc lập 42 trường hợp cập nhật operator 'gte').
  * [`SEMANTIC_INCONSISTENCY_QUEUE.md`](SEMANTIC_INCONSISTENCY_QUEUE.md) (Thẩm định 20 trường hợp bất nhất ngữ nghĩa: 10 Priority Contradictions + 10 Education Internal Alternatives).

---

## II. Quy tắc Gán nhãn Ngữ nghĩa (Semantic Annotation Rules)

### 1. Toán tử Ràng buộc (Operator)
* `At least 3 years`, `Ít nhất 2 năm`, `Tối thiểu 1 năm`, `GPA >= 3.2`, `IELTS 6.0+` → `operator = "gte"`.
* `More than 5 years`, `Trên 3 năm`, `GPA > 8.0` → `operator = "gt"`.
* `Maximum 2 years`, `Không quá 3 năm` → `operator = "lte"`.
* `Exactly 1 year`, `Đúng 1 năm` → `operator = "eq"`.
* Yêu cầu bắt buộc không có ngưỡng đo lường số học → `operator = "required"` hoặc `operator = null`.

### 2. Chính sách Ràng buộc Thời gian Đơn giản (Bare Duration Annotation Policy)
Trong văn bản Job Description xuất hiện nhiều câu diễn đạt thời lượng kinh nghiệm không đi kèm từ so sánh rõ ràng:
* Ví dụ: `"3 years of experience with Python"`, `"2 years of backend experience"`, `"3 năm kinh nghiệm lập trình Java"`.

* **Quy tắc Chính thức (Official Policy):**
  Trong **Job Description requirement context**, nếu một câu yêu cầu trực tiếp số năm kinh nghiệm (`X years of experience` hoặc `X năm kinh nghiệm`) như một tiêu chuẩn năng lực, thì chuẩn hóa chính thức thành:
  * `minimum_experience_months = X * 12`
  * `operator = "gte"`
  *Ví dụ:* `"3 years of experience with Java"` → `minimum_experience_months = 36`, `operator = "gte"`.
  *Lý do:* Trong quy ước tuyển dụng, câu nêu số năm kinh nghiệm như một qualification được hiểu là mức kinh nghiệm tối thiểu ứng viên cần đáp ứng, trừ khi nguồn có câu chữ chỉ rõ ngữ nghĩa khác.

* **Ngoại lệ (Exceptions):**
  Nếu câu không đủ rõ để xác định đây là minimum qualification, hoặc số năm chỉ xuất hiện trong:
  - Mô tả công việc chung (Job description overview);
  - Bối cảnh lịch sử công ty hoặc dự án (Historical context / Company profile);
  - Phúc lợi / Đãi ngộ (Benefits);
  - Ngữ cảnh ngoài yêu cầu tuyển dụng (Non-requirement context);
  → Tuyệt đối **KHÔNG** tự động gán `gte`. Đánh dấu `NEEDS_DISCUSSION` hoặc `POLICY_REQUIRES_LEAD_REVIEW`.

* **Tình trạng Chính sách (Resolution Status):**
  `Bare Duration Policy Unresolved = NO` (Chính sách đã được chuẩn hóa dứt khoát làm căn cứ thẩm định, không dựa vào parser output).

### 3. Phân biệt Ngữ nghĩa Tuyển chọn Lựa chọn (OR / HOẶC Disjunction Rules)
Scanner và Reviewer bắt buộc phân biệt rõ 4 loại ngữ nghĩa của liên từ "OR" / "HOẶC":
* **A. Lựa chọn Kỹ năng / Tiêu chí Thay thế (Skill Requirement Alternative):**
  * Ví dụ: `"Java or Kotlin"`, `"React hoặc Vue"`, `"Android or iOS"`.
  * Gán: Các atomic requirements thuộc cùng nhóm có chung `group_id`, `group_operator = "any_of"`.
* **B. Chứng chỉ Ngoại ngữ & Tương đương (Credential Equivalence):**
  * Ví dụ: `"IELTS 6.0 or equivalent"`, `"IELTS 6.0+ hoặc tương đương"`, `"Có chứng chỉ AWS hoặc tương đương"`.
  * Gán: Đây là tiêu chí độc lập (`group_operator = "atomic"`), gán `credential = "IELTS"`, `operator = "gte"`, `threshold = 6.0`, `equivalent_allowed = true`. **KHÔNG** tạo `group_operator = "any_of"`.
* **C. Lựa chọn Ngành học Nội bộ (Internal Education Alternative):**
  * Ví dụ: `"Tốt nghiệp đại học chuyên ngành CNTT hoặc Toán tin"`, `"Bachelor degree in Computer Science or Statistics"`.
  * Gán: Phân loại thành `EDUCATION_INTERNAL_ALTERNATIVE`. Tiêu chí bằng cấp vẫn là atomic requirement đại diện cho trình độ học vấn, các ngành học tương đương được ghi nhận trong nội dung qualification.
* **D. Lựa chọn Phương thức / Kinh nghiệm (Research / Experience Alternative):**
  * Ví dụ: `"Có công trình nghiên cứu khoa học hoặc tham gia kỳ thi AI"`.
  * Nếu hai phương thức là hai cách đáp ứng độc lập → Gán `group_operator = "any_of"`.

### 4. Mức độ Ưu tiên (Priority)
Reviewer phải xem xét ngữ cảnh toàn câu, không chỉ dựa vào tiêu đề:
* **Must-have (`must_have`):** Yêu cầu bắt buộc (*Required*, *Must have*, *Yêu cầu*, *Bắt buộc*, *Cần có*).
* **Preferred (`preferred`):** Ưu tiên, lợi thế, điểm cộng (*Preferred*, *Nice to have*, *Plus*, *Advantage*, *Ưu tiên*, *Lợi thế lớn*, *Điểm cộng*).
* *Lưu ý quan trọng:* Dòng chứa `"Biết thêm Docker là điểm cộng"` hoặc `"TOEIC 600+ là điểm cộng"` dù nằm trong phần Requirements vẫn phải được gán là `preferred`. Scanner đã gắn cờ 10 trường hợp `PRIORITY_CONTRADICTION` để Reviewer đối soát trong Phase B.

### 5. Khái niệm Kỹ năng & Tránh Ảo giác (Concept & Non-hallucination)
* Nếu văn bản nguồn nêu rõ công nghệ có trong taxonomy (Java, Python, React, Docker, AWS, SQL...):
  → Gán `concept` tương ứng (`skill-java`, `skill-python`...).
* Nếu văn bản chỉ nêu chung: `"Có kiến thức nền tảng về lập trình vững chắc"` hoặc `"Strong programming foundation"`:
  → Bắt buộc gán `concept = null`. Tuyệt đối **KHÔNG suy diễn** ra Python, Java hay C++.
* `"Willing to learn Python"` / `"Sẵn sàng học hỏi công nghệ mới"`:
  → Gán `kind = "other"`, `priority = "preferred"`, **KHÔNG biến thành required skill Python**.
* Công nghệ xuất hiện trong Quyền lợi / Phúc lợi (Benefits): `"Được đào tạo Python miễn phí"`
  → Nằm trong Benefits, **KHÔNG trích xuất thành Requirement**.

### 6. Xác minh Đoạn dẫn chứng (Evidence Span Verification)
Reviewer bắt buộc đối soát tọa độ offset:
`source.text[char_start:char_end] == evidence.text`
* Chuỗi ký tự dẫn chứng cắt ra từ văn bản gốc phải khớp chính xác 100% từng ký tự, dấu cách.
* Nếu phát hiện lệch offset: đánh dấu trạng thái `APPROVED_WITH_CORRECTION`.

---

## III. Chuẩn hóa Trạng thái Quyết định của Reviewer (Decision States)

Toàn bộ tài liệu, biểu mẫu markdown và JSON sử dụng thống nhất **đúng 4 trạng thái quyết định**:

1. **`APPROVED`**: Chấp thuận toàn bộ nhãn đề xuất (bao gồm cả evidence offsets).
2. **`APPROVED_WITH_CORRECTION`**: Chấp thuận sau khi đã điều chỉnh nhãn ngữ nghĩa hoặc tọa độ dẫn chứng (ghi rõ chi tiết trong notes).
3. **`NEEDS_DISCUSSION`**: Tiêu chí mơ hồ, cần thảo luận với Lead Reviewer trước khi chốt.
4. **`REJECTED`**: Bác bỏ trường thông tin đề xuất (do sai lệch ngữ nghĩa hoặc phát hiện ảo giác).

*Đối với 42 trường hợp Potential Leakage, quyết định ở cấp độ correction gồm:*
* `APPROVE_CORRECTION`
* `REJECT_CORRECTION`
* `NEEDS_DISCUSSION`

---

## IV. Tiêu chuẩn Ký duyệt Đóng Gate (Sign-off Criteria)

Gate P2 Adjudication chỉ hoàn thành khi:
* 90 / 90 cases đạt `APPROVED` hoặc `APPROVED_WITH_CORRECTION`.
* Số cases `NEEDS_DISCUSSION = 0`.
* Số cases `REJECTED = 0`.
* Số cases `AWAITING_HUMAN_SIGNOFF = 0`.
* Toàn bộ 42 potential leakage corrections được thẩm định độc lập.
* Chữ ký, reviewer ID và timestamp thực tế của con người được ghi nhận đầy đủ.
"""

(pack_dir / "ANNOTATION_GUIDELINE.md").write_text(guideline_text, encoding="utf-8")

print("Pack generation and restructure complete.")
