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


async def test_hybrid_resume_rejects_description_fragment_as_project_name() -> None:
    source = build_source_document(
        DocumentArtifacts(
            markdown=(
                "Experience\nFounder & Fullstack Developer at IVORA 2025 - Present\n"
                "Projects\nOnline Bookstore System\nDescription: feedback\n"
                "IVORA — Online Wedding Invitation Platform"
            ),
            content_list=[
                {"type": "title", "text": "EXPERIENCE"},
                {"type": "text", "text": "Founder & Fullstack Developer at IVORA 2025 - Present"},
                {"type": "title", "text": "PROJECTS"},
                {"type": "text", "text": "Online Bookstore System"},
                {"type": "text", "text": "Description: feedback"},
                {"type": "text", "text": "feedback"},
                {"type": "text", "text": "IVORA — Online Wedding Invitation Platform"},
                {"type": "text", "text": "– Architected a multi-stage RAG system."},
                {"type": "title", "text": "Python, PyTorch, YOLOv8, OpenCV, EasyOCR"},
            ],
        ),
        document_id="cv-project-fragment",
        document_sha256="c" * 64,
    )
    candidate = {
        "headline": None,
        "summary": None,
        "employment": [],
        "education": [],
        "projects": [
            {"name": "feedback", "description": None, "quote": "feedback"},
            {
                "name": "IVORA — Online Wedding Invitation Platform",
                "description": None,
                "quote": "IVORA — Online Wedding Invitation Platform",
            },
            {
                "name": "Architected a multi-stage RAG system",
                "description": None,
                "quote": "Architected a multi-stage RAG system",
            },
            {
                "name": "Python, PyTorch, YOLOv8",
                "description": None,
                "quote": "Python, PyTorch, YOLOv8, OpenCV, EasyOCR",
            },
            {"name": "Advanced Multi-Stage RAG Pipeline", "description": None, "quote": "feedback"},
        ],
        "certifications": [],
    }

    parsed = await HybridResumeParser(client=FakeClient(json.dumps(candidate))).parse(
        source, extraction_version="test"
    )

    assert [project.name for project in parsed.resume.projects] == ["Online Bookstore System"]


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


async def test_llm_reading_wins_a_clash_with_the_deterministic_baseline() -> None:
    """The baseline used to shadow the LLM whenever their keys collided.

    The deterministic pass names the project from the heading line, dates and
    all; the LLM names it from the title alone. Both are grounded, so the
    merge has to keep one -- and it used to keep the baseline's.
    """
    raw = (
        "Jane Doe\nProjects\nHComic - Comic Reading Platform 07/2026 - Present\n"
        "Role: Fullstack Developer"
    )
    source = build_source_document(
        DocumentArtifacts(
            markdown=raw,
            content_list=[
                {"type": "text", "text": "Jane Doe"},
                {"type": "title", "text": "Projects"},
                {"type": "title", "text": "HComic - Comic Reading Platform 07/2026 - Present"},
                {"type": "text", "text": "Role: Fullstack Developer"},
            ],
        ),
        document_id="cv-hybrid-clash",
        document_sha256="a" * 64,
    )
    payload = json.dumps(
        {
            "projects": [
                {
                    "name": "HComic - Comic Reading Platform",
                    "description": "Comic reading platform.",
                    "quote": "HComic - Comic Reading Platform 07/2026 - Present",
                }
            ]
        }
    )

    parsed = await HybridResumeParser(client=FakeClient(payload)).parse(
        source, extraction_version="test"
    )
    names = [item.name for item in parsed.resume.projects]

    # One project, named the way the LLM read it.
    assert names == ["HComic - Comic Reading Platform"]


async def test_deterministic_baseline_still_fills_what_the_llm_missed() -> None:
    """Inverting the precedence must not drop baseline-only entries."""
    raw = (
        "Jane Doe\nProjects\nHComic - Comic Reading Platform 07/2026 - Present\n"
        "URL Shortener Service 04/2026 - 07/2026"
    )
    source = build_source_document(
        DocumentArtifacts(
            markdown=raw,
            content_list=[
                {"type": "text", "text": "Jane Doe"},
                {"type": "title", "text": "Projects"},
                {"type": "title", "text": "HComic - Comic Reading Platform 07/2026 - Present"},
                {"type": "title", "text": "URL Shortener Service 04/2026 - 07/2026"},
            ],
        ),
        document_id="cv-hybrid-gapfill",
        document_sha256="a" * 64,
    )
    payload = json.dumps(
        {
            "projects": [
                {
                    "name": "HComic - Comic Reading Platform",
                    "description": "Comic reading platform.",
                    "quote": "HComic - Comic Reading Platform 07/2026 - Present",
                }
            ]
        }
    )

    parsed = await HybridResumeParser(client=FakeClient(payload)).parse(
        source, extraction_version="test"
    )
    names = [item.name for item in parsed.resume.projects]

    assert names[0] == "HComic - Comic Reading Platform"
    assert any("URL Shortener Service" in name for name in names)
    assert len(names) == 2
