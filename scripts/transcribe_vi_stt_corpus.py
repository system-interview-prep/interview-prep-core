"""Run the frozen corpus through the same LiveKit Inference STT model as production."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import av
from livekit import rtc
from livekit.agents import inference
from livekit.agents.utils import http_context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.modules.interviews.adapters.voice.evaluation import validate_corpus_manifest  # noqa: E402


def decode_audio(path: Path) -> list[rtc.AudioFrame]:
    frames: list[rtc.AudioFrame] = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(str(path)) as container:
        for decoded in container.decode(audio=0):
            for output in resampler.resample(decoded):
                data = output.to_ndarray().tobytes()
                frames.append(
                    rtc.AudioFrame(
                        data=data,
                        sample_rate=16000,
                        num_channels=1,
                        samples_per_channel=len(data) // 2,
                    )
                )
        for output in resampler.resample(None):
            data = output.to_ndarray().tobytes()
            frames.append(
                rtc.AudioFrame(
                    data=data,
                    sample_rate=16000,
                    num_channels=1,
                    samples_per_channel=len(data) // 2,
                )
            )
    if not frames:
        raise ValueError(f"decoded no audio frames from {path}")
    return frames


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def run(args: argparse.Namespace) -> None:
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    validate_corpus_manifest(args.manifest, expected_samples=args.expected_samples)
    rows = read_jsonl(args.manifest)
    if args.shakedown_per_dialect:
        rows = [
            row
            for dialect in ("north", "central", "south")
            for row in [item for item in rows if item.get("dialect") == dialect][
                : args.shakedown_per_dialect
            ]
        ]
    completed = {row["id"]: row for row in read_jsonl(args.output)} if args.output.exists() else {}
    errors_path = args.output.with_suffix(".errors.jsonl")
    failures = 0
    async with http_context.open():
        stt = inference.STT(model=args.model, language=args.language)
        try:
            for index, row in enumerate(rows, start=1):
                if row["id"] in completed:
                    continue
                audio_path = (args.manifest.parent / row["audioPath"]).resolve()
                try:
                    speech_stream = stt.stream(language=args.language)
                    event = None
                    try:
                        for frame in decode_audio(audio_path):
                            speech_stream.push_frame(frame)
                        speech_stream.end_input()
                        async for candidate in speech_stream:
                            if candidate.alternatives:
                                event = candidate
                    finally:
                        await speech_stream.aclose()

                    if event is None:
                        raise RuntimeError("STT stream ended without a transcript")
                    hypothesis = event.alternatives[0].text if event.alternatives else ""
                    completed[row["id"]] = {
                        "id": row["id"],
                        "hypothesis": hypothesis,
                        "model": args.model,
                        "language": args.language,
                    }
                    args.output.write_text(
                        "".join(
                            json.dumps(completed[key], ensure_ascii=False) + "\n"
                            for key in sorted(completed)
                        ),
                        encoding="utf-8",
                    )
                    print(f"[{index}/{len(rows)}] {row['id']}: {hypothesis}", flush=True)
                except Exception as exc:
                    failures += 1
                    with errors_path.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "id": row["id"],
                                    "errorType": type(exc).__name__,
                                    "error": str(exc)[:500],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    print(
                        f"[{index}/{len(rows)}] {row['id']}: ERROR {type(exc).__name__}",
                        flush=True,
                    )
        finally:
            await stt.aclose()
    if failures:
        raise RuntimeError(f"STT failed for {failures} of {len(rows)} samples; see {errors_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-samples", type=int, default=300)
    parser.add_argument("--model", default="assemblyai/universal-3-6-pro")
    parser.add_argument("--language", default="vi")
    parser.add_argument("--shakedown-per-dialect", type=int, default=0)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
