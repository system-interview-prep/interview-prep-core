"""The interviewer's playbook: approved lessons in prompts, proposals from past turns."""

import pytest

from src.modules.interviews.application import interviewer_playbook as ip
from src.modules.interviews.core import playbook


@pytest.fixture(autouse=True)
def _empty_playbook():
    playbook.load([])
    yield
    playbook.load([])


def test_empty_playbook_leaves_prompts_unchanged() -> None:
    assert playbook.block("probe") == "" and playbook.version() == ""


def test_block_lists_lessons_of_the_step_and_skill_below_the_rules() -> None:
    playbook.load(
        [
            {"id": "a", "step": "probe", "text": "Hỏi một số liệu cụ thể trước."},
            {"id": "b", "step": "all", "text": "Giữ câu ngắn."},
            {"id": "c", "step": "clarify", "text": "Đổi ví dụ khi giải thích lại."},
            {"id": "d", "step": "probe", "skill": "Redis", "text": "Hỏi về khi Redis sập."},
        ]
    )
    text = playbook.block("probe", True, "redis")
    assert "người duyệt" in text and "mọi quy tắc ở trên luôn được ưu tiên" in text
    assert "- Hỏi một số liệu cụ thể trước." in text and "- Giữ câu ngắn." in text
    assert "Hỏi về khi Redis sập." in text and "Đổi ví dụ" not in text
    assert "Hỏi về khi Redis sập." not in playbook.block("probe", True, "Kafka")
    assert len(playbook.version()) == 8 and playbook.ids() == ["a", "b", "c", "d"]


def test_cases_carry_what_the_interviewer_did_not_what_the_candidate_said() -> None:
    case = ip.case_from_event(
        {
            "session_id": "12345678-aaaa",
            "turn_index": 3,
            "payload": {
                "candidate": "em tên Nguyễn Văn A, email a@b.com",
                "interviewer": "Bạn có thể nói rõ hơn? Liên hệ a@b.com",
                "action": "PROBE",
                "flags": ["probe_off_target"],
                "langfuse_trace_id": "t1",
                "playbook_ids": ["a"],
            },
        }
    )
    assert case["case"] == "12345678#3" and "candidate" not in case
    assert "a@b.com" not in case["interviewer"]
    assert ip.verdict_of(case) == "bad"


def _case(cid, flags=(), verdicts=None, ids=()):
    return {"case": cid, "flags": list(flags), "verdicts": verdicts or {}, "playbookIds": list(ids)}


def test_bad_turns_first_with_some_good_contrast_and_counters() -> None:
    cases = [_case(f"b{i}", ["x"]) for i in range(40)] + [
        _case(f"g{i}", verdicts={"human.probe_ok": True}) for i in range(10)
    ]
    picked = ip.pick_cases(cases, limit=20)
    assert len(picked) == 20 and sum(c["case"].startswith("g") for c in picked) == 5
    counts = ip.count_outcomes(
        [
            _case("1", ["x"], ids=["a"]),
            _case("2", verdicts={"judge.probe_ok": True}, ids=["a", "b"]),
            _case("3", ids=["b"]),
        ]
    )
    assert counts["harmful"] == {"a": 1} and counts["helpful"] == {"a": 1, "b": 1}
    assert ip.verdict_of(_case("4", verdicts={"human.decision_ok": False})) == "bad"
    assert ip.verdict_of(_case("5")) is None


def test_reflector_output_is_validated() -> None:
    raw = """{"ops": [
      {"op": "ADD", "step": "probe", "text": "Hỏi số liệu cụ thể.", "why": "w", "cases": ["c1"]},
      {"op": "EDIT", "id": "unknown", "step": "probe", "text": "x"},
      {"op": "RETIRE", "id": "a", "why": "gây cờ"},
      {"op": "ADD", "step": "weird", "text": "Bước lạ thành all."},
      {"op": "DROP", "id": "a"}
    ]}"""
    ops = ip.parse_ops(raw, {"a"})
    assert [o["op"] for o in ops] == ["ADD", "RETIRE", "ADD"]
    assert ops[2]["step"] == "all"
    assert ip.parse_ops("không phải json", set()) == []
