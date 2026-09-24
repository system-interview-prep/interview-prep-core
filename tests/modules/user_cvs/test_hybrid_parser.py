import json

from src.modules.ai import ModelServiceError
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.hybrid import HybridResumeParser
from src.modules.user_cvs.parsing.domain.source import build_source_document


class FakeClient:
    enabled = True

    def __init__(self, output):
        self.output = output
        self.input_text = None

    async def generate(self, request):
        self.input_text = request.input_text
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _source():
    raw = (
        "Jane Doe\njane@example.com\nSummary\nBackend engineer building reliable APIs."
        "\nExperience\nPlatform Engineer at Acme, 2021 - Present"
    )
    return build_source_document(
        DocumentArtifacts(
            markdown=raw,
            content_list=[
                {"type": "text", "text": "Jane Doe"},
                {"type": "text", "text": "jane@example.com"},
                {"type": "title", "text": "Summary"},
                {"type": "text", "text": "Backend engineer building reliable APIs."},
                {"type": "title", "text": "Experience"},
                {"type": "text", "text": "Platform Engineer at Acme, 2021 - Present"},
            ],
        ),
        document_id="cv-hybrid-1",
        document_sha256="a" * 64,
    )


async def test_hybrid_resume_adds_only_uniquely_grounded_entities_and_redacts_pii() -> None:
    candidate = {
        "headline": {"value": "Backend Engineer", "quote": "Backend engineer building reliable APIs."},
        "summary": None,
        "employment": [
            {
                "jobTitle": "Platform Engineer",
                "organization": "Acme",
                "startDate": {"value": "2021", "precision": "year"},
                "endDate": None,
                "isCurrent": True,
                "responsibilities": [],
                "quote": "Platform Engineer at Acme, 2021 - Present",
            }
        ],
        "education": [],
        "projects": [],
        "certifications": [
            {"name": "Invented", "issuer": None, "credentialId": None, "quote": "not present"}
        ],
    }
    client = FakeClient(json.dumps(candidate))
    parsed = await HybridResumeParser(client=client).parse(_source(), extraction_version="test")

    assert parsed.resume.profile.headline == "Backend Engineer"
    assert any(item.job_title == "Platform Engineer" for item in parsed.resume.employment)
    assert not parsed.resume.certifications
    assert "jane@example.com" not in client.input_text
    assert len(client.input_text) == len(_source().text)
    assert any(warning.code == "llm_claim_rejected" for warning in parsed.resume.parsing.warnings)
    for evidence in parsed.resume.evidence:
        assert _source().text[evidence.char_start : evidence.char_end] == evidence.text


async def test_hybrid_resume_falls_back_without_losing_deterministic_result() -> None:
    parsed = await HybridResumeParser(client=FakeClient(ModelServiceError("offline"))).parse(
        _source(), extraction_version="test"
    )
    assert parsed.resume.parsing.parser_version == "hybrid-resume-v2"
    assert any(warning.code == "llm_fallback" for warning in parsed.resume.parsing.warnings)
    assert parsed.identity.emails[0].value == "jane@example.com"


async def test_hybrid_resume_falls_back_when_grounded_candidate_breaks_domain_invariants() -> None:
    candidate = {
        "headline": None,
        "summary": None,
        "employment": [
            {
                "jobTitle": "Senior Platform Engineer",
                "organization": "Acme",
                "startDate": {"value": "2025", "precision": "year"},
                "endDate": {"value": "2021", "precision": "year"},
                "isCurrent": False,
                "responsibilities": [],
                "quote": "Platform Engineer at Acme, 2021 - Present",
            }
        ],
        "education": [],
        "projects": [],
        "certifications": [],
    }
    parsed = await HybridResumeParser(client=FakeClient(json.dumps(candidate))).parse(
        _source(), extraction_version="test"
    )
    assert any(warning.code == "llm_fallback" for warning in parsed.resume.parsing.warnings)
