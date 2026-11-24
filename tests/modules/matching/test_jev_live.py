"""Opt-in live smoke test for the TypeSafe/Jev API contract.

This test only runs when RUN_LIVE_JEV_TESTS=1. Secrets are read from the
GitHub Actions environment and are never committed to the repository.
"""

from __future__ import annotations

import os

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_JEV_TESTS", "").strip() != "1",
    reason="set RUN_LIVE_JEV_TESTS=1 to run the live Jev smoke test",
)


def test_typesafe_systemone_live_contract() -> None:
    api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        pytest.skip("TYPESAFE_API_KEY is required for live Jev testing")

    base_url = os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    model = os.getenv("TYPESAFE_DEFAULT_MODEL", "jev-latest").strip() or "jev-latest"
    timeout_seconds = float(os.getenv("JEV_TIMEOUT_SECONDS", "10"))

    payload = {
        "model": model,
        "state": {
            "requirement": "At least 3 years of Java experience",
            "current_status": "unknown",
            "candidate_evidence": [
                {
                    "evidence_id": "live-smoke-evidence",
                    "section": "experience",
                    "text": "Worked as a Java backend developer, but no employment dates are provided.",
                }
            ],
            "policy": (
                "Do not decide whether the requirement is met. Decide only whether "
                "candidate-provided factual evidence is needed next and what kind."
            ),
        },
        "questions": {
            "next_action": {
                "type": "choice",
                "instructions": "What is the safest next action?",
                "criteria": {
                    "ask_candidate": "A factual detail is missing and should be supplied by the candidate.",
                    "review_existing_evidence": "Existing evidence should be reviewed before asking the candidate.",
                    "no_safe_clarification": "Candidate clarification would not safely resolve the requirement.",
                },
            },
            "missing_dimension": {
                "type": "choice",
                "instructions": "Which factual evidence dimension is most important next?",
                "criteria": {
                    "duration": "How long the candidate performed or used something.",
                    "proficiency": "Depth or proficiency level for a skill.",
                    "scale": "System, workload, traffic, team, or operational scale.",
                    "responsibility": "The candidate's direct ownership or responsibility.",
                    "education": "Degree, field of study, or education status.",
                    "language_level": "Language proficiency or language-test level.",
                    "certification": "A required professional or technical certification.",
                    "experience_context": "Concrete project, production, domain, or hands-on context.",
                    "other": "Another factual dimension not covered above.",
                },
            },
        },
    }

    response = httpx.post(
        f"{base_url}/v1/systemone",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()

    answers = body.get("answers")
    assert isinstance(answers, dict)
    for key in ("next_action", "missing_dimension"):
        answer = answers.get(key)
        assert isinstance(answer, dict)
        assert answer.get("type") == "choice"
        assert isinstance(answer.get("choice"), str)
        confidence = answer.get("confidence")
        assert isinstance(confidence, (int, float))
        assert 0.0 <= float(confidence) <= 1.0
