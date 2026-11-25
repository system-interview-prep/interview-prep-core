"""T1.5.1–3, T2.5.1–3, T2.6.1–4 (as M-SWAP-1..6, langmem extractor), T3.5.1 — candidate memory (flag off by default)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore

from src.modules.interviews.agents.interviewer import graph as turn_graph
from src.modules.interviews.memory import candidate_memory as cm
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, TurnAction
from src.modules.interviews.core.conversation.prompt.candidate_memory import MAX_BLOCK_CHARS, MAX_ITEMS, block
from src.modules.interviews.core.conversation.turn_engine import InterviewCoreEngine
from src.modules.interviews.core.guardrails.output import memory_rejection_reason
from tests.modules.interviews.agent.conftest import FakeToolCaller, doc_id, scripted_chat_model, tool_call
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


# ----------------------------------------------------------------- T2.6.x extraction (langmem; M-SWAP-1..6)

EVALUATION = {
    "turn_results": [
        {"competency": "skill-redis", "stage": "DEEP_DIVE", "outcome": "ANSWERED",
         "question_text": "Bạn dùng Redis cache thế nào?", "strengths": ["Nêu TTL"],
         "weaknesses": ["Chưa nói invalidation"], "evidence_quotes": ["em đặt TTL năm phút cho từng key"], "score": 6.0},
    ]
}
NS = cm.namespace(USER)


def _insert(text: str, kind: str = "gap", skill: str = "skill-redis", call_id: str = "c1") -> dict:
    return {"id": call_id, "name": "CandidateMemory", "args": {"kind": kind, "skill_key": skill, "text": text, "confidence": 0.8}}


def _record_rows(monkeypatch) -> list:
    """Capture ``ai_call_logs`` rows instead of buffering them."""
    import src.core.pipeline_events as events

    rows: list = []
    monkeypatch.setattr(events, "record_ai_call", lambda **row: rows.append(row))
    return rows


@pytest.fixture
def extraction(monkeypatch):
    """``extract_after_session`` with the db replaced: grader conclusions + the user of the session."""

    async def fake_user(db, session_id):
        return USER

    monkeypatch.setattr(cm, "_user_id_of_session", fake_user)

    async def run(store, model, session_id="sess-1"):
        # The report is handed in by whoever ran ``score``; memory does not
        # reach into agents.evaluator for it (V2 spec §10).
        return await cm.extract_after_session(
            None, session_id, evaluation=EVALUATION, store=store, model=model
        )

    return run


@pytest.mark.asyncio
async def test_t2_6_1_m_swap_1_an_insert_lands_in_the_store_and_the_prompt_carries_no_quotes(extraction) -> None:
    store = InMemoryStore()
    model = scripted_chat_model([[_insert("Còn hở phần invalidation")]])
    counts = await extraction(store, model)
    assert counts == {"added": 1, "updated": 0, "retired": 0, "rejected": 0, "truncated": 0}
    prompt = "\n".join(model.prompts)
    assert "em đặt TTL năm phút" not in prompt and "6.0" not in prompt
    assert "Chưa nói invalidation" in prompt and "CandidateMemory" in prompt
    items = await cm.load(USER, store=store)
    assert [i["text"] for i in items] == ["Còn hở phần invalidation"]
    assert items[0]["source"] == "extracted" and items[0]["evidence"]["session_id"] == "sess-1" and items[0]["times_seen"] == 1
    raw = (await store.asearch(NS, limit=5))[0].value
    assert raw["kind"] == cm.SCHEMA_KIND and raw["content"]["text"] == "Còn hở phần invalidation"


@pytest.mark.asyncio
async def test_m_swap_1b_patch_and_remove_name_existing_rows(extraction) -> None:
    store = InMemoryStore()
    seeded = cm._seed_items(GAPS)
    for item in seeded:
        await store.aput(NS, item["key"], cm.wrap({k: v for k, v in item.items() if k != "key"}))
    redis_key = next(i["key"] for i in seeded if i["skill_key"] == "skill-redis")
    sql_key = next(i["key"] for i in seeded if i["skill_key"] == "skill-sql")
    model = scripted_chat_model([[
        {"id": "p1", "name": "PatchDoc", "args": {"json_doc_id": doc_id(NS, redis_key), "planned_edits": "nắm rồi",
                                                  "patches": [{"op": "replace", "path": "/kind", "value": "strength"},
                                                              {"op": "replace", "path": "/text", "value": "Đã nắm invalidation"}]}},
        {"id": "r1", "name": "RemoveDoc", "args": {"json_doc_id": doc_id(NS, sql_key)}},
    ]])
    counts = await extraction(store, model, session_id="sess-2")
    assert counts == {"added": 0, "updated": 1, "retired": 1, "rejected": 0, "truncated": 0}
    items = {i["key"]: i for i in await cm.load(USER, store=store)}
    assert sql_key not in items
    patched = items[redis_key]
    assert patched["kind"] == "strength" and patched["text"] == "Đã nắm invalidation"
    assert patched["times_seen"] == 3 and patched["evidence"]["session_id"] == "sess-2"  # seed had 2, +1
    assert patched["source"] == "seed_gap_notebook" and patched["evidence"]["question"]  # bookkeeping kept through the patch
    assert "Bạn dùng Redis cache thế nào trong dự án" in model.prompts[0]  # existing rows are shown with their ids


@pytest.mark.asyncio
async def test_m_swap_2_every_model_call_is_an_ai_call_logs_row_with_tokens(extraction, monkeypatch) -> None:
    rows = _record_rows(monkeypatch)
    store = InMemoryStore()
    model = scripted_chat_model([[_insert("Còn hở phần invalidation")]], usage={"input_tokens": 321, "output_tokens": 45, "total_tokens": 366})
    await extraction(store, model)
    assert len(rows) == 1
    row = rows[0]
    assert row["purpose"] == cm.PURPOSE_EXTRACT and row["provider"] == "openai" and row["model"] == "scripted-chat"
    assert row["status"] == "ok" and row["input_tokens"] == 321 and row["output_tokens"] == 45
    assert "CandidateMemory" in row["output_text"] and "Chưa nói invalidation" in row["input_text"]


@pytest.mark.asyncio
async def test_m_swap_3_the_whole_extraction_is_one_langfuse_generation(extraction, monkeypatch) -> None:
    seen = {}

    class FakeGeneration:
        def __init__(self, purpose, **kwargs):
            seen["start"] = {"purpose": purpose, **kwargs}

        def end(self, **kwargs):
            seen["end"] = kwargs

    monkeypatch.setattr(cm.langfuse, "Generation", FakeGeneration)
    store = InMemoryStore()
    await extraction(store, scripted_chat_model([[_insert("Còn hở phần invalidation")]]))
    assert seen["start"]["purpose"] == cm.PURPOSE_EXTRACT and seen["start"]["model"] == "scripted-chat"
    assert "em đặt TTL" not in seen["start"]["input_text"]
    assert seen["end"]["usage"].input_tokens == 100 and seen["end"]["usage"].output_tokens == 20
    assert json.loads(seen["end"]["output"])["added"] == 1


@pytest.mark.asyncio
async def test_m_swap_4_a_model_error_is_logged_and_returns_nothing(extraction, monkeypatch) -> None:
    rows = _record_rows(monkeypatch)
    events = []
    monkeypatch.setattr(cm, "trace_event", lambda area, name, **kw: events.append(name))
    store = InMemoryStore()
    model = scripted_chat_model([], raise_with=RuntimeError("provider down"))
    assert await extraction(store, model) == {}
    assert await cm.load(USER, store=store) == []
    assert rows and rows[-1]["status"] == "error" and rows[-1]["error_type"] == "RuntimeError"
    assert "candidate_memory_extract_error" in events


@pytest.mark.asyncio
async def test_t2_6_2_m_swap_5_more_than_five_changes_are_rolled_back(extraction, monkeypatch) -> None:
    events = []
    monkeypatch.setattr(cm, "trace_event", lambda area, name, **kw: events.append((name, kw)))
    store = InMemoryStore()
    model = scripted_chat_model([[_insert(f"gap {i}", call_id=f"c{i}") for i in range(8)]])
    counts = await extraction(store, model)
    assert counts["truncated"] == 3 and counts["added"] == 5 and counts["rejected"] == 0
    assert len(await cm.load(USER, store=store, limit=50)) == 5
    assert ("candidate_memory_ops_capped", {"session_id": "sess-1", "truncated": 3}) in events


@pytest.mark.asyncio
async def test_m_swap_5b_a_row_without_text_or_skill_is_rejected_and_removed(extraction) -> None:
    store = InMemoryStore()
    blank = _insert("   ", call_id="c2")
    no_skill = _insert("Còn hở phần index", skill="  ", call_id="c3")
    counts = await extraction(store, scripted_chat_model([[_insert("Còn hở phần invalidation"), blank, no_skill]]))
    assert counts == {"added": 1, "updated": 0, "retired": 0, "rejected": 2, "truncated": 0}
    assert [i["text"] for i in await cm.load(USER, store=store)] == ["Còn hở phần invalidation"]


@pytest.mark.asyncio
async def test_m_swap_6_no_changes_means_no_writes(extraction) -> None:
    store = InMemoryStore()
    await store.aput(NS, "k1", cm.wrap({"kind": "gap", "skill_key": "s", "text": "t", "last_seen": "2026-01-01"}))
    before = (await store.asearch(NS, limit=5))[0].value
    counts = await extraction(store, scripted_chat_model([[]]))
    assert counts == {"added": 0, "updated": 0, "retired": 0, "rejected": 0, "truncated": 0}
    assert (await store.asearch(NS, limit=5))[0].value == before


@pytest.mark.asyncio
async def test_t2_6_4_deleting_a_user_empties_the_namespace() -> None:
    store = InMemoryStore()
    await store.aput(NS, "k1", cm.wrap({"kind": "gap", "skill_key": "s", "text": "t"}))
    assert await cm.delete_user(USER, store=store) == 1
    assert await cm.load(USER, store=store) == []


@pytest.mark.asyncio
async def test_extraction_failures_never_raise(monkeypatch) -> None:
    async def boom(db, session_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(cm, "_user_id_of_session", boom)
    assert await cm.extract_after_session(
        None, "s", evaluation=EVALUATION, store=InMemoryStore()
    ) == {}


@pytest.mark.asyncio
async def test_no_report_means_no_extraction() -> None:
    """``score`` hands in the stored report; without one there is nothing to learn."""
    assert await cm.extract_after_session(None, "s", evaluation=None, store=InMemoryStore()) == {}


@pytest.mark.asyncio
async def test_the_default_chat_model_is_blocked_in_tests(extraction) -> None:
    """Without ``model=`` the extractor would build ChatOpenAI: tests/conftest.py blocks it, so {} and no call."""
    store = InMemoryStore()
    assert await cm.extract_after_session(None, "s", evaluation=EVALUATION, store=store) == {}


def test_a_flat_value_from_before_the_envelope_still_reads() -> None:
    assert cm.unwrap({"kind": "gap", "skill_key": "s", "text": "t"}) == {"kind": "gap", "skill_key": "s", "text": "t"}
    assert cm.unwrap(cm.wrap({"kind": "gap", "skill_key": "s", "text": "t"}))["text"] == "t"


def test_t2_6_3_the_playbook_reflector_cannot_see_candidate_memory() -> None:
    root = Path(__file__).resolve().parents[4] / "src" / "modules" / "interviews"
    playbook = root / "admin" / "interviewer_playbook.py"
    source = playbook.read_text(encoding="utf-8-sig")  # the file starts with a BOM
    tree = ast.parse(source)
    names = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any("candidate_memory" in name for name in names)
    assert "candidate_memory" not in source


# ----------------------------------------------------------------- T3.5.1 a returning candidate

@pytest.mark.asyncio
async def test_t3_5_1_memory_reaches_the_prompts_and_the_decider_but_is_never_recited(session_state_factory, monkeypatch) -> None:
    from src.modules.interviews.agents.interviewer import thinker as decide_module

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
