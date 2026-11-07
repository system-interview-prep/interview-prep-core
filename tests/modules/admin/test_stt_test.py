import asyncio
import io
import json
import wave
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from livekit.agents.stt import SpeechEventType
from starlette.datastructures import Headers

from src.core import trace_logging
from src.modules.admin.ai import stt_test


def event(kind, text=""):
    return SimpleNamespace(type=kind, alternatives=[SimpleNamespace(text=text, confidence=0.9)])


class FakeStream:
    def __init__(self, failure=False):
        self.queue = asyncio.Queue()
        self.closed = False
        self.ended = False
        self.failure = failure
        self.samples = 0

    def push_frame(self, frame):
        self.samples += frame.samples_per_channel
        self.queue.put_nowait(event(SpeechEventType.INTERIM_TRANSCRIPT, "Em dùng you state"))

    def end_input(self):
        self.ended = True
        self.queue.put_nowait(event(SpeechEventType.END_OF_SPEECH))
        self.queue.put_nowait(event(SpeechEventType.FINAL_TRANSCRIPT, "Em dùng you state để giữ state."))
        self.queue.put_nowait(event(SpeechEventType.FINAL_TRANSCRIPT, "Nhà trường bắt em phải nộp giấy."))
        self.queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.failure:
            raise RuntimeError("secret-provider-token")
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def aclose(self):
        self.closed = True


class FakeProvider:
    def __init__(self, failure=False):
        self.input = FakeStream(failure)
        self.closed = False

    def stream(self, **kwargs):
        return self.input

    async def aclose(self):
        self.closed = True


class FakeSocket:
    def __init__(self, messages):
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.sent = []
        self.closed = False

    async def receive(self):
        return await self.messages.get()

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


def wav_upload():
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 2 * 8000)
    return UploadFile(
        file=io.BytesIO(data.getvalue()),
        filename="human.wav",
        headers=Headers(
            {
                "content-type": "audio/wav",
            }
        ),
    )


