"""Cho giọng TTS đọc rồi để STT nghe lại: tìm câu và thuật ngữ bị đọc sai.

Nghiên cứu 24/10 (INTERVIEWER_SPEECH_CONTROL_RESEARCH, mục 7): một câu dễ đọc cho
TTS thì khi STT nghe lại ít lỗi. Script này lấy các câu soạn sẵn của người phỏng
vấn (và câu trong file tuỳ chọn), đưa qua cùng bước chuẩn hoá mà phòng voice dùng
(``speakable_text``), cho VieNeu đọc, rồi cho OpenAI transcription nghe lại.

Kết quả ghi ra trace-logs/tts_roundtrip_<giờ>.md và .json:
- CER (tỉ lệ lỗi ký tự) từng câu, các câu tệ nhất trước;
- thuật ngữ chữ Latin có trong câu mà STT không nghe ra (ứng viên cho
  ``adapters/voice/pronunciations_vi.py``).

Chạy trong container backend, khi server VieNeu đang chạy:

    docker compose exec backend python scripts/tts_roundtrip.py --limit 40
    docker compose exec backend python scripts/tts_roundtrip.py --file scripts/my_lines.txt

Cần: VIENEU_TTS_BASE_URL (mặc định http://host.docker.internal:8000),
OPENAI_API_KEY. Mỗi câu tốn một lượt TTS và một lượt nghe lại.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import time
import unicodedata
import wave
from pathlib import Path

import httpx

from src.modules.interviews.adapters.voice.language import speakable_text
from src.modules.interviews.core import interviewer_script as script

POOLS = (
    "ACK_FALLBACK_VI", "INTRO_ACK_VI", "NON_ANSWER_ACK_VI", "GIVE_UP_ACK_VI", "SKIP_ACK_VI", "PROBE_ACK_VI",
    "PROBE_FALLBACK_VI", "PROCESSING_FILLERS_VI", "NOT_HEARD_VI", "CLOSE_COMPLETED_VI", "QNA_MORE_VI",
)
# Technical sentences of the kind the interviewer asks, heavy on English terms.
SAMPLE_QUESTIONS = (
    "Khi Redis lỗi, hệ thống của bạn xử lý cache invalidation thế nào?",
    "Bạn dùng JWT và refresh token ra sao trong REST API?",
    "Trong CI/CD, bạn rollback một bản deploy lỗi trên Kubernetes thế nào?",
    "INNER JOIN và LEFT JOIN trong SQL khác nhau ở chỗ nào?",
    "API trả về HTTP 429 thì client nên retry có backoff thế nào?",
    "Bạn đo p95 latency của endpoint này bằng công cụ gì?",
    "Docker image với container khác nhau ở chỗ nào?",
    "Bạn dùng Kafka hay RabbitMQ cho message queue, vì sao?",
)


def _lines(limit: int, extra_file: str | None) -> list[str]:
    lines: list[str] = list(SAMPLE_QUESTIONS)
    for name in POOLS:
        lines += list(getattr(script, name, ()))
    if extra_file:
        text = Path(extra_file).read_text(encoding="utf-8")
        lines += [line.strip() for line in text.splitlines() if line.strip()]
    seen: list[str] = []
    for line in lines:
        if line not in seen:
            seen.append(line)
    return seen[:limit] if limit else seen


def _synthesize(client: httpx.Client, base_url: str, text: str) -> tuple[bytes, int]:
    payload = {"input": text, "response_format": "pcm", "stream_format": "sse"}
    for key, env in (("voice", "VIENEU_TTS_VOICE"), ("model", "VIENEU_TTS_MODEL")):
        if os.getenv(env):
            payload[key] = os.environ[env]
    if os.getenv("VIENEU_TTS_SAMPLE_RATE"):
        payload["sample_rate"] = os.environ["VIENEU_TTS_SAMPLE_RATE"]
    headers = {"Content-Type": "application/json"}
    if os.getenv("VIENEU_TTS_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['VIENEU_TTS_API_KEY']}"
    pcm = bytearray()
    url = f"{base_url}/v1/audio/speech"
    with client.stream("POST", url, json=payload, headers=headers, timeout=180) as response:
        response.raise_for_status()
        rate = int(response.headers.get("x-sample-rate", payload.get("sample_rate", 16000)))
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            if isinstance(event, dict) and event.get("audio"):
                pcm += base64.b64decode(event["audio"])
    return bytes(pcm), rate


def _wav(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def _transcribe(wav: bytes) -> str:
    from openai import OpenAI

    client = OpenAI()
    model = os.getenv("ROUNDTRIP_STT_MODEL", "gpt-4o-transcribe")
    result = client.audio.transcriptions.create(model=model, file=("line.wav", wav), language="vi")
    return str(getattr(result, "text", "") or "")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def _cer(reference: str, hypothesis: str) -> float:
    ref, hyp = _norm(reference), _norm(hypothesis)
    if not ref:
        return 0.0
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i]
        for j, h in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h)))
        previous = current
    return previous[-1] / len(ref)


def _missed_terms(spoken: str, heard: str) -> list[str]:
    heard_norm = _norm(heard)
    terms = re.findall(r"[A-Za-z][A-Za-z0-9+#.]*[A-Za-z0-9+#]", spoken)
    return [term for term in dict.fromkeys(terms) if _norm(term) not in heard_norm]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--file", default=None)
    args = parser.parse_args()
    base_url = os.getenv("VIENEU_TTS_BASE_URL", "http://host.docker.internal:8000").rstrip("/")
    rows = []
    with httpx.Client() as client:
        for i, line in enumerate(_lines(args.limit, args.file), 1):
            spoken = speakable_text(line, locale="vi-VN")
            try:
                pcm, rate = _synthesize(client, base_url, spoken)
                heard = _transcribe(_wav(pcm, rate))
                row = {"line": line, "spoken": spoken, "heard": heard, "cer": round(_cer(spoken, heard), 3),
                       "missed_terms": _missed_terms(spoken, heard)}
            except Exception as exc:  # noqa: BLE001 - one bad line does not stop the run
                row = {"line": line, "spoken": spoken, "error": f"{type(exc).__name__}: {exc}"[:200]}
            rows.append(row)
            print(f"[{i}] CER {row.get('cer', '—')} {line[:70]}", flush=True)

    scored = [r for r in rows if "cer" in r]
    misses: dict[str, int] = {}
    for r in scored:
        for term in r["missed_terms"]:
            misses[term] = misses.get(term, 0) + 1
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = Path("trace-logs")
    out.mkdir(exist_ok=True)
    base = out / f"tts_roundtrip_{stamp}"
    mean = round(sum(r["cer"] for r in scored) / len(scored), 3) if scored else None
    base.with_suffix(".json").write_text(
        json.dumps({"mean_cer": mean, "missed_terms": misses, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        f"# TTS đọc → STT nghe lại ({stamp})", "",
        f"- Số câu: {len(rows)} (lỗi: {len(rows) - len(scored)}); CER trung bình: {mean}",
        "- Thuật ngữ STT không nghe ra (số lần): "
        + (", ".join(f"{t} ({n})" for t, n in sorted(misses.items(), key=lambda x: -x[1])) or "không có"),
        "", "| CER | Câu đưa vào TTS | STT nghe được | Thuật ngữ sót |", "|---|---|---|---|",
    ]
    for r in sorted(scored, key=lambda x: -x["cer"]):
        lines.append(f"| {r['cer']} | {r['spoken']} | {r['heard']} | {', '.join(r['missed_terms'])} |")
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:5]))
    print(f"\nĐã ghi {base}.md và .json")


if __name__ == "__main__":
    main()
