import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi import FastAPI
from livekit import rtc
from livekit.agents import stt

from src.core.security import current_user
from src.modules.admin.ai import stt_livekit as api
from src.modules.interviews.adapters.voice.stt import livekit_stt_test as worker


@pytest.fixture
def settings(monkeypatch):
    value = SimpleNamespace(
        jwt_secret="test-secret-not-production-at-least-32-bytes",
        jwt_algorithm="HS256",
        livekit_url="wss://livekit.test",
        livekit_api_key="test-key",
        livekit_api_secret="test-livekit-secret-at-least-32-bytes",
        livekit_agent_name="intervia-voice",
        trace_logs_enabled=True,
        app_env="development",
    )
    monkeypatch.setattr(api, "get_settings", lambda: value)
    monkeypatch.setattr(api, "stt_provider_settings", lambda: SimpleNamespace(provider="qwen", model="fake"))
    return value


@pytest.mark.asyncio
async def test_only_admin_can_issue_room_and_reference_never_reaches_worker(settings, monkeypatch):
    traces = []
    monkeypatch.setattr(api, "trace_event", lambda *args, **kw: traces.append((args, kw)))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_user] = lambda: {"sub": "user-1", "roles": []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        assert (await client.post("/ai/speech/test/livekit-token", json={})).status_code == 403
        assert not traces
        app.dependency_overrides[current_user] = lambda: {"sub": "admin-1", "roles": ["ADMIN"]}
        result = await client.post(
            "/ai/speech/test/livekit-token",
            json={"context": "Backend Developer", "referenceText": "SECRET ANSWER"},
        )
        assert result.status_code == 200
        room = result.json()
        livekit_claims = jwt.decode(
            room["participantToken"],
            settings.livekit_api_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        runtime = livekit_claims["roomConfig"]["agents"][0]["metadata"].removeprefix("stt-test:")
        claims = jwt.decode(runtime, settings.jwt_secret, algorithms=["HS256"], audience=api.AUDIENCE)
        assert "SECRET ANSWER" not in json.dumps(claims)
        assert claims["roomName"] == room["roomName"]
        assert claims["testId"] == room["testId"]
        assert livekit_claims["video"]["room"] == room["roomName"]
        headers = {"Authorization": f"Bearer {runtime}"}
        start = await client.post("/ai/speech/test/livekit/runtime/start", headers=headers)
        assert start.json()["context"] == "Backend Developer"
        assert "referenceText" not in start.json()
        logged = await client.post(
            "/ai/speech/test/livekit/runtime/events",
            headers=headers,
            json={
                "event": "stt_test_completed",
                "fields": {"test_id": "forged", "session_id": "forged", "status": "stopped"},
            },
        )
        assert logged.status_code == 200
        assert traces[-1][1]["test_id"] == room["testId"]
        assert "session_id" not in traces[-1][1]
        assert traces[0][1]["reference_text"] == "SECRET ANSWER"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "interview:voice-runtime"},
        {"aud": "interview-voice"},
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"testId": None},
    ],
)
async def test_runtime_rejects_interview_wrong_scope_expired_or_incomplete_tokens(settings, changes):
    app = FastAPI()
    app.include_router(api.router)
    claims = {
        "sub": "admin-1",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=1),
        "scope": api.SCOPE,
        "tokenType": "stt-test-runtime",
        "aud": api.AUDIENCE,
        "testId": "run-1",
        "roomName": "stt-test-run-1",
        "identity": "admin-1",
    }
    claims.update(changes)
    token = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        response = await client.post(
            "/ai/speech/test/livekit/runtime/start", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 401


class Stream:
    def __init__(self):
        self.events = asyncio.Queue()
        self.frames = []
        self.closed = False
        self.final_text = "Em dung Node.js"
        self.interim_text = None

    def push_frame(self, frame):
        self.frames.append(frame)
        if self.interim_text:
            self.events.put_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language="vi", text=self.interim_text, confidence=1)],
                )
            )

    def end_input(self):
        self.events.put_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language="vi", text=self.final_text, confidence=1)],
            )
        )
        self.events.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.events.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["stop", "disconnect", "trace_error", "term_repair"])
