# Interview Chat — workflow, evidence, time and completion contract

Status: implementation target, 2026-09-25. This document distinguishes the current MVP from the next gates.

## Product boundary

Question practice under /practice uses sequential frozen questions. Interview Chat under /interview is a conversation grounded in a CV, an active JD, a P1 agenda and P2 frozen approved question/rubric versions. Text first; voice can reuse the same controller later. Matching score is never an interview score.

## Session lifecycle

1. Validate ownership of CV and access to JD; create an interview_chat session and DRAFT plan.
2. P1 builds a deterministic agenda including all must-have evaluation targets, importance, selection rank, question budget and source revisions.
3. P2 hard-filters approved/calibrated questions by exact taxonomy/concept/purpose, compatible locale and rubric. Freeze exact question and rubric snapshots. If any target lacks eligible questions, return question_unavailable before entering the room. Never synthesize a main question.
4. Start sets a server-authoritative chat start/deadline, records a greeting and asks only the first frozen main question.
5. For each candidate message, classify intent (answer, clarification request, skip, end request); record the message with an idempotency key; decide whether to clarify, probe, advance or wrap up. A clarification request does not count as assessed evidence. A substantive probe is bounded to the current question and recorded with decision provenance.
6. Close a turn only when its relevant candidate messages have been collected or it is skipped/timed out. Advance to an uncovered competency while the deadline permits. Never open a new question without enough remaining time plus wrap-up reserve.
7. End exactly once as COMPLETED, USER_ENDED, TIME_EXPIRED or TECHNICAL_FAILURE. Preserve transcript and unanswered targets. Report coverage and review state honestly.

## Controller order of precedence

- Closed/expired/explicit end request: WRAP_UP.
- Pending provider action: resume or retry the same action, without accepting a second candidate message.
- Clarification request: rephrase the current approved question, preserving objective.
- Skip: mark the current turn as skipped with missing evidence.
- Answer: if one targeted clarification can resolve missing evidence, no follow-up has been used and time permits, ask it; otherwise finish the turn.
- Next question: prioritize uncovered must-have targets in persisted P1 selectionRank order, then other targets; select only from frozen P2 versions, respecting remaining time and duplicate constraints.
- No feasible next question: WRAP_UP.

LLM may propose action and wording, but code validates hard limits, citations to current candidate message and forbidden disclosures. A generic safe clarification is acceptable; a new unapproved assessment objective is not. Persist policy version, decision reason, input message IDs, selected question IDs and rejected proposal reason for audit.

## Timing

Store chat_started_at and deadline_at on the session. Do not use session creation time because planning can be slow. Return serverNow, deadlineAt and remainingSeconds on runtime reads. The server checks the deadline before accepting a message and before opening the next question. Reserve time for wrap-up; the reserve and minimum-question durations are versioned policy parameters, initially measured in a pilot rather than claimed as empirical constants. Network/provider time is accounted for explicitly in policy. Refresh cannot reset the deadline. An expired session remains closed even if no browser tab is open; the first later request must finalize it, with an optional background sweeper.

## Evidence and scoring gate

For each frozen question, collect candidate message IDs and immutable content spans. A grader proposes per-criterion ordinal levels, reason codes and evidence spans under the frozen rubric. Backend validates that each span belongs to the correct candidate message/turn, then computes criterion aggregates and coverage under a versioned policy. Missing, skipped or unscorable evidence remains unknown/review_required, never silently becomes zero. A session score is nullable until required criterion coverage and review gates pass. A completed conversation can therefore have no score. Do not show mock score or infer hiring decisions.

## Reliability gate

Require clientMessageId; unique per session and bound to a content hash. Record PENDING/PROCESSING/COMPLETED/FAILED_RETRYABLE action state. Serialize state transitions per session; allocate sequence under a row lock or database sequence. The first transaction persists the candidate message and action claim. Provider work runs outside the transaction. The final transaction atomically stores the assistant message and transition. Retrying the same key returns the stored result or resumes a pending action; a different payload for the same key is rejected. A provider timeout must not strand the session or create duplicate replies. Startup and completion are idempotent.

## Acceptance scenarios

- Full answer: advance with a grounded transition.
- Sparse answer: clarify at most once, then advance with insufficient evidence if still sparse.
- Candidate asks for clarification: rephrase without consuming an assessment answer.
- Skip/end request: preserve unanswered targets and close with the correct reason.
- Slow or failed provider after candidate persistence: refresh and retry without duplicate messages or turn advancement.
- Parallel submits: one serialized outcome and stable message sequence.
- Deadline before a new question: no new question; close with TIME_EXPIRED.
- Scoring: every non-null criterion result has valid evidence; incomplete coverage yields null session score.

## Current implementation gaps at commit bde554c

The chat runtime traverses turns by turn_index, lacks a server deadline and scoring, and uses a generic fallback if a frozen question has no text. clientMessageId is optional; MAX(sequence)+1 is not serialized. A retry after candidate persistence may return assistantResponse=null indefinitely. The current probe validator checks length and prohibited words, not semantic grounding. These are implementation gaps, not completed features.

## Implementation sequence

1. Runtime safety: fail closed on missing frozen question, strict experience type, required idempotency key, pending recovery, serialized sequence, tests.
2. Server time: start/deadline migration, wrap-up and expiry policy, tests and FE timer from server state.
3. Controller intent and decision trace: clarify/skip/probe/next with bounded memory and no unapproved main question, scenario tests.
4. Rubric evaluation: criterion evidence validation, coverage, nullable score, human review and calibration tests.
5. FE report: transcript, evidence and honest unavailable/review states; no fake scores.
