import hashlib
import json
from pathlib import Path

import pytest

from src.modules.interviews.adapters.voice.evaluation import (
    edit_counts,
    evaluate_manifest,
    normalize_vi_text,
    score_pair,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _sample(tmp_path: Path, sample_id: str, reference: str, dialect: str, environment: str) -> dict:
    audio = tmp_path / f"{sample_id}.wav"
    audio.write_bytes(f"real-audio-placeholder-{sample_id}".encode())
    return {
        "id": sample_id,
        "audioPath": audio.name,
        "reference": reference,
        "speakerId": f"speaker-{sample_id}",
        "split": "test",
        "dialect": dialect,
        "environment": environment,
        "device": "laptop",
        "durationSeconds": 1.0,
        "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "consent": "test-consent",
    }


def test_vietnamese_normalization_preserves_diacritics_and_words():
    assert normalize_vi_text("  TÔI dùng Node.js, ạ! ") == "tôi dùng node js ạ"


def test_edit_counts_and_pair_scoring_are_micro_aggregatable():
    counts = edit_counts(["tôi", "là", "an"], ["tôi", "an", "ạ"])
    assert counts.substitutions == 2
    assert counts.deletions == 0
    assert counts.insertions == 0
    assert counts.errors == 2
    word_counts, char_counts = score_pair("Xin chào!", "xin chào")
    assert word_counts.rate == 0
    assert char_counts.rate == 0


def test_evaluate_manifest_penalizes_missing_predictions_and_groups_results(tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(
        manifest,
        [
            _sample(tmp_path, "n-1", "xin chào", "north", "quiet"),
            _sample(tmp_path, "s-1", "tôi là An", "south", "noise"),
        ],
    )
    _write_jsonl(predictions, [{"id": "n-1", "hypothesis": "Xin chào."}])
    report = evaluate_manifest(manifest, predictions)
    assert report["coverage"] == 0.5
    assert report["exactMatchRate"] == 0.5
    assert report["wer"]["deletions"] == 3
    assert len(report["groups"]) == 2


def test_evaluate_manifest_rejects_predictions_outside_frozen_manifest(tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(manifest, [_sample(tmp_path, "a", "a", "north", "quiet")])
    _write_jsonl(predictions, [{"id": "b", "hypothesis": "b"}])
    with pytest.raises(ValueError, match="outside the manifest"):
        evaluate_manifest(manifest, predictions)
