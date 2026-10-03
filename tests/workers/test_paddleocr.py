from types import SimpleNamespace

import pytest

from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.workers.paddleocr import normalize_paddle_results


def test_normalize_paddle_pages_preserves_reading_order_bbox_and_tables() -> None:
    pages = [
        SimpleNamespace(
            json={
                "page_index": 0,
                "parsing_res_list": [
                    {
                        "block_label": "title",
                        "block_content": "Experience",
                        "block_bbox": [[0, 0], [100, 0], [100, 20], [0, 20]],
                        "block_order": 0,
                    },
                    {
                        "block_label": "table",
                        "block_content": "<table><tr><td>Python</td><td>4 years</td></tr></table>",
                        "block_bbox": [0, 30, 200, 100],
                        "block_order": 1,
                    },
                ],
            }
        ),
        SimpleNamespace(
            json={
                "page_index": 1,
                "parsing_res_list": [
                    {
                        "block_label": "text",
                        "block_content": "Education",
                        "block_bbox": [0, 0, 100, 20],
                        "block_order": 0,
                    }
                ],
            }
        ),
    ]

    artifacts = normalize_paddle_results(pages, extractor_version="3.3.0")

    assert artifacts.extractor_name == "paddleocr"
    assert artifacts.extractor_version == "3.3.0"
    assert artifacts.content_list[0]["text"] == "Experience"
    assert artifacts.content_list[0]["bbox"] == [0.0, 0.0, 100.0, 20.0]
    assert artifacts.content_list[1]["type"] == "table"
    assert artifacts.content_list[1]["page_idx"] == 0
    assert artifacts.content_list[2]["page_idx"] == 1
    assert "Python" in artifacts.markdown
    assert "Education" in artifacts.markdown


def test_normalize_paddle_results_falls_back_to_plain_ocr_lines() -> None:
    artifacts = normalize_paddle_results(
        [
            {
                "page_index": 2,
                "overall_ocr_res": {
                    "rec_texts": ["Skills", "Python"],
                    "rec_polys": [
                        [[10, 10], [50, 10], [50, 20], [10, 20]],
                        [[10, 30], [70, 30], [70, 40], [10, 40]],
                    ],
                },
            }
        ],
        extractor_version="3.3.0",
    )

    assert [block["text"] for block in artifacts.content_list] == ["Skills", "Python"]
    assert artifacts.content_list[0]["bbox"] == [10.0, 10.0, 50.0, 20.0]
    assert artifacts.content_list[0]["page_idx"] == 2
    assert artifacts.markdown == "Skills\nPython"


def test_normalize_paddle_results_rejects_empty_output() -> None:
    with pytest.raises(RuntimeError, match="no document text"):
        normalize_paddle_results([], extractor_version="3.3.0")


def test_normalized_paddle_artifacts_feed_existing_source_and_evidence_layout() -> None:
    artifacts = normalize_paddle_results(
        [
            {
                "page_index": 0,
                "parsing_res_list": [
                    {
                        "block_label": "title",
                        "block_content": "Skills",
                        "block_bbox": [10, 20, 100, 40],
                        "block_order": 0,
                    },
                    {
                        "block_label": "text",
                        "block_content": "Python",
                        "block_bbox": [10, 45, 100, 65],
                        "block_order": 1,
                    },
                ],
            }
        ],
        extractor_version="3.3.0",
    )

    source = build_source_document(artifacts, document_id="cv-1", document_sha256="a" * 64)

    assert source.text == "Skills\nPython"
    assert source.blocks[1].page == 1
    assert source.blocks[1].reading_order == 1
    assert source.blocks[1].bounding_box == (10.0, 45.0, 100.0, 65.0)
