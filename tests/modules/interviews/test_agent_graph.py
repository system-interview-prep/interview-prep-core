from collections import Counter
from importlib import import_module
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import AsyncConnectionPool

from src.modules.interviews.agent import checkpoints
from src.modules.interviews.agent.graph import COMMANDS, build_interview_graph
from src.modules.interviews.application.agent_runtime import run_interview_command


@pytest.mark.asyncio
async def test_all_commands_route_to_one_graph_node() -> None:
    calls = Counter()

    def operation_for(name):
        async def operation(payload):
            calls[name] += 1
            return {"command": name, "payload": payload}
        return operation

    operations = {name: operation_for(name) for name in COMMANDS}
    graph = build_interview_graph(operations)
    for name in COMMANDS:
        state = await graph.ainvoke(
            {"command": name, "session_id": "s", "event_id": name, "payload": {"x": 1}},
        )
        assert state["result"] == {"command": name, "payload": {"x": 1}}
        assert state["completed"] is True
    assert calls == Counter(COMMANDS)


@pytest.mark.asyncio
async def test_retry_with_same_event_id_reuses_checkpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        checkpoints, "_checkpointer",
        InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None)),
    )
    calls = Counter()

    async def operation(payload):
        calls["respond"] += 1
        return {"reply": payload["content"]}

    operations = {name: operation for name in COMMANDS}
    first = await run_interview_command(
        session_id="s", command="respond", operations=operations,
        event_id="message-1", payload={"content": "hello"},
    )
    second = await run_interview_command(
        session_id="s", command="respond", operations=operations,
        event_id="message-1", payload={"content": "hello"},
    )
    assert first == second == {"reply": "hello"}
    assert calls["respond"] == 1
    with pytest.raises(ValueError, match="EVENT_ID_PAYLOAD_MISMATCH"):
        await run_interview_command(
            session_id="s", command="respond", operations=operations,
            event_id="message-1", payload={"content": "different"},
        )


@pytest.mark.asyncio
async def test_respond_requires_stable_id() -> None:
    with pytest.raises(ValueError, match="client_message_id"):
        await run_interview_command(
            session_id="s", command="respond", operations={}, payload={"content": "hello"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["prepare", "freeze", "open", "finish", "score"])
async def test_non_turn_commands_recheck_business_state(monkeypatch, command) -> None:
    monkeypatch.setattr(checkpoints, "_checkpointer", InMemorySaver())
    calls = Counter()

    async def operation(_):
        calls[command] += 1
        return {"version": calls[command]}

    operations = {name: operation for name in COMMANDS}
    payload = {"reason": "USER_ENDED"} if command == "finish" else None
    first = await run_interview_command(
        session_id="s", command=command, operations=operations, payload=payload,
    )
    second = await run_interview_command(
        session_id="s", command=command, operations=operations, payload=payload,
    )
    assert first == {"version": 1}
    assert second == {"version": 2}


@pytest.mark.asyncio
async def test_video_endpoint_submits_only_final_transcript(monkeypatch) -> None:
    router = import_module("src.modules.interviews.api.router")
    owner = AsyncMock(return_value={"id": "s"})
    runner = AsyncMock(return_value={"message": "next question"})
    monkeypatch.setattr(router, "_owned_session", owner)
    monkeypatch.setattr(router, "database_operations", lambda **_: {})
    monkeypatch.setattr(router, "run_interview_command", runner)

    result = await router.send_video_transcript(
        "s",
        router.SendVideoTranscript(clientMessageId="v-1", finalTranscript="My answer"),
        user={"sub": "u"}, db=object(),
    )

    assert result == {"message": "next question"}
    assert runner.await_args.kwargs["command"] == "respond"
    assert runner.await_args.kwargs["payload"]["modality"] == "VIDEO"
    assert runner.await_args.kwargs["payload"]["content"] == "My answer"
    assert "video_observations" not in runner.await_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_checkpoint_lifespan_initializes_strict_postgres_saver(monkeypatch) -> None:
    setup = AsyncMock()
    saver = type("FakeSaver", (), {"setup": setup})()
    received = {}

    class FakePool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    def make_pool(url, **kwargs):
        received.update(url=url, pool_kwargs=kwargs)
        return FakePool()

    def make_saver(pool, *, serde):
        received.update(pool=pool, serde=serde)
        return saver

    monkeypatch.setattr(
        AsyncConnectionPool, "__new__", lambda cls, *args, **kwargs: make_pool(*args, **kwargs)
    )
    monkeypatch.setattr(
        AsyncPostgresSaver, "__new__", lambda cls, *args, **kwargs: make_saver(*args, **kwargs)
    )
    async with checkpoints.interview_checkpoint_lifespan():
        assert checkpoints.get_interview_checkpointer() is saver
    setup.assert_awaited_once()
    assert received["url"].startswith("postgresql://")
    assert received["pool_kwargs"]["kwargs"]["autocommit"] is True
    assert received["serde"]._allowed_msgpack_modules is None
    assert checkpoints.get_interview_checkpointer() is None
