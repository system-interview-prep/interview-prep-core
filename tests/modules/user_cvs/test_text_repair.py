"""Whitespace-only repair of torn extraction text, and the check that keeps it so."""

from __future__ import annotations

import json

import pytest

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.text_repair import (
    TextRepairer,
    needs_repair,
    repair_artifacts,
    tear_signals,
    verify_repair,
)

TORN = "Bachelor’s Degree in Information Technology (Major in Data Science)GP A: 3.25/4.00"
FIXED = "Bachelor’s Degree in Information Technology (Major in Data Science)\nGPA: 3.25/4.00"


class ScriptedClient:
    """Answers with whatever blocks it was given; records every call."""

    def __init__(self, answers: list[list[str]] | None = None, *, raises: bool = False) -> None:
        self.answers = list(answers or [])
        self.raises = raises
        self.calls: list[list[str]] = []

    def complete(self, *, instructions: str, input_text: str, output_schema: dict) -> str:
        self.calls.append(json.loads(input_text)["blocks"])
        if self.raises:
            raise ConnectionError("provider down")
        return json.dumps({"blocks": self.answers.pop(0)}, ensure_ascii=False)


# --- the invariant --------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "repaired"),
    [
        ("GP A: 3.25/4.00", "GPA: 3.25/4.00"),
        (TORN, FIXED),
        ("M U C T I E U", "MUC TIEU"),
        ("Python\n", "Python"),
        ("Tiếng Việt", "Tiếng  Việt"),
    ],
)
def test_a_whitespace_only_change_is_a_repair(original: str, repaired: str) -> None:
    assert verify_repair(original, repaired)


@pytest.mark.parametrize(
    ("original", "repaired"),
    [
        ("GP A: 3.25/4.00", "GPA: 3.52/4.00"),  # digits swapped
        ("GP A: 3.25/4.00", "GPA: 3.25/4.0"),  # a character dropped
        ("GP A: 3.25/4.00", "GPA: 3.25/4.00 (good)"),  # text added
        ("Tieng Viet", "Tiếng Việt"),  # accents "restored"
        ("Python", "python"),  # case changed
    ],
)
def test_any_other_change_is_rejected(original: str, repaired: str) -> None:
    assert not verify_repair(original, repaired)


# --- the gate -------------------------------------------------------------------


def test_a_torn_label_is_detected_in_an_otherwise_clean_document() -> None:
    """The document-level shred ratio stays near zero here; the label pattern must catch it."""
    text = "Skills: Python, SQL, Docker\n" * 20 + TORN
    signals = tear_signals(text)
    assert signals["short_token_ratio"] < 0.12
    assert signals["split_labels"] == 1
    assert signals["glued_lines"] == 1
    assert needs_repair(text)


def test_a_shredded_document_is_detected_by_its_short_tokens() -> None:
    assert needs_repair("M U C T I E U N G H E N G H I E P V A K Y N A N G")


def test_a_clean_document_is_not_sent_anywhere() -> None:
    assert not needs_repair("Bachelor of Information Technology\nGPA: 3.25/4.00\nSkills: Python, SQL")


# --- the repairer ---------------------------------------------------------------


def test_only_the_torn_block_changes_and_the_rest_stay_verbatim() -> None:
    client = ScriptedClient([["Skills", FIXED, "Python"]])
    outcome = TextRepairer(client).repair(["Skills", TORN, "Python"])

    assert outcome.blocks == ["Skills", FIXED, "Python"]
    assert (outcome.changed, outcome.rejected, outcome.calls) == (1, 0, 1)


def test_a_block_the_model_altered_is_kept_as_extracted() -> None:
    client = ScriptedClient([["Skills", "GPA: 3.52/4.00", "Python"]])
    outcome = TextRepairer(client).repair(["Skills", "GP A: 3.25/4.00", "Python"])

    assert outcome.blocks == ["Skills", "GP A: 3.25/4.00", "Python"]
    assert (outcome.changed, outcome.rejected) == (0, 1)


def test_a_reply_with_the_wrong_block_count_keeps_every_original() -> None:
    client = ScriptedClient([["Skills GPA: 3.25/4.00 Python"]])
    outcome = TextRepairer(client).repair(["Skills", "GP A: 3.25/4.00", "Python"])

    assert outcome.blocks == ["Skills", "GP A: 3.25/4.00", "Python"]
    assert outcome.rejected == 3


def test_a_transport_failure_keeps_every_original() -> None:
    outcome = TextRepairer(ScriptedClient(raises=True)).repair(["GP A: 3.25"])

    assert outcome.blocks == ["GP A: 3.25"]
    assert outcome.rejected == 1


def test_long_documents_go_in_batches_and_come_back_in_order() -> None:
    blocks = [f"block {index} GP A: 3.{index}" for index in range(6)]
    fixed = [b.replace("GP A", "GPA") for b in blocks]
    client = ScriptedClient([fixed[:3], fixed[3:]])
    outcome = TextRepairer(client, batch_characters=60).repair(blocks)

    assert outcome.blocks == fixed
    assert outcome.calls == 2
    assert [len(call) for call in client.calls] == [3, 3]


# --- the artifacts --------------------------------------------------------------


def _artifacts(*texts: str) -> DocumentArtifacts:
    return DocumentArtifacts(
        markdown="\n\n".join(texts),
        content_list=[
            {"type": "text", "text": text, "page_idx": 0, "bbox": [0, 10 * i, 50, 10 * i + 8]}
            for i, text in enumerate(texts)
        ],
        extractor_version="pdf_text-test",
    )


def test_repaired_blocks_keep_their_page_and_bounding_box() -> None:
    artifacts = _artifacts("Education", TORN, "Skills: Python")
    client = ScriptedClient([["Education", FIXED, "Skills: Python"]])

    repaired, outcome = repair_artifacts(artifacts, TextRepairer(client))

    assert outcome is not None and outcome.changed == 1
    assert repaired.content_list[1]["text"] == FIXED
    assert repaired.content_list[1]["bbox"] == [0, 10, 50, 18]
    assert repaired.content_list[0] == artifacts.content_list[0]
    assert repaired.extractor_version == artifacts.extractor_version


def test_a_clean_document_is_returned_untouched_without_a_call() -> None:
    artifacts = _artifacts("Education", "GPA: 3.25/4.00", "Skills: Python")
    client = ScriptedClient()

    repaired, outcome = repair_artifacts(artifacts, TextRepairer(client))

    assert repaired is artifacts
    assert outcome is None
    assert client.calls == []


def test_markdown_only_artifacts_are_repaired_paragraph_by_paragraph() -> None:
    artifacts = DocumentArtifacts(markdown=f"Education\n\n{TORN}", content_list=[])
    client = ScriptedClient([["Education", FIXED]])

    repaired, outcome = repair_artifacts(artifacts, TextRepairer(client))

    assert outcome is not None and outcome.changed == 1
    assert repaired.markdown == f"Education\n\n{FIXED}"
