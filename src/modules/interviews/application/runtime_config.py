"""The interview settings this API process runs with, for the admin "Cài đặt" page.

Read-only: values come from ``.env`` (``env`` names the variable) or are fixed
in code (``env`` is None). Changing one means editing ``.env`` and restarting
the backend, worker and voice agent; the page says so.
"""

from __future__ import annotations

from typing import Any

from src.core.config import get_settings
from src.modules.interviews.core import interview_engine, interviewer_voice
from src.modules.interviews.planning.entry_gate import (
    DEFAULT_CHALLENGE_PERCENT,
    DEFAULT_LOW_FIT_PERCENT,
    DEFAULT_MIN_MATCH_PERCENT,
    entry_policy,
)


def item(key: str, value: Any, env: str | None = None, default: Any = None) -> dict[str, Any]:
    """One setting; ``changed`` is True when ``.env`` overrides the default."""
    return {
        "key": key,
        "value": value,
        "env": env,
        "default": default,
        "changed": env is not None and default is not None and value != default,
    }


def interview_config_groups() -> list[dict[str, Any]]:
    s = get_settings()
    policy = entry_policy()
    return [
        {
            "key": "entry",
            "items": [
                item(
                    "minMatchPercent",
                    policy.min_match_percent,
                    "INTERVIEW_MIN_MATCH_PERCENT",
                    DEFAULT_MIN_MATCH_PERCENT,
                ),
                item("blockNotEligible", policy.block_not_eligible, "INTERVIEW_BLOCK_NOT_ELIGIBLE", False),
                item("requireMatchScore", policy.require_match_score, "INTERVIEW_REQUIRE_MATCH_SCORE", False),
                item(
                    "lowFitPercent",
                    policy.low_fit_percent,
                    "INTERVIEW_LOW_FIT_PERCENT",
                    DEFAULT_LOW_FIT_PERCENT,
                ),
                item(
                    "challengePercent",
                    policy.challenge_percent,
                    "INTERVIEW_CHALLENGE_PERCENT",
                    DEFAULT_CHALLENGE_PERCENT,
                ),
            ],
        },
        {
            "key": "runtime",
            "items": [
                item("llmModel", s.llm_model, "LLM_MODEL"),
                item(
                    "aiRequestsPerMinute",
                    s.interview_ai_requests_per_minute,
                    "INTERVIEW_AI_REQUESTS_PER_MINUTE",
                    20,
                ),
                item(
                    "dynamicPlanner",
                    s.interview_dynamic_planner_enabled,
                    "INTERVIEW_DYNAMIC_PLANNER_ENABLED",
                    False,
                ),
                item("maxQnaQuestions", interview_engine.MAX_QNA_QUESTIONS),
                item("qnaBudgetSeconds", interview_engine.QNA_BUDGET_SECONDS),
            ],
        },
        {
            "key": "speech",
            "items": [
                item("speechV2", interviewer_voice.speech_v2_enabled(), "INTERVIEWER_SPEECH_V2", True),
                item("ackMode", interviewer_voice.ack_mode(), "INTERVIEWER_ACK_MODE", "free"),
                item("dialect", interviewer_voice.voice_dialect(), "INTERVIEWER_VOICE_DIALECT", "bac"),
                item("ackMaxWords", interviewer_voice.ACK_MAX_WORDS),
                item("probeMaxWords", interviewer_voice.PROBE_MAX_WORDS),
                item("clarifyMaxWords", interview_engine.CLARIFY_MAX_WORDS),
                item("qnaMaxWords", interviewer_voice.QNA_MAX_WORDS),
                item("maxNhePerTurn", interviewer_voice.MAX_NHE_PER_TURN),
                item("answerPromptMaxChars", interviewer_voice.ANSWER_PROMPT_MAX_CHARS),
            ],
        },
    ]
