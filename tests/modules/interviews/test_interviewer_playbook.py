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


def test_followups_link_turns_of_the_same_interview_in_order() -> None:
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 10, 26, tzinfo=UTC)

    def row(sid, turn, action, minutes):
        return {
            "session_id": sid,
            "turn_index": turn,
            "created_at": t0 + timedelta(minutes=minutes),
            "payload": {"action": action, "sufficiency": "INSUFFICIENT" if action == "EVALUATE" else None},
        }

    # newest first, as the query returns them, two interviews interleaved
    cases = ip.link_followups(
        [
            ip.case_from_event(r)
            for r in [
                row("bbbbbbbb-2", 1, "NEXT_QUESTION", 9),
                row("aaaaaaaa-1", 2, "PROBE", 5),
                row("bbbbbbbb-2", 0, "EVALUATE", 4),
                row("aaaaaaaa-1", 1, "EVALUATE", 1),
            ]
        ]
    )
    by = {c["case"]: c for c in cases}
    assert by["aaaaaaaa#1"]["next"] == {"action": "PROBE", "intent": None}
    assert by["bbbbbbbb#0"]["next"]["action"] == "NEXT_QUESTION"
    assert by["aaaaaaaa#2"]["next"] is None and by["bbbbbbbb#1"]["next"] is None
    old = ip.case_from_event(row("cccccccc-3", 0, "PROBE", -60 * 24 * 20))
    assert ip.in_window(cases + [old], 7) == cases


def test_lessons_about_masking_markers_are_dropped() -> None:
    raw = """{"ops": [
      {"op": "ADD", "step": "all", "text": "Không để lộ placeholder [tên] khi chào.", "cases": ["c1"]},
      {"op": "ADD", "step": "probe", "text": "Hỏi ví dụ.", "why": "nói [sđt]", "cases": ["c1"]},
      {"op": "ADD", "step": "probe", "text": "Hỏi số liệu đo được.", "cases": ["c1"]}
    ]}"""
    assert [o["text"] for o in ip.parse_ops(raw, set())] == ["Hỏi số liệu đo được."]


class _FakeDb:
    def __init__(self, known=()):
        self.known = list(known)
        self.sql: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.sql.append((str(statement), params or {}))

        class _Result:
            def __init__(self, rows):
                self.rows = rows

            def mappings(self):
                return self

            def all(self):
                return self.rows

        return _Result(self.known if "SELECT" in str(statement) else [])


async def test_lessons_without_evidence_are_not_stored() -> None:
    db = _FakeDb()
    ops = [
        {"op": "ADD", "step": "probe", "text": "Hỏi số liệu đo được.", "why": "w", "cases": ["x#1"]},
        {
            "op": "ADD",
            "step": "probe",
            "text": "Hỏi về rủi ro khi triển khai.",
            "why": "w",
            "cases": ["nope#9"],
        },
        {
            "op": "ADD",
            "step": "clarify",
            "text": "Đổi ví dụ khi giải thích lại câu hỏi.",
            "why": "w",
            "cases": [],
        },
    ]
    stored = await ip.store_ops(
        db, ops, run_id="r", cases=[{"case": "x#1", "sessionId": "x", "turnIndex": 1}]
    )
    assert stored == 1
    inserts = [p for s, p in db.sql if s.startswith("INSERT")]
    assert len(inserts) == 1 and inserts[0]["text"] == "Hỏi số liệu đo được."


async def test_counters_are_set_not_added() -> None:
    from collections import Counter

    db = _FakeDb()
    counts = {"helpful": Counter({"a": 2}), "harmful": Counter({"b": 1})}
    await ip._apply_counts(db, counts)
    await ip._apply_counts(db, counts)
    statements = [s for s, _ in db.sql]
    assert statements[0].startswith("UPDATE interviewer_playbook SET helpful = 0, harmful = 0")
    assert all("+ :n" not in s for s in statements)
    assert (
        "UPDATE interviewer_playbook SET helpful = :n, updated_at = now() WHERE id = :id",
        {"n": 2, "id": "a"},
    ) in db.sql


class _OneRowDb:
    def __init__(self, row):
        self.row = row

    async def execute(self, statement, params=None):
        row = self.row

        class _Result:
            def mappings(self):
                return self

            def one_or_none(self):
                return row

        return _Result()


async def test_an_admin_cannot_approve_a_lesson_they_wrote() -> None:
    row = {"id": "e1", "status": "PROPOSED", "created_by": "admin-1", "source": {}}
    with pytest.raises(ip.PlaybookError) as raised:
        await ip.decide(
            _OneRowDb(row), "e1", decision="APPROVE", actor_id="admin-1", note=None, text_value=None
        )
    assert raised.value.status_code == 403 and raised.value.code == "own_entry"


def test_entry_view_shows_status_authors_and_own_lessons() -> None:
    row = {
        "id": "e1",
        "step": "probe",
        "text": "Hỏi số liệu đo được.",
        "status": "REJECTED",
        "origin": "manual",
        "created_by": "admin-1",
        "decided_by": "admin-2",
        "note": "trùng bài khác",
    }
    view = ip.entry_view(row, actor_id="admin-1", names={"admin-1": "Lan", "admin-2": "Minh"})
    assert view["status"] == "REJECTED" and view["note"] == "trùng bài khác"
    assert view["createdBy"] == "Lan" and view["decidedBy"] == "Minh" and view["mine"] is True
    ai_lesson = ip.entry_view({**row, "origin": "reflection", "created_by": None}, actor_id="admin-1")
    assert ai_lesson["createdBy"] is None and ai_lesson["mine"] is False
