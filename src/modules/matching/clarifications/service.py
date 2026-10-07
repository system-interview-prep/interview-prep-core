"""Question selection and generation orchestration after the initial match."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from functools import lru_cache

from fastapi.concurrency import run_in_threadpool

from src.core.config import get_settings
from src.core.trace_logging import trace_event

from src.modules.matching.application.facade import get_matching_facade
from src.modules.matching.clarifications.ambiguity import (
    AmbiguityAnalyzer,
    JevAmbiguityAnalyzer,
    build_ambiguity_analyzer_from_env,
    build_clarification_plans,
    build_clarification_requests_from_plans,
)
from src.modules.matching.clarifications.models import (
    ClarificationCandidate,
    MatchClarificationAnalysis,
)
from src.modules.matching.clarifications.question_generation import (
    ClarificationPlan,
    ClarificationQuestionService,
    build_clarification_question_service_from_env,
)
from src.modules.matching.domain.schemas import MatchRequest, MatchResult

_CLARIFICATION_TOKEN_TTL_SECONDS = 300


def _clarification_token_secret() -> bytes:
    return get_settings().jwt_secret.encode("utf-8")


def _encode_clarification_plans(plans: list[ClarificationPlan]) -> str:
    payload = {
        "expires_at": int(time.time()) + _CLARIFICATION_TOKEN_TTL_SECONDS,
        # Do not put CV evidence text in a browser-visible token. The backend
        # reconstructs the evidence from the canonical resume after verifying
        # this signed Jev decision.
        "plans": [
            {
                "requirement_id": plan.requirement_id,
                "missing_dimension": plan.missing_dimension,
                "confidence": plan.confidence,
            }
            for plan in plans
        ],
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(
        _clarification_token_secret(), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def _decode_clarification_plans(token: str) -> list[dict[str, object]] | None:
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(
            _clarification_token_secret(), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + ("=" * (-len(encoded) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if int(payload["expires_at"]) < int(time.time()):
            return None
        plans = payload["plans"]
        if not isinstance(plans, list):
            return None
        if not all(isinstance(item, dict) for item in plans):
            return None
        return plans
    except (binascii.Error, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _plans_from_preparation_token(
    token: str,
    requirement_ids: set[str],
    result: MatchResult,
    payload: MatchRequest,
) -> list[ClarificationPlan]:
    approved_candidates = _decode_clarification_plans(token)
    if approved_candidates is None:
        trace_event("clarification", "preparation_token_rejected", reason="invalid_or_expired")
        return []

    candidate_by_id = {
        str(item.get("requirement_id")): item for item in approved_candidates
    }
    if not requirement_ids.issubset(candidate_by_id):
        trace_event(
            "clarification",
            "preparation_token_rejected",
            reason="requirement_not_prepared",
            requested_requirement_ids=sorted(requirement_ids),
        )
        return []

    unknown_ids = {
        item.requirement_id
        for item in result.requirement_results
        if item.status == "unknown"
    }
    requirements_by_id = {item.requirement_id: item for item in payload.job.requirements}
    selected: list[ClarificationPlan] = []
    for requirement_id in sorted(requirement_ids):
        if requirement_id not in unknown_ids:
            continue
        requirement = requirements_by_id.get(requirement_id)
        candidate = candidate_by_id[requirement_id]
        if requirement is None:
            continue
        requirement_text = JevAmbiguityAnalyzer._requirement_text(requirement)
        evidence = JevAmbiguityAnalyzer._select_evidence(requirement_text, payload.resume)
        selected.append(
            ClarificationPlan(
                requirementId=requirement_id,
                missingDimension=str(candidate["missing_dimension"]),
                confidence=float(candidate["confidence"]),
                evidenceRefs=[item.evidence_id for item in evidence],
                requirementText=requirement_text,
                subjectTerms=JevAmbiguityAnalyzer._subject_terms(requirement),
                evidenceTexts=[item.text for item in evidence],
            )
        )
    if len(selected) != len(requirement_ids):
        trace_event(
            "clarification",
            "preparation_token_rejected",
            reason="plan_no_longer_unknown",
            requested_requirement_ids=sorted(requirement_ids),
        )
    trace_event(
        "clarification",
        "preparation_token_accepted",
        approved_plan_count=len(selected),
        requested_requirement_ids=sorted(requirement_ids),
    )
    return selected


@lru_cache
def get_ambiguity_analyzer() -> AmbiguityAnalyzer:
    """Reuse the analyzer/client across requests, matching other provider factories."""

    return build_ambiguity_analyzer_from_env()


@lru_cache
def get_clarification_question_service() -> ClarificationQuestionService | None:
    return build_clarification_question_service_from_env()


async def run_match_with_clarifications(payload: MatchRequest) -> MatchClarificationAnalysis:
    """Run matching first, then analyze only the requirements left ``unknown``.

    The core result is created before the Jev call. Clarification analysis reads
    that result but never mutates it, so Jev cannot change requirement status,
    eligibility, suitability score, score provenance, or fit band.
    """

    trace_event("clarification", "one_shot_started")
    result = await run_in_threadpool(get_matching_facade().match, payload)
    plans = await run_in_threadpool(
        build_clarification_plans,
        payload,
        result,
        get_ambiguity_analyzer(),
    )
    requests = await run_in_threadpool(
        build_clarification_requests_from_plans,
        plans,
        get_clarification_question_service(),
    )
    trace_event(
        "clarification",
        "one_shot_completed",
        unknown_count=sum(item.status == "unknown" for item in result.requirement_results),
        approved_plan_count=len(plans),
        generated_question_count=len(requests),
        outcome=("ready" if requests else "unavailable" if plans else "not_needed"),
    )
    return MatchClarificationAnalysis(
        matchResult=result,
        clarificationRequests=requests,
        clarificationStatus=("ready" if requests else "unavailable" if plans else "not_needed"),
        clarificationCandidates=[
            ClarificationCandidate(
                requirementId=plan.requirement_id,
                missingDimension=plan.missing_dimension,
                confidence=plan.confidence,
            )
            for plan in plans
        ],
    )


async def prepare_match_clarifications(payload: MatchRequest) -> MatchClarificationAnalysis:
    """Run matching and Jev only; do not start question generation yet."""

    trace_event("clarification", "prepare_started")
    result = await run_in_threadpool(get_matching_facade().match, payload)
    plans = await run_in_threadpool(
        build_clarification_plans,
        payload,
        result,
        get_ambiguity_analyzer(),
    )
    trace_event(
        "clarification",
        "prepare_completed",
        unknown_count=sum(item.status == "unknown" for item in result.requirement_results),
        approved_plan_count=len(plans),
        outcome="ready" if plans else "not_needed",
    )
    return MatchClarificationAnalysis(
        matchResult=result,
        clarificationStatus="ready" if plans else "not_needed",
        clarificationCandidates=[
            ClarificationCandidate(
                requirementId=plan.requirement_id,
                missingDimension=plan.missing_dimension,
                confidence=plan.confidence,
            )
            for plan in plans
        ],
        clarificationToken=_encode_clarification_plans(plans) if plans else None,
    )


async def generate_match_clarifications(
    payload: MatchRequest,
    requirement_ids: set[str],
    clarification_token: str | None = None,
) -> MatchClarificationAnalysis:
    """Generate from the signed Jev decision returned by the preparation phase."""

    trace_event(
        "clarification",
        "question_phase_started",
        requested_requirement_ids=sorted(requirement_ids),
    )
    result = await run_in_threadpool(get_matching_facade().match, payload)
    if clarification_token:
        plans = _plans_from_preparation_token(clarification_token, requirement_ids, result, payload)
    else:
        # Backward compatibility for non-browser callers that still use the
        # old two-argument service API. The browser flow always sends the
        # signed preparation token and therefore does not call Jev twice.
        trace_event("clarification", "legacy_question_phase_without_token")
        plans = await run_in_threadpool(
            build_clarification_plans,
            payload,
            result,
            get_ambiguity_analyzer(),
            requirement_ids,
        )
    requests = await run_in_threadpool(
        build_clarification_requests_from_plans,
        plans,
        get_clarification_question_service(),
    )
    trace_event(
        "clarification",
        "question_phase_completed",
        requested_requirement_ids=sorted(requirement_ids),
        approved_plan_count=len(plans),
        generated_question_count=len(requests),
        outcome=("ready" if requests else "unavailable" if plans else "not_needed"),
    )
    return MatchClarificationAnalysis(
        matchResult=result,
        clarificationRequests=requests,
        clarificationStatus=("ready" if requests else "unavailable" if plans else "not_needed"),
        clarificationCandidates=[
            ClarificationCandidate(
                requirementId=plan.requirement_id,
                missingDimension=plan.missing_dimension,
                confidence=plan.confidence,
            )
            for plan in plans
        ],
    )