@pytest.fixture
def setup_trace(monkeypatch, tmp_path):
    settings = SimpleNamespace(trace_logs_enabled=True, app_env="development", trace_logs_dir=str(tmp_path))
    monkeypatch.setattr(trace_logging, "get_settings", lambda: settings)
    monkeypatch.setattr(trace_logging, "record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(stt_test, "get_settings", lambda: settings)
    monkeypatch.setattr(
        stt_test, "stt_provider_settings", lambda: SimpleNamespace(provider="assemblyai", model="fake")
    )
    monkeypatch.setenv("LIVEKIT_STT_LANGUAGE", "session")
    providers = []

    def factory(config, language, context):
        assert language == "vi"
        provider = FakeProvider()
        providers.append(provider)
        return provider

    monkeypatch.setattr(stt_test, "_provider", factory)
    return settings, providers, tmp_path


def read_records(path):
    return [
        json.loads(line)
        for line in (path / "test-stt" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.asyncio
async def test_file_trace_keeps_all_finals_raw_hypothesis_and_correction(setup_trace):
    settings, providers, path = setup_trace
    result = await stt_test.transcribe_file(
        wav_upload(),
        stt_test.SttTestOptions(
            context="Backend Developer useState",
            referenceText="Bài mẫu khác hẳn bản nhận dạng.",
        ),
    )
    assert result["text"] == "Em dùng useState để giữ state. Nhà trường bắt em phải nộp giấy."
    assert result["traceEnabled"] is True
    records = read_records(path)
    assert {row["test_id"] for row in records} == {result["testId"]}
    assert {row["workflow"] for row in records} == {"test_stt"}
    finals = [row for row in records if row.get("kind") == "final"]
    assert len(finals) == 2
    assert finals[0]["raw_text"] == "Em dùng you state để giữ state."
    assert finals[0]["text"] == "Em dùng useState để giữ state."
    assert finals[0]["decisions"][0]["replacement"] == "useState"
    assert finals[1]["changed"] is False
    summary = records[-1]
    assert summary["status"] == "completed"
    assert summary["final_count"] == 2
    assert summary["audio_received_ms"] == 1000
    assert summary["text"] == result["text"]
    assert "Bài mẫu" not in summary["text"]
    assert providers[0].input.samples == 16000
    assert providers[0].input.ended and providers[0].input.closed and providers[0].closed
    assert list(path.iterdir()) == [path / "test-stt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_stream_trace_drains_last_transcripts_even_on_stop_or_disconnect(setup_trace, disconnect):
    settings, providers, path = setup_trace
    socket = FakeSocket(
        [
            {"text": json.dumps({"type": "configure", "context": "useState", "referenceText": "Bài đọc."})},
            {"bytes": b"\x00\x00" * 1600},
            {"type": "websocket.disconnect"} if disconnect else {"text": '{"type":"stop"}'},
        ]
    )
    await stt_test.transcribe_stream(socket)
    records = read_records(path)
    assert records[-1]["final_count"] == 2
    assert records[-1]["status"] == ("disconnected" if disconnect else "stopped")
    ready = socket.sent[0]
    assert ready["testId"] == records[-1]["test_id"]
    assert ready["traceEnabled"] is True
    if not disconnect:
        assert len([row for row in socket.sent if row.get("type") == "final"]) == 2
        assert socket.sent[-1]["type"] == "stopped"
    assert socket.closed and providers[0].input.closed and providers[0].closed


@pytest.mark.asyncio
async def test_provider_error_is_traced_without_leaking_exception_secrets(setup_trace, monkeypatch):
    settings, providers, path = setup_trace
    provider = FakeProvider(failure=True)
    monkeypatch.setattr(stt_test, "_provider", lambda *args: provider)
    socket = FakeSocket([{"text": '{"type":"configure"}'}])
    await asyncio.wait_for(stt_test.transcribe_stream(socket), timeout=3)
    records = read_records(path)
    assert records[-1]["status"] == "error"
    assert any(row.get("error_type") == "RuntimeError" for row in records)
    assert "secret-provider-token" not in json.dumps(records)
    assert provider.input.closed and provider.closed


@pytest.mark.asyncio
async def test_provider_setup_failure_still_has_a_run_id_and_failure_trace(setup_trace, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("secret-provider-token")

    monkeypatch.setattr(stt_test, "_provider", unavailable)
    socket = FakeSocket([{"text": '{"type":"configure"}'}])
    await stt_test.transcribe_stream(socket)
    records = read_records(setup_trace[2])
    assert records[0]["event"] == "stt_test_started"
    assert records[-1]["status"] == "error"
    assert socket.sent[0]["testId"] == records[-1]["test_id"]
    assert "secret-provider-token" not in json.dumps(records)


@pytest.mark.asyncio
async def test_invalid_audio_is_not_reported_as_a_successful_test(setup_trace):
    settings, providers, path = setup_trace
    socket = FakeSocket([{"text": '{"type":"configure"}'}, {"bytes": b"odd"}])
    await stt_test.transcribe_stream(socket)
    assert read_records(path)[-1]["status"] == "error"
    assert providers[0].input.closed and providers[0].closed


@pytest.mark.asyncio
async def test_reference_never_steers_the_recognizer_or_repairs(setup_trace):
    settings, providers, path = setup_trace
    result = await stt_test.transcribe_file(wav_upload(), stt_test.SttTestOptions(referenceText="useState"))
    assert result["text"].startswith("Em dùng you state")


@pytest.mark.asyncio
async def test_concurrent_tests_have_separate_ids_and_summaries(setup_trace):
    settings, providers, path = setup_trace
    results = await asyncio.gather(
        *[stt_test.transcribe_file(wav_upload(), stt_test.SttTestOptions()) for _ in range(2)]
    )
    assert results[0]["testId"] != results[1]["testId"]
    summaries = [row for row in read_records(path) if row["event"] == "stt_test_completed"]
    assert len(summaries) == 2
    assert all(row["final_count"] == 2 for row in summaries)


def test_timing_fields_distinguish_audio_time_from_observed_result_delay(setup_trace, monkeypatch):
    settings, providers, path = setup_trace
    clock = [1.0]
    monkeypatch.setattr(stt_test, "perf_counter", lambda: clock[0])
    trace = stt_test.SttTestTrace("stream", "qwen", "fake", "vi")
    clock[0] = 1.1
    trace.audio(1600)
    clock[0] = 1.2
    trace.speech_state("ended")
    clock[0] = 1.22
    trace.input_ended("stop")
    clock[0] = 1.25
    trace.transcript("Em dùng Redis.", final=True, confidence=1.0)
    record = read_records(path)[-1]
    assert record["elapsed_ms"] == 250
    assert record["audio_received_ms"] == 100
    assert record["after_last_audio_ms"] == 150
    assert record["speech_end_to_result_ms"] == 50
    assert record["input_end_to_result_ms"] == 30
    assert record["confidence"] is None  # Qwen does not supply calibrated confidence.


def test_interim_tail_and_final_use_the_same_policy_in_legacy_and_file_traces(setup_trace):
    _, _, path = setup_trace
    trace = stt_test.SttTestTrace("stream", "qwen", "fake", "vi")
    trace.configure(stt_test.SttTestOptions(context="PostgreSQL"))
    raw = "Em dùng postgre sql."
    assert trace.transcript(raw, final=False)["text"] == raw
    assert trace.transcript(raw, final=True)["text"] == "Em dùng PostgreSQL."
    records = [r for r in read_records(path) if r["event"] == "stt_test_transcript"]
    assert records[0]["decisions"][0]["reason"] == "unstable_interim_tail"
    assert records[1]["raw_text"] == raw and records[1]["changed"]


def test_raw_provider_spacing_is_preserved_for_debugging(setup_trace):
    trace = stt_test.SttTestTrace("stream", "assemblyai", "fake", "vi")
    trace.transcript("  Em dùng Redis.  ", final=True)
    record = read_records(setup_trace[2])[-1]
    assert record["raw_text"] == "  Em dùng Redis.  "
    assert record["text"] == "Em dùng Redis."


def test_admin_file_endpoint_preserves_optional_form_fields(setup_trace):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.core.security import require_admin
    from src.modules.admin.ai.controller import router

    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[require_admin] = lambda: {"sub": "admin-test"}
    with TestClient(app) as client:
        response = client.post(
            "/admin/ai/speech/test/transcribe",
            files={"audio": ("human.wav", wav_upload().file.read(), "audio/wav")},
            data={"context": "useState", "referenceText": "Bài đọc độc lập."},
        )
    assert response.status_code == 200
    assert response.json()["text"].startswith("Em dùng useState")
    configured = next(row for row in read_records(setup_trace[2]) if row["event"] == "stt_test_configured")
    assert configured["reference_text"] == "Bài đọc độc lập."


@pytest.mark.asyncio
async def test_unauthenticated_socket_never_starts_a_test(setup_trace, monkeypatch):
    from src.modules.admin.ai import controller

    async def denied(socket, db):
        await socket.close()
        return False

    monkeypatch.setattr(controller, "_authenticate_speech_websocket", denied)
    socket = FakeSocket([])
    await controller.admin_speech_test_transcribe_stream(socket, None)
    assert socket.closed
    assert setup_trace[1] == []
    assert not (setup_trace[2] / "test-stt").exists()


@pytest.mark.asyncio
async def test_trace_disabled_is_reported_honestly_to_admin(setup_trace):
    settings, providers, path = setup_trace
    settings.trace_logs_enabled = False
    result = await stt_test.transcribe_file(wav_upload(), stt_test.SttTestOptions())
    assert result["traceEnabled"] is False
    assert not (path / "test-stt").exists()
