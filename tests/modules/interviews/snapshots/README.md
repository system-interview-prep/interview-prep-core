# Turn snapshots

`turn_outputs_v0.jsonl` — every `InterviewCoreEngine.handle_turn` call made by
`test_interview_engine.py`, `test_persona_simulations.py` and
`test_followup_transition_behavior.py` on 2026-11-24 (branch
`feat/interview-agent-decision`, after the Pha -1 triage): 38 turns from 32 tests,
recorded with `RECORD_TURN_SNAPSHOT=<file>` (see `../conftest.py`).

It is the reference for INTERVIEW_AGENT_TEST_PLAN_V1 **T2.1.6** and **T2.D2.1**: the
engine's *current* behaviour, whatever the tests assert. Two uses:

1. **Drift detector** — re-record and diff; a change in `action`, `current_stage`,
   `message_text` or `metadata` (ignoring `time_remaining_seconds`, `started_at`,
   `elapsed_*`) is a behaviour change that must be intended.
2. **Dual-run gate (Pha 1)** — the recorder fixture runs the old engine and the new turn
   graph on the same mocked LLM and the same input, and asserts equality per test. The
   LLM responses are the test's own mocks, so a static replay is not enough; the fixture is.

Re-record only on purpose: `RECORD_TURN_SNAPSHOT=$PWD/tests/modules/interviews/snapshots/turn_outputs_v0.jsonl uv run ... pytest <the three files>`.
