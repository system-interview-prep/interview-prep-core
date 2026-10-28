# AI-authored clarification questions for matching

The `POST /api/v1/matching/clarifications` endpoint first runs the ordinary
matching pipeline. It then considers only requirements whose result is
`unknown`. The optional clarification path can identify a missing evidence
dimension and ask an LLM to write one neutral Vietnamese question. It does not
change requirement statuses, eligibility, or scores.

## Controls

The matching clarification feature is grouped under
`src/modules/matching/clarifications`: `models` owns the request/response
contracts, `service` builds questions after the baseline match, `answer_evidence`
parses scoped self-reports, `rescore` runs the second scoring pass, and `router`
owns both HTTP endpoints. The question-generation guard remains in
`clarification_questions.py` as a separate provider/validation component.

- The feature is disabled unless `MATCHING_CLARIFICATION_QUESTIONS_ENABLED=true`.
- `JEV_CLARIFICATION_ENABLED=true` and a TypeSafe/Jev key are separately
  required to choose whether a candidate question is safe and what fact is
  missing.
- `OPENAI_API_KEY` is required for question composition. The request uses a
  strict JSON schema and `store=false`.
- `MATCHING_CLARIFICATION_SEMANTIC_THRESHOLD` is required and must be between 0
  and 1. Calibrate it on a reviewed Vietnamese set of relevant and deliberately
  irrelevant questions before enabling generation in production. Do not copy a
  threshold from another model or dataset.
- Questions must contain exactly one question, use only candidate CV evidence
  references selected for the plan, and cannot introduce numeric details absent
  from that CV evidence. JD thresholds are deliberately excluded from this
  allowlist so the generated question does not reveal or repeat the employer's
  cutoff.
- The question is withheld if generation, structural validation, or semantic
  scoring fails. The match remains `unknown` and the original result is
  unchanged.
- Semantic similarity checks whether the question follows the intended missing
  dimension. It is not a proof of factuality; the source and structural checks
  remain separate gates.

## Rollout

1. Keep both clarification flags off while collecting and reviewing a test set.
2. Evaluate semantic scores for on-target and off-target questions, including
   unsupported assumptions, added tools, numbers, certifications, and leading
   wording. Select a threshold for an acceptable false-accept rate.
3. Enable the Jev analysis and question generation in a non-production
   environment. Review returned question text and `semanticAlignmentScore`.
4. Enable production only after review. Disable
   `MATCHING_CLARIFICATION_QUESTIONS_ENABLED` to roll back instantly; ordinary
   matching continues to work.

## Candidate answers and rescore

The browser uses `POST /api/v1/matching/clarifications-by-ids` with
`candidateId` and `jobId`; the backend resolves the canonical CV/JD itself and
returns the baseline match plus AI-written questions. It then sends
`candidateId`, `jobId`, that returned `initialAnalysis`, and answers keyed by
`requirementId` to `POST /api/v1/matching/clarifications/rescore-by-ids`. The
backend resolves the canonical inputs again, recomputes the baseline, and
rejects answers for requirements that are no longer `unknown` or were not
returned as clarification requests. Client-supplied scores and statuses are
never used as the baseline. The canonical-payload endpoints
`/clarifications` and `/clarifications/rescore` remain available for trusted
server-side callers.

Each answer is attached as scoped `candidate_self_report` evidence to only the
requirement it answers. The normal deterministic matching pipeline runs again;
all unanswered requirements retain their recomputed baseline status. An
answer that does not provide evidence the evaluator can use remains `unknown`.
The response returns both original and final match results, records evidence
provenance, and caps confidence for results that use self-reported evidence.
Self-reports are not written into the canonical CV document or reused for other
requirements. Generated questions remain separate from the interview question
bank.

The workspace `.env` enables the flags with an initial semantic threshold of
`0.72` for local integration. This value is a starting configuration, not a
calibration result or production rollout approval. Production still requires
reviewing the threshold against a labeled relevant/irrelevant question set.
