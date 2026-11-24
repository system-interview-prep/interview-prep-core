"""Whitespace-only repair of extracted CV text, by a model that cannot be trusted.

Text-layer extraction tears words apart and glues lines together: "GPA" comes
out as "GP A", "MỤC TIÊU" as "MU C TIEU", and a degree line runs straight into
the GPA that followed it on the page.  Nothing downstream survives that - the
parser leaves the GPA empty, the regex that reads it misses, the judge is not
asked - so the document is repaired once, here, before the source document and
its evidence offsets are built.

The model is allowed exactly one operation: adding or removing whitespace.
That makes its work checkable by machine.  Strip every whitespace character
from the original and from the answer; the two must be identical, character
for character.  An answer that fails the check is discarded block by block,
so the model can neither invent, drop nor alter a single letter.  Blocks keep
their page and bounding box because they are repaired in place.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts

logger = logging.getLogger(__name__)

REPAIR_VERSION = "repair-v1"

# Share of alphabetic tokens that are a single letter, above which the document
# as a whole reads as shredded ("S o f t w a r e").  Ordinary prose, initials
# and bullet letters included, sits well below this.
SHORT_TOKEN_RATIO = 0.12

# A label torn in two ("GP A:", "IEL TS 6.5") or a line glued to the previous
# one ("Science)GPA:", "2027GPA").  Either is enough to ask for a repair.
_SPLIT_LABEL = re.compile(r"\b[A-Z]{1,3} [A-Z]{1,3}\b\s*[:\d]")
_GLUED_LINE = re.compile(r"[\)\]][A-Za-z]|[a-z0-9][A-Z]{2,}[A-Z ]{0,4}:")

_WHITESPACE = re.compile(r"\s+")
_ALPHA_TOKEN = re.compile(r"[^\W\d_]+")

# The keys, in the order the source builder reads them, under which an
# extraction block keeps its text.  Only a plain string is repaired; a table
# body or list is left alone rather than reshaped.
_TEXT_KEYS = ("text", "content", "title_content", "paragraph_content", "code_content")

_INSTRUCTIONS = """You repair text extracted from a CV (resume) PDF. The extractor tore some \
words apart with spaces and glued some lines together.

You may do exactly one thing: add or remove whitespace (spaces, line breaks). \
You must not add, remove, reorder or change any other character - not a letter, \
digit, accent, punctuation mark or symbol. Keep every block in the same order.

Fix only what is clearly broken:
- join a word or label split by stray spaces: "GP A: 3.25" -> "GPA: 3.25", \
"MU C TIEU NGH E NGHI E P" -> "MUC TIEU NGHE NGHIEP", "Py thon" -> "Python";
- put a line break where two lines were glued: "Data Science)GPA: 3.25/4.00" \
-> "Data Science)\\nGPA: 3.25/4.00";
- leave correct text exactly as it is.

