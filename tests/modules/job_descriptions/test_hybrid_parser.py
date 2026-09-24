import json
from types import SimpleNamespace

import pytest

from src.modules.ai import ModelServiceError
from src.modules.job_descriptions.parsing.hybrid import HybridJobDescriptionParser
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document


class FakeModelClient:
    def __init__(self, output: str | Exception) -> None:
        self.output = output
        self.enabled = True

    async def generate(self, request):
        del request
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _source():
    return build_source_document(
        DocumentArtifacts(
            markdown="fallback",
            content_list=[
                {"type": "text", "text": "Description", "page_idx": 0},
                {"type": "text", "text": "We are hiring a Backend Engineer.", "page_idx": 0},
                {"type": "text", "text": "Requirements", "page_idx": 0},
                {"type": "text", "text": "Python experience is required.", "page_idx": 0},
                {"type": "text", "text": "Responsibilities", "page_idx": 0},
                {"type": "text", "text": "Build reliable APIs.", "page_idx": 0},
            ],
        ),
        document_id="hybrid-jd-1",
        document_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_hybrid_parser_accepts_only_exactly_grounded_claims() -> None:
    candidate = {
        "jobTitle": {"value": "Backend Engineer", "quote": "Backend Engineer"},
        "responsibilities": [{"value": "Build APIs", "quote": "Build reliable APIs."}],
        "requirements": [
            {
                "value": "Python",
                "quote": "Python experience is required.",
                "kind": "skill",
                    "priority": "must_have"
            }
        ],
        "benefits": [{"value": "Invented benefit", "quote": "Not present in source"}],
    }
    parsed = await HybridJobDescriptionParser(client=FakeModelClient(json.dumps(candidate))).parse(
        _source(), extraction_version="test"
    )

    assert parsed.job_title == "Backend Engineer"
    assert any(item.text == "Build APIs" for item in parsed.responsibilities)
    assert any(item.raw_label == "Python" and item.concept for item in parsed.requirements)
    assert not any(item.text == "Invented benefit" for item in parsed.benefits)
    evidence = {item.evidence_id: item for item in parsed.evidence}
    assert all(
        ref in evidence
        for item in [*parsed.responsibilities, *parsed.requirements, *parsed.benefits]
        for ref in item.evidence_refs
    )
    assert any(warning.code == "llm_claim_rejected" for warning in parsed.parsing.warnings)


@pytest.mark.asyncio
async def test_hybrid_parser_falls_back_to_deterministic_output() -> None:
    parsed = await HybridJobDescriptionParser(client=FakeModelClient(ModelServiceError("offline"))).parse(
        _source(), extraction_version="test"
    )

    assert parsed.parsing.parser_version == "hybrid-jd-v2"
    assert any(warning.code == "llm_fallback" for warning in parsed.parsing.warnings)


@pytest.mark.asyncio
async def test_hybrid_parser_uses_openai_facade_by_default(monkeypatch) -> None:
    candidate = {
        "jobTitle": {"value": "Backend Engineer", "quote": "Backend Engineer"},
        "responsibilities": [],
        "requirements": [],
        "benefits": [],
    }
    calls = []

    async def fake_generate_text(**kwargs):
        calls.append(kwargs)
        return json.dumps(candidate)

    monkeypatch.setattr(
        "src.modules.job_descriptions.parsing.hybrid.get_settings",
        lambda: SimpleNamespace(openai_api_key="test-key", jd_parser_max_output_tokens=768),
    )
    monkeypatch.setattr("src.modules.job_descriptions.parsing.hybrid.generate_text", fake_generate_text)

    parsed = await HybridJobDescriptionParser().parse(_source(), extraction_version="test")

    assert parsed.job_title == "Backend Engineer"
    assert calls and calls[0]["max_output_tokens"] == 768


@pytest.mark.asyncio
async def test_hybrid_parser_handles_markdown_fences_and_whitespace_variance() -> None:
    candidate_json = """```json
    {
        "jobTitle": {"value": "Backend Engineer", "quote": "Backend Engineer"},
        "responsibilities": [
            {"value": "Build APIs", "quote": "Build\\nreliable APIs."}
        ],
        "requirements": [
            {
                "value": "Python",
                "quote": "Python   experience is required.",
                "kind": "skill",
                "priority": "must_have"
            }
        ],
        "benefits": [],
        "extra_unknown_field": "should_be_ignored"
    }
    ```"""
    parsed = await HybridJobDescriptionParser(client=FakeModelClient(candidate_json)).parse(
        _source(), extraction_version="test"
    )

    assert parsed.job_title == "Backend Engineer"
    assert any(item.text == "Build APIs" for item in parsed.responsibilities)
    assert any(item.raw_label == "Python" for item in parsed.requirements)
    evidence = {item.evidence_id: item for item in parsed.evidence}
    assert all(
        ref in evidence
        for item in [*parsed.responsibilities, *parsed.requirements]
        for ref in item.evidence_refs
    )


@pytest.mark.asyncio
async def test_hybrid_parser_uses_one_canonical_requirement_source() -> None:
    candidate = {
        "jobTitle": {"value": "Backend Engineer", "quote": "Backend Engineer"},
        "responsibilities": [],
        "requirements": [
            {
                "value": "Required Python experience",
                "quote": "Python experience is required.",
                "kind": "skill",
                "priority": "must_have",
            }
        ],
        "benefits": [],
    }

    parsed = await HybridJobDescriptionParser(client=FakeModelClient(json.dumps(candidate))).parse(
        _source(), extraction_version="test"
    )

    assert [item.raw_label for item in parsed.requirements] == ["Required Python experience"]
    assert len(parsed.requirements) == 1
    assert not parsed.requirements[0].requirement_id.startswith("req-llm-")
    assert len(parsed.requirements[0].evidence_refs) >= 1


@pytest.mark.asyncio
async def test_hybrid_parser_preserves_preferred_when_reconciling_same_source_requirement() -> None:
    candidate = {
        "jobTitle": {"value": "Backend Engineer", "quote": "Backend Engineer"},
        "responsibilities": [],
        "requirements": [
            {
                "value": "Python experience is a plus",
                "quote": "Python experience is required.",
                "kind": "skill",
                "priority": "preferred",
            }
        ],
        "benefits": [],
    }

    parsed = await HybridJobDescriptionParser(client=FakeModelClient(json.dumps(candidate))).parse(
        _source(), extraction_version="test"
    )

    assert len(parsed.requirements) == 1
    assert parsed.requirements[0].priority == "preferred"
