"""Reproducible WER/CER scoring for a consented Vietnamese speech corpus."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

_PUNCTUATION_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_vi_text(text: str) -> str:
    """Normalize Unicode and punctuation without deleting spoken fillers."""
    normalized = unicodedata.normalize("NFC", text).casefold().replace("_", " ")
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


@dataclass(frozen=True)
class EditCounts:
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    reference_units: int = 0

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self) -> float:
        return self.errors / self.reference_units if self.reference_units else 0.0

    def __add__(self, other: EditCounts) -> EditCounts:
        return EditCounts(
            substitutions=self.substitutions + other.substitutions,
            deletions=self.deletions + other.deletions,
            insertions=self.insertions + other.insertions,
            reference_units=self.reference_units + other.reference_units,
        )


def edit_counts(reference: Sequence[str], hypothesis: Sequence[str]) -> EditCounts:
    """Return a deterministic minimum-edit alignment and its S/D/I counts."""
    rows: list[list[tuple[int, int, int, int]]] = [
        [(i, 0, i, 0) for i in range(len(hypothesis) + 1)]
    ]
    rows[0] = [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    for index, ref_unit in enumerate(reference, start=1):
        row: list[tuple[int, int, int, int]] = [(index, 0, index, 0)]
        for column, hyp_unit in enumerate(hypothesis, start=1):
            diagonal = rows[index - 1][column - 1]
            substitution = (
                diagonal
                if ref_unit == hyp_unit
                else (diagonal[0] + 1, diagonal[1] + 1, diagonal[2], diagonal[3])
            )
            deletion = rows[index - 1][column]
            deletion = (deletion[0] + 1, deletion[1], deletion[2] + 1, deletion[3])
            insertion = row[column - 1]
            insertion = (insertion[0] + 1, insertion[1], insertion[2], insertion[3] + 1)
            # Alignments can have the same total distance. Prefer the
            # diagonal substitution, then deletion, then insertion so the
            # published S/D/I breakdown stays stable across runs.
            row.append(
                min(
                    ((substitution, 0), (deletion, 1), (insertion, 2)),
                    key=lambda candidate: (candidate[0][0], candidate[1]),
                )[0]
            )
        rows.append(row)
    _, substitutions, deletions, insertions = rows[-1][-1]
    return EditCounts(substitutions, deletions, insertions, len(reference))


def score_pair(reference: str, hypothesis: str) -> tuple[EditCounts, EditCounts]:
    normalized_reference = normalize_vi_text(reference)
    normalized_hypothesis = normalize_vi_text(hypothesis)
    word_counts = edit_counts(normalized_reference.split(), normalized_hypothesis.split())
    char_counts = edit_counts(
        list(normalized_reference.replace(" ", "")),
        list(normalized_hypothesis.replace(" ", "")),
    )
    return word_counts, char_counts


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        records.append(value)
    return records


def validate_corpus_manifest(manifest_path: Path, *, expected_samples: int | None = None) -> dict:
    manifest_path = manifest_path.resolve()
    rows = _read_jsonl(manifest_path)
    required = {
        "id",
        "audioPath",
        "reference",
        "speakerId",
        "split",
        "dialect",
        "environment",
        "device",
        "durationSeconds",
        "sha256",
        "consent",
    }
    seen_ids: set[str] = set()
    speakers: set[str] = set()
    dialects: set[str] = set()
    total_seconds = 0.0
    for row in rows:
        missing = sorted(key for key in required if not str(row.get(key, "")).strip())
        if missing:
            raise ValueError(f"manifest sample {row.get('id')!r} is missing: {', '.join(missing)}")
        sample_id = str(row["id"])
        if sample_id in seen_ids:
            raise ValueError(f"manifest id is duplicated: {sample_id!r}")
        seen_ids.add(sample_id)
        relative = Path(str(row["audioPath"]))
        audio_path = (manifest_path.parent / relative).resolve()
        if manifest_path.parent not in audio_path.parents:
            raise ValueError(f"manifest sample {sample_id!r} escapes the corpus directory")
        if not audio_path.is_file():
            raise ValueError(f"manifest sample {sample_id!r} audio does not exist: {relative}")
        digest = sha256(audio_path.read_bytes()).hexdigest()
        if digest != str(row["sha256"]).lower():
            raise ValueError(f"manifest sample {sample_id!r} sha256 does not match its audio")
        duration = float(row["durationSeconds"])
        if duration <= 0:
            raise ValueError(f"manifest sample {sample_id!r} has invalid durationSeconds")
        total_seconds += duration
        speakers.add(str(row["speakerId"]))
        dialects.add(str(row["dialect"]))
    if expected_samples is not None and len(rows) != expected_samples:
        raise ValueError(f"expected {expected_samples} samples, found {len(rows)}")
    return {
        "samples": len(rows),
        "speakers": len(speakers),
        "dialects": sorted(dialects),
        "durationSeconds": round(total_seconds, 3),
    }


def evaluate_manifest(manifest_path: Path, predictions_path: Path) -> dict:
    corpus = validate_corpus_manifest(manifest_path)
    manifest = _read_jsonl(manifest_path)
    predictions = _read_jsonl(predictions_path)
    prediction_by_id: dict[str, str] = {}
    for record in predictions:
        sample_id = str(record.get("id", "")).strip()
        if not sample_id or sample_id in prediction_by_id:
            raise ValueError(f"prediction id is missing or duplicated: {sample_id!r}")
        prediction_by_id[sample_id] = str(record.get("hypothesis", ""))

    seen_ids: set[str] = set()
    word_total = EditCounts()
    char_total = EditCounts()
    exact_matches = 0
    groups: dict[tuple[str, str], tuple[EditCounts, EditCounts, int]] = defaultdict(
        lambda: (EditCounts(), EditCounts(), 0)
    )
    for sample in manifest:
        sample_id = str(sample.get("id", "")).strip()
        reference = str(sample.get("reference", "")).strip()
        audio_path = str(sample.get("audioPath", "")).strip()
        if not sample_id or sample_id in seen_ids:
            raise ValueError(f"manifest id is missing or duplicated: {sample_id!r}")
        if not reference or not audio_path:
            raise ValueError(f"manifest sample {sample_id!r} needs reference and audioPath")
        seen_ids.add(sample_id)
        hypothesis = prediction_by_id.get(sample_id, "")
        word_counts, char_counts = score_pair(reference, hypothesis)
        word_total += word_counts
        char_total += char_counts
        if normalize_vi_text(reference) == normalize_vi_text(hypothesis):
            exact_matches += 1
        key = (str(sample.get("dialect", "unknown")), str(sample.get("environment", "unknown")))
        group_words, group_chars, group_samples = groups[key]
        groups[key] = (group_words + word_counts, group_chars + char_counts, group_samples + 1)

    extras = sorted(set(prediction_by_id) - seen_ids)
    if extras:
        raise ValueError(f"predictions contain ids outside the manifest: {', '.join(extras[:5])}")

    sample_count = len(manifest)
    predicted_count = len(seen_ids & prediction_by_id.keys())

    def serialize_counts(counts: EditCounts) -> dict:
        return {**asdict(counts), "errors": counts.errors, "rate": round(counts.rate, 6)}

    return {
        "corpus": corpus,
        "samples": sample_count,
        "predictedSamples": predicted_count,
        "coverage": round(predicted_count / sample_count, 6) if sample_count else 0.0,
        "exactMatchRate": round(exact_matches / sample_count, 6) if sample_count else 0.0,
        "wer": serialize_counts(word_total),
        "cer": serialize_counts(char_total),
        "groups": [
            {
                "dialect": dialect,
                "environment": environment,
                "samples": count,
                "wer": serialize_counts(words),
                "cer": serialize_counts(chars),
            }
            for (dialect, environment), (words, chars, count) in sorted(groups.items())
        ],
    }