Return the blocks as a JSON object {"blocks": [...]} with the same number of \
blocks, each the repaired text of the block at the same position."""

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"blocks": {"type": "array", "items": {"type": "string"}}},
    "required": ["blocks"],
    "additionalProperties": False,
}


class RepairClient(Protocol):
    def complete(self, *, instructions: str, input_text: str, output_schema: dict[str, Any]) -> str: ...


def collapse_whitespace(text: str) -> str:
    """The characters of ``text`` with every whitespace removed, NFC-normalised."""
    return _WHITESPACE.sub("", unicodedata.normalize("NFC", text))


def verify_repair(original: str, repaired: str) -> bool:
    """True when ``repaired`` differs from ``original`` in whitespace only."""
    return collapse_whitespace(original) == collapse_whitespace(repaired)


def tear_signals(text: str) -> dict[str, float | int]:
    # Single letters only: Vietnamese is full of two-letter words ("và", "có",
    # "là"), and a short document has too few tokens for a ratio to mean much.
    tokens = _ALPHA_TOKEN.findall(text)
    short = sum(1 for token in tokens if len(token) == 1)
    return {
        "short_token_ratio": round(short / len(tokens), 4) if len(tokens) >= 20 else 0.0,
        "split_labels": len(_SPLIT_LABEL.findall(text)),
        "glued_lines": len(_GLUED_LINE.findall(text)),
    }


def needs_repair(text: str) -> bool:
    """Whether the text shows any of the tears the repair exists for.

    A clean document is never sent to the model: it costs a call and gives the
    model a chance to touch a CV that already read correctly.
    """
    signals = tear_signals(text)
    return (
        signals["short_token_ratio"] >= SHORT_TOKEN_RATIO
        or signals["split_labels"] > 0
        or signals["glued_lines"] > 0
    )


@dataclass(frozen=True)
class RepairOutcome:
    blocks: list[str]
    changed: int = 0
    rejected: int = 0
    calls: int = 0
    signals: dict[str, float | int] = field(default_factory=dict)


class TextRepairer:
    """Repair a list of text blocks, keeping every block the model did not repair verbatim."""

    def __init__(self, client: RepairClient, *, batch_characters: int = 6_000) -> None:
        self._client = client
        self._batch_characters = batch_characters

    def repair(self, blocks: list[str]) -> RepairOutcome:
        out: list[str] = []
        changed = rejected = calls = 0
        for batch in self._batches(blocks):
            answer = self._ask(batch)
            calls += 1
            if answer is None or len(answer) != len(batch):
                # A reply of the wrong shape says nothing about any one block.
                out.extend(batch)
                rejected += len(batch)
                continue
            for original, candidate in zip(batch, answer, strict=True):
                if not isinstance(candidate, str) or not verify_repair(original, candidate):
                    out.append(original)
                    rejected += 1
                elif candidate.strip() != original.strip():
                    out.append(candidate.strip())
                    changed += 1
                else:
                    out.append(original)
        return RepairOutcome(out, changed=changed, rejected=rejected, calls=calls)

    def _batches(self, blocks: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        size = 0
        for block in blocks:
            if current and size + len(block) > self._batch_characters:
                batches.append(current)
                current, size = [], 0
            current.append(block)
            size += len(block)
        if current:
            batches.append(current)
        return batches

    def _ask(self, batch: list[str]) -> list[Any] | None:
        try:
            raw = self._client.complete(
                instructions=_INSTRUCTIONS,
                input_text=json.dumps({"blocks": batch}, ensure_ascii=False),
                output_schema=_OUTPUT_SCHEMA,
            )
            answer = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - a failed repair is a kept original
            logger.warning("text repair call failed: %s", type(exc).__name__)
            return None
        blocks = answer.get("blocks") if isinstance(answer, dict) else None
        return blocks if isinstance(blocks, list) else None


def _block_text_key(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in _TEXT_KEYS:
        if isinstance(item.get(key), str) and item[key].strip():
            return key
    return None


def repair_artifacts(
    artifacts: DocumentArtifacts, repairer: TextRepairer
) -> tuple[DocumentArtifacts, RepairOutcome | None]:
    """Repair the artifacts' text blocks in place; ``None`` when no tear was found.

    With a content list, each block's own text field is replaced so page and
    bounding box survive.  Without one, the markdown is repaired paragraph by
    paragraph, which is how the source builder splits it.
    """
    if artifacts.content_list:
        located = [
            (index, key)
            for index, item in enumerate(artifacts.content_list)
            if (key := _block_text_key(item))
        ]
        texts = [artifacts.content_list[index][key] for index, key in located]
    else:
        located = []
        texts = [p for p in re.split(r"\n\s*\n", artifacts.markdown or "") if p.strip()]
    joined = "\n".join(texts)
    if not texts or not needs_repair(joined):
        return artifacts, None
    outcome = replace(repairer.repair(texts), signals=tear_signals(joined))
    if not outcome.changed:
        return artifacts, outcome
    if located:
        content_list = list(artifacts.content_list)
        for (index, key), text in zip(located, outcome.blocks, strict=True):
            content_list[index] = {**content_list[index], key: text}
        return replace(artifacts, content_list=content_list), outcome
    return replace(artifacts, markdown="\n\n".join(outcome.blocks)), outcome
