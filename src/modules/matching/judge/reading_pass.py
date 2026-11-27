"""Build the ``ReadingSet`` for one match, before any evaluator runs.

For each unresolved requirement the pass picks a bounded set of candidate
spans - the document spans ranked by BM25 against the requirement, plus any
answer the candidate wrote for this requirement - and asks the judge.  The
answer is stored by requirement id; the evaluators read it, they never call
the model.

With the judge disabled this returns ``None`` and nothing changes.  Enabled,
a grounded ``satisfies`` may raise a verdict (see ``judge_evaluators``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from src.core.config import Settings, get_settings
from src.core.trace_logging import trace_event
from src.modules.ai.facade import OpenAIStructuredClient
from src.modules.matching.domain.schemas import CanonicalJob, UnresolvedRequirement
from src.modules.matching.evaluation.legacy_rules import select_evaluator
from src.modules.matching.judge.cache import CachingJudgeClient
from src.modules.matching.judge.grounded_judge import GroundedJudge, OpenAIJudgeClient
from src.modules.matching.judge.numeric_judge import NumericJudge
from src.modules.matching.judge.readings import NUMERIC_PROMPT_VERSION, PROMPT_VERSION, ReadingSet
from src.modules.matching.retrieval.bm25 import bm25_similarity
from src.modules.user_cvs.schemas import CanonicalResume, EvidenceSpan

logger = logging.getLogger(__name__)

# Sections the Jev gate already keeps away from the model.
_PRIVATE_SECTIONS = {"identity", "contact", "personal", "pii"}


def requirement_text(requirement: UnresolvedRequirement) -> str:
    labels = [concept.label for concept in requirement.atomic_concepts if concept.label]
    if labels:
        return f"{requirement.raw_label} ({', '.join(labels)})"
    return requirement.raw_label


def select_candidates(
    requirement: UnresolvedRequirement,
    resume: CanonicalResume,
    *,
    top_k: int,
    sections: tuple[str, ...] = (),
) -> list[EvidenceSpan]:
    """The spans the judge may cite: ranked document spans plus the requirement's own answers.

    The answer the candidate wrote for this requirement is always included,
    whatever its wording - its relevance is established by the id it carries,
    not by word overlap, the same rule ``legacy_rules`` applies.

    ``sections`` names CV sections whose spans are included whole, ahead of
    the ranking.  A GPA lives in the education section whether or not the
    extractor left the letters "GPA" together; ranking by word overlap would
    not find "GP A: 3.25/4.00" for "GPA từ 3.2", and the judge cannot read
    what it was not handed.
    """

    query = requirement_text(requirement)
    document = [
        span
        for span in resume.evidence
        if span.evidence_source == "document" and span.section not in _PRIVATE_SECTIONS
    ]
    preferred = [span for span in document if span.section in sections]
    ranked = sorted(
        (span for span in document if span.section not in sections),
        key=lambda span: (-bm25_similarity(query, span.text), span.char_start, span.evidence_id),
    )
    answers = [
        span
        for span in resume.evidence
        if span.evidence_source == "candidate_self_report"
        and span.source_requirement_id == requirement.requirement_id
    ]
    return [*answers, *preferred, *ranked[:top_k]]


def default_judge(settings: Settings | None = None) -> GroundedJudge | None:
    settings = settings or get_settings()
    if not settings.openai_api_key:
        return None
    model = settings.matching_judge_model or settings.llm_model
    try:
        client = OpenAIJudgeClient(
            api_key=settings.openai_api_key,
            model=model,
            max_output_tokens=settings.matching_judge_max_output_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Matching judge unavailable: %s", type(exc).__name__)
        return None
    return GroundedJudge(
        CachingJudgeClient(client, model=model, prompt_version=PROMPT_VERSION),
        prompt_version=PROMPT_VERSION,
    )


# Evaluators whose question is "what number does the CV state".  These go to
# the numeric judge; every other unresolved requirement goes to entailment.
# Each names the CV sections its number is written in, handed to the judge
# whole (see ``select_candidates``).
NUMERIC_EVALUATORS: dict[str, tuple[str, ...]] = {
    "gpa": ("education",),
    "language_credential": ("languages", "certifications", "education"),
}


def default_numeric_judge(settings: Settings | None = None) -> NumericJudge | None:
    settings = settings or get_settings()
    if not settings.openai_api_key:
        return None
    model = settings.matching_judge_model or settings.llm_model
    try:
        client = OpenAIStructuredClient(
            api_key=settings.openai_api_key,
            model=model,
            purpose="matching.judge.numeric",
            schema_name="numeric_extraction",
            max_output_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Numeric judge unavailable: %s", type(exc).__name__)
        return None
    return NumericJudge(CachingJudgeClient(client, model=model, prompt_version=NUMERIC_PROMPT_VERSION))


def build_reading_set(
    resume: CanonicalResume,
    job: CanonicalJob,
    *,
    judge: GroundedJudge | None = None,
    numeric_judge: NumericJudge | None = None,
    settings: Settings | None = None,
    clarification_prompts: Mapping[str, str] | None = None,
) -> ReadingSet | None:
    """One reading per unresolved requirement.

    ``clarification_prompts`` maps a requirement id to the question the
    candidate answered, when the resume carries that answer.  Only the rescore
    path has one; a first-pass match passes nothing and the judge is called
    exactly as before.
    """
    settings = settings or get_settings()
    if not settings.matching_judge_enabled:
        return None
    unresolved = [item for item in job.requirements if isinstance(item, UnresolvedRequirement)]
    readings = ReadingSet(promptVersion=PROMPT_VERSION)
    if not unresolved:
        return readings
    judge = judge or default_judge(settings)
    if judge is None:
        for requirement in unresolved:
            readings.fallbacks[requirement.requirement_id] = "judge_unavailable"
        trace_event("judge", "judge_unavailable", requirement_count=len(unresolved))
        return readings
    numeric_judge = numeric_judge or default_numeric_judge(settings)
    readings = readings.model_copy(update={"prompt_version": judge.prompt_version})
    for requirement in unresolved:
        text = requirement_text(requirement)
        sections = NUMERIC_EVALUATORS.get(select_evaluator(requirement).name, ())
        candidates = select_candidates(
            requirement, resume, top_k=settings.matching_judge_top_k, sections=sections
        )
        if numeric_judge is not None and sections:
            numeric = numeric_judge.read(
                requirement_id=requirement.requirement_id, requirement_text=text, candidates=candidates
            )
            if numeric.reading is not None:
                readings.numerics[requirement.requirement_id] = numeric.reading
            if numeric.fallback is not None:
                readings.fallbacks[requirement.requirement_id] = numeric.fallback
            continue
        outcome = judge.judge(
            requirement_id=requirement.requirement_id,
            requirement_text=text,
            candidates=candidates,
            asked=(clarification_prompts or {}).get(requirement.requirement_id),
        )
        if outcome.reading is not None:
            readings.entailments[requirement.requirement_id] = outcome.reading
        if outcome.fallback is not None:
            readings.fallbacks[requirement.requirement_id] = outcome.fallback
    trace_event(
        "judge",
        "reading_set_built",
        prompt_version=readings.prompt_version,
        requirement_count=len(unresolved),
        read_count=len(readings.entailments),
        numeric_count=len(readings.numerics),
        fallback_count=len(readings.fallbacks),
    )
    return readings
