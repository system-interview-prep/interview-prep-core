from src.modules.user_cvs.evaluation.runner import evaluate_cases
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser


async def test_cv_evaluation_reports_metrics_and_passes_explicit_gate() -> None:
    raw = "Skills\nPython\nEnglish: B2"
    cases = [
        {
            "case_id": "cv-eval-1",
            "raw_text": raw,
            "content_list": [
                {"type": "title", "text": "Skills"},
                {"type": "text", "text": "Python"},
                {"type": "text", "text": "English: B2"},
            ],
            "expected": {
                "skills": [{"concept": {"conceptId": "skill-python"}}],
                "employment": [],
                "education": [],
                "projects": [],
                "certifications": [],
                "languages": [{"code": "en", "level": "B2"}],
            },
        }
    ]
    report = await evaluate_cases(
        cases,
        DeterministicResumeParser(),
        quality_gates={"min_macro_f1": 1.0, "max_fallback_rate": 0.0},
        input_cost_per_million_tokens=1.0,
        output_cost_per_million_tokens=2.0,
    )
    assert report["summary"]["macro_f1"] == 1.0
    assert report["summary"]["evidence_valid_rate"] == 1.0
    assert report["summary"]["review_required_rate"] == 1.0
    assert report["summary"]["estimated_cost_usd"] > 0
    assert report["quality_gate"]["passed"] is True


async def test_cv_evaluation_fails_quality_gate_on_missing_gold_fact() -> None:
    cases = [
        {
            "case_id": "cv-eval-miss",
            "raw_text": "No structured sections",
            "expected": {"skills": [{"concept": {"conceptId": "skill-python"}}]},
        }
    ]
    report = await evaluate_cases(cases, DeterministicResumeParser(), quality_gates={"min_macro_f1": 0.99})
    assert report["quality_gate"]["passed"] is False
    assert report["quality_gate"]["checks"]["macro_f1"] is False