async def test_livekit_room_drains_final_and_cleans_up_without_interview_core(monkeypatch, exit_mode):
    handlers, sent, posted = {}, [], []
    ready, audio_fed = asyncio.Event(), asyncio.Event()
    stream = Stream()
    if exit_mode == "term_repair":
        stream.final_text = stream.interim_text = "Em dùng postgre sql."
    provider = SimpleNamespace(stream=lambda **kw: stream, aclose=None)
    closed = []

    async def close_provider():
        closed.append("provider")

    provider.aclose = close_provider

    async def publish(data, **kwargs):
        event = json.loads(data)
        sent.append(event)
        if event["type"] == "ready":
            ready.set()

    def on(name):
        def register(fn):
            handlers[name] = fn
            return fn

        return register

    room = SimpleNamespace(
        name="stt-test-run-1", on=on, local_participant=SimpleNamespace(publish_data=publish)
    )

    async def connect():
        pass

    ctx = SimpleNamespace(
        room=room,
        job=SimpleNamespace(room=SimpleNamespace(name=room.name)),
        connect=connect,
        add_shutdown_callback=lambda fn: None,
        shutdown=lambda **kw: closed.append("job"),
    )

    class Client:
        async def post(self, path, **kw):
            assert path.startswith(worker.RUNTIME)  # Never call interview runtime.
            if path.endswith("/start"):
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", "http://api"),
                    json={
                        "testId": "run-1",
                        "roomName": room.name,
                        "identity": "admin-1",
                        "context": "PostgreSQL" if exit_mode == "term_repair" else "Backend",
                        "locale": "vi-VN",
                        "traceEnabled": True,
                    },
                )
            posted.append(kw["json"])
            return httpx.Response(
                503 if exit_mode == "trace_error" else 200, request=httpx.Request("POST", "http://api")
            )

        async def aclose(self):
            closed.append("http")

    tasks = []

    class Session:
        def __init__(self, **kw):
            assert kw["stt"] is provider

        def on(self, *args):
            pass

        async def start(self, *, agent, **kw):
            async def audio():
                yield rtc.AudioFrame(b"\x01\x00" * 320, 16000, 1, 320)
                audio_fed.set()
                await asyncio.Event().wait()

            async def consume():
                async for _ in agent.stt_node(audio(), None):
                    pass

            tasks.append(asyncio.create_task(consume()))

        async def aclose(self):
            await asyncio.gather(*tasks)
            closed.append("session")

    monkeypatch.setattr(worker, "AgentSession", Session)
    monkeypatch.setattr(worker, "create_stt", lambda *args, **kw: provider)
    monkeypatch.setattr(
        worker, "stt_provider_settings", lambda: SimpleNamespace(provider="qwen", model="fake")
    )
    monkeypatch.setattr(worker, "recognition_options", lambda *args: {})
    monkeypatch.setattr(worker, "recognition_room_options", lambda *args, **kw: None)
    task = asyncio.create_task(worker.run_stt_test(ctx, Client()))
    await asyncio.wait_for(ready.wait(), 2)
    await asyncio.wait_for(audio_fed.wait(), 2)
    # A packet from someone else cannot stop this run.
    handlers["data_received"](
        SimpleNamespace(
            topic=worker.TOPIC, data=b'{"type":"stop"}', participant=SimpleNamespace(identity="intruder")
        )
    )
    await asyncio.sleep(0)
    assert not task.done()
    if exit_mode == "disconnect":
        handlers["participant_disconnected"](SimpleNamespace(identity="admin-1"))
    else:
        handlers["data_received"](
            SimpleNamespace(
                topic=worker.TOPIC, data=b'{"type":"stop"}', participant=SimpleNamespace(identity="admin-1")
            )
        )
    await asyncio.wait_for(task, 3)
    assert [e["type"] for e in sent] == (
        ["ready", "interim", "final", "stopped"]
        if exit_mode == "term_repair"
        else ["ready", "final", "stopped"]
    )
    if exit_mode == "term_repair":
        assert sent[1]["text"] == "Em dùng postgre sql."
        assert sent[2]["text"] == "Em dùng PostgreSQL."
        records = [e["fields"] for e in posted if e["event"] == "stt_test_transcript"]
        assert records[0]["decisions"][0]["reason"] == "unstable_interim_tail"
        assert records[1]["raw_text"] == "Em dùng postgre sql."
        assert records[1]["text"] == "Em dùng PostgreSQL."
    assert sent[-1]["traceDelivered"] is (exit_mode != "trace_error")
    assert stream.closed and len(stream.frames) == 1
    summary = next(e["fields"] for e in posted if e["event"] == "stt_test_completed")
    assert summary["final_count"] == 1 and summary["audio_received_ms"] == 20
    assert set(closed) == {"provider", "session", "http", "job"}
