"""T1.5.1–3, T2.5.1–3, T2.6.1–4, T3.5.1 — candidate memory (flag off by default)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore

from src.modules.interviews.agent import turn_graph
from src.modules.interviews.application.runtime import candidate_memory as cm
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, TurnAction
from src.modules.interviews.core.conversation.prompt.candidate_memory import MAX_BLOCK_CHARS, MAX_ITEMS, block
from src.modules.interviews.core.conversation.turn_engine import InterviewCoreEngine
from src.modules.interviews.core.dialogue.policy import memory_rejection_reason
from tests.modules.interviews.agent.conftest import FakeToolCaller, tool_call
from tests.modules.interviews.agent.test_turn_graph import ScriptedLLM

USER = "user-42"
GAPS = {
    "open": [
        {"skillKey": "skill-redis", "skill": "Redis", "point": "Xử lý cache invalidation", "timesAsked": 2,
         "lastSeen": {"sessionId": "s1", "date": "2026-11-01T10:00:00+00:00", "question": "Bạn dùng Redis cache thế nào trong dự án của bạn?"}},
        {"skillKey": "skill-sql", "skill": "SQL", "point": "Index cho truy vấn chậm", "timesAsked": 1,
         "lastSeen": {"sessionId": "s1", "date": "2026-11-01T10:05:00+00:00", "question": "Bạn tối ưu query chậm ra sao?"}},
    ],
    "fixed": [
        {"skillKey": "skill-docker", "skill": "Docker", "point": "Multi-stage build", "timesAsked": 2, "timesMissed": 1,
         "lastSeen": {"sessionId": "s2", "date": "2026-11-10T10:00:00+00:00", "question": "Dockerfile của bạn có gì?"}},
    ],
}


def _items(n: int) -> list[dict]:
    return [{"key": f"k{i}", "kind": "gap", "skill_key": f"skill-{i}", "text": f"Còn hở điểm số {i} " * 3} for i in range(n)]


# ----------------------------------------------------------------- T1.5.x prompt block & guard

def test_t1_5_1_block_is_empty_three_lines_or_capped() -> None:
    assert block([], True) == ""
    three = block(_items(3), True)
    assert three.count("\n- ") == 3
    # the head forbids recital; the item lines themselves never recite
    assert all("lần trước" not in line for line in three.split("\n") if line.startswith("- "))
    capped = block(_items(20), True)
    assert capped.count("\n- ") <= MAX_ITEMS and len(capped) <= MAX_BLOCK_CHARS + 200


def test_t1_5_1b_block_prefers_the_competency_being_asked() -> None:
    items = [{"kind": "gap", "skill_key": "skill-sql", "text": "Còn hở index"},
             {"kind": "strength", "skill_key": "skill-redis", "text": "Đã nắm TTL"}]
    text = block(items, True, competency="skill-redis")
    assert text.index("Đã nắm TTL") < text.index("Còn hở index")


def test_t1_5_2_the_guard_rejects_recital_and_passes_topic_naming() -> None:
    items = [{"kind": "gap", "skill_key": "skill-redis", "text": "Còn hở invalidation",
              "evidence": {"question": "Bạn dùng Redis cache thế nào trong dự án của bạn?"}}]
    assert memory_rejection_reason("Lần trước bạn nói dùng Redis với TTL 30s, giờ thì sao?", items) == "recalls_quote"
    assert memory_rejection_reason("Last time you said you used Redis; what about invalidation?", items) == "recalls_quote"
    assert memory_rejection_reason("Bạn dùng Redis cache thế nào trong dự án của bạn?", items) == "repeats_memory_question"
    assert memory_rejection_reason("Khi dữ liệu đổi thì cache được cập nhật ra sao?", items) is None
    assert memory_rejection_reason("", items) is None


def test_t1_5_3_the_decider_search_result_carries_no_evidence() -> None:
    import asyncio

    store = InMemoryStore()
    asyncio.run(store.aput(cm.namespace(USER), "k1", {
        "kind": "gap", "skill_key": "skill-redis", "text": "Còn hở invalidation",
        "evidence": {"question": "Bạn dùng Redis cache thế nào trong dự án của bạn?", "session_id": "s1"},
        "last_seen": "2026-11-01", "confidence": 0.7,
    }))
    hits = asyncio.run(cm.search_for(USER, store=store)("skill-redis"))
    assert hits == [{"kind": "gap", "skill_key": "skill-redis", "text": "Còn hở invalidation"}]


# ----------------------------------------------------------------- T2.5.x seed & load

@pytest.mark.asyncio
async def test_t2_5_1_seed_twice_writes_once(monkeypatch) -> None:
    store = InMemoryStore()

    async def fake_progress(db, user_id, **_):
        return {"gaps": GAPS}

    monkeypatch.setattr(cm, "load_progress", fake_progress)
    assert await cm.seed_from_gap_notebook(None, USER, store=store) == 3
    assert await cm.seed_from_gap_notebook(None, USER, store=store) == 0
    items = await cm.load(USER, store=store, limit=50)
    assert len(items) == 3 and {i["kind"] for i in items} == {"gap", "strength"}
    assert all(i["source"] == "seed_gap_notebook" and i["evidence"]["session_id"] for i in items)
    redis = [i for i in items if i["skill_key"] == "skill-redis"][0]
    assert redis["text"].startswith("Còn hở: Xử lý cache invalidation")


@pytest.mark.asyncio
async def test_t2_5_2_a_namespace_with_items_is_not_reseeded(monkeypatch) -> None:
    store = InMemoryStore()
    await store.aput(cm.namespace(USER), "existing", {"kind": "gap", "skill_key": "x", "text": "x", "last_seen": "2026"})
    called = []

    async def fake_progress(db, user_id, **_):
        called.append(user_id)
        return {"gaps": GAPS}

    monkeypatch.setattr(cm, "load_progress", fake_progress)
    assert await cm.seed_from_gap_notebook(None, USER, store=store) == 0 and called == []


@pytest.mark.asyncio
async def test_load_filters_by_skill_and_falls_back_to_all(monkeypatch) -> None:
    store = InMemoryStore()
    for i, skill in enumerate(["skill-redis", "skill-redis", "skill-sql"]):
        await store.aput(cm.namespace(USER), f"k{i}", {"kind": "gap", "skill_key": skill, "text": f"t{i}", "last_seen": f"2026-11-0{i+1}"})
    cm.clear_cache()
    assert {i["skill_key"] for i in await cm.load(USER, skill_key="skill-redis", store=store)} == {"skill-redis"}
    assert len(await cm.load(USER, skill_key="skill-go", store=store)) == 3  # no match → everything, newest first
    assert (await cm.load(USER, store=store))[0]["key"] == "k2"
    assert await cm.load("", store=store) == [] and await cm.load(USER, store=None) == []


# ----------------------------------------------------------------- T2.6.x extraction

EVALUATION = {
    "turn_results": [
        {"competency": "skill-redis", "stage": "DEEP_DIVE", "outcome": "ANSWERED",
         "question_text": "Bạn dùng Redis cache thế nào?", "strengths": ["Nêu TTL"],
         "weaknesses": ["Chưa nói invalidation"], "evidence_quotes": ["em đặt TTL năm phút cho từng key"], "score": 6.0},
    ]
}


@pytest.mark.asyncio
async def test_t2_6_1_the_extraction_prompt_never_carries_the_candidates_words(monkeypatch) -> None:
    store = InMemoryStore()
    seen = {}

    async def fake_generate(**kwargs):
        seen.update(kwargs)
        return json.dumps({"ops": [{"op": "ADD", "key": "", "kind": "gap", "skill_key": "skill-redis",
                                    "text": "Còn hở phần invalidation", "confidence": 0.8}]})

    async def fake_eval(db, session_id):
        return EVALUATION

    async def fake_user(db, session_id):
        return USER

    monkeypatch.setattr(cm, "get_session_evaluation", fake_eval)
    monkeypatch.setattr(cm, "_user_id_of_session", fake_user)
    counts = await cm.extract_after_session(None, "sess-1", store=store, generate=fake_generate)
    assert counts["added"] == 1
    assert "em đặt TTL năm phút" not in seen["input_text"] and "6.0" not in seen["input_text"]
    assert "Chưa nói invalidation" in seen["input_text"]
    assert seen["purpose"] == cm.PURPOSE_EXTRACT and seen["json_schema"]["name"] == "candidate_memory_ops"
    items = await cm.load(USER, store=store)
    assert items[0]["source"] == "extracted" and items[0]["evidence"]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_t2_6_2_more_than_five_ops_are_truncated_and_bad_ops_rejected() -> None:
    store = InMemoryStore()
    ops = [{"op": "ADD", "key": "", "kind": "gap", "skill_key": "s", "text": f"gap {i}", "confidence": 0.5} for i in range(7)]
    ops[1]["kind"] = "mood"  # not a kind → rejected
    counts = await cm.apply_ops(USER, ops, store=store, session_id="s")
    assert counts["truncated"] == 2 and counts["rejected"] == 1 and counts["added"] == 4


@pytest.mark.asyncio
async def test_update_and_retire_touch_only_existing_keys() -> None:
    store = InMemoryStore()
    await cm.apply_ops(USER, [{"op": "ADD", "key": "", "kind": "gap", "skill_key": "skill-redis", "text": "Còn hở invalidation", "confidence": 0.6}], store=store)
    key = (await cm.load(USER, store=store))[0]["key"]
    counts = await cm.apply_ops(USER, [
        {"op": "UPDATE", "key": key, "kind": "strength", "skill_key": "skill-redis", "text": "Đã nắm invalidation", "confidence": 0.9},
        {"op": "UPDATE", "key": "nope", "kind": "gap", "skill_key": "x", "text": "x", "confidence": 0.5},
        {"op": "RETIRE", "key": "nope"},
    ], store=store, session_id="s2")
    assert counts == {"added": 0, "updated": 1, "retired": 0, "rejected": 2, "truncated": 0}
    item = (await cm.load(USER, store=store))[0]
    assert item["kind"] == "strength" and item["times_seen"] == 2 and item["evidence"]["session_id"] == "s2"
    assert (await cm.apply_ops(USER, [{"op": "RETIRE", "key": key}], store=store))["retired"] == 1
    assert await cm.load(USER, store=store) == []


@pytest.mark.asyncio
async def test_t2_6_4_deleting_a_user_empties_the_namespace() -> None:
    store = InMemoryStore()
    await cm.apply_ops(USER, [{"op": "ADD", "key": "", "kind": "gap", "skill_key": "s", "text": "t", "confidence": 0.5}], store=store)
    assert await cm.delete_user(USER, store=store) == 1
    assert await cm.load(USER, store=store) == []


@pytest.mark.asyncio
async def test_extraction_failures_never_raise(monkeypatch) -> None:
    async def boom(db, session_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(cm, "get_session_evaluation", boom)
    assert await cm.extract_after_session(None, "s", store=InMemoryStore()) == {}


def test_t2_6_3_the_playbook_reflector_cannot_see_candidate_memory() -> None:
    root = Path(__file__).resolve().parents[4] / "src" / "modules" / "interviews"
    playbook = root / "application" / "quality_admin" / "interviewer_playbook.py"
    source = playbook.read_text(encoding="utf-8-sig")  # the file starts with a BOM
    tree = ast.parse(source)
    names = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any("candidate_memory" in name for name in names)
    assert "candidate_memory" not in source


# ----------------------------------------------------------------- T3.5.1 a returning candidate

@pytest.mark.asyncio
async def test_t3_5_1_memory_reaches_the_prompts_and_the_decider_but_is_never_recited(session_state_factory, monkeypatch) -> None:
    from src.modules.interviews.agent import decide as decide_module

    store = InMemoryStore()
    await store.aput(cm.namespace(USER), "k1", {
        "kind": "gap", "skill_key": "skill-python", "text": "Còn hở phần cache invalidation",
        "evidence": {"question": "Bạn dùng Redis cache thế nào trong dự án của bạn?", "session_id": "s1"},
        "last_seen": "2026-11-01", "confidence": 0.7,
    })
    cm.clear_cache()
    state = session_state_factory()
    state["candidate_memory"] = await cm.load(USER, store=store)

    class RecordingLLM(ScriptedLLM):
        def __init__(self):
            super().__init__({"intent": "ANSWER", "sufficiency_status": "INSUFFICIENT", "missing_aspect": "invalidation",
                              "acknowledgement": "Ừ, mình hình dung được rồi."},
                             line="Lần trước bạn nói dùng TTL, giờ invalidation thì sao?")
            self.prompts = []

        async def generate_json(self, system_prompt, user_content):
            self.prompts.append(system_prompt)
            return await super().generate_json(system_prompt, user_content)

        async def generate_text(self, system_prompt, user_content):
            self.prompts.append(system_prompt)
            # the first probe recites the memory; the replacement line does not
            self.line = "Khi dữ liệu đổi thì cache của bạn được cập nhật ra sao?" if "buổi trước" in user_content else self.line
            return await super().generate_text(system_prompt, user_content)

    caller = FakeToolCaller(
        [tool_call("search_candidate_memory", call_id="m1", skill_key="skill-python")],
        [tool_call("acknowledge_and_probe", kind="gap", missing_aspect="invalidation", rationale="gap seen before")],
    )
    monkeypatch.setattr(decide_module, "generate_tool_calls", caller)
    llm = RecordingLLM()
    out = await turn_graph.run_turn(
        InterviewCoreEngine(llm_client=llm),
        CandidateTurnInput(session_id="sess-mem", turn_index=3, text_content="Em dùng Redis làm cache với TTL năm phút và xoá key khi dữ liệu đổi."),
        state, mode="shadow", memory_search=cm.search_for(USER, store=store),
    )
    # the memory block reached the evaluation and probe prompts, as topics only
    assert any("Còn hở phần cache invalidation" in p for p in llm.prompts)
    assert not any("Bạn dùng Redis cache thế nào trong dự án" in p for p in llm.prompts)
    # the decider searched memory and saw topics only
    assert out.metadata["decision"]["agent"]["args"]["memory_hits"] == 1
    assert "search_candidate_memory" in out.metadata["decision"]["allowed_actions"]
    # the recited line was replaced and the rejection recorded
    assert out.action == TurnAction.PROBE
    assert "lần trước" not in out.message_text.lower()
    assert out.metadata["memory_recall_rejected"] == "recalls_quote"
