# Turn snapshots

**The current reference is `turn_outputs_v1.jsonl`** (35 turns, recorded
2026-11-26 at the repaired Phase 0 baseline of
INTERVIA_INTERVIEW_AGENT_V2_SPEC). Use it for every drift check from here on.

`turn_outputs_v0.jsonl` is kept only as history: it records the engine as it
behaved on 2026-11-24, *before* `ac91bd3`…`34dece7`. Those five commits removed
scripted speech rewriting, the rigid dialogue limits and the warm-up
confirmation beat on purpose, so v0 differs from today in 24 of 38 turns. Every
difference is in the `ack_*`/`acknowledgement`/`speech_plan`/`transition*` family
or the warm-up and probe-cap outcomes; five scripted-transition tests and the
probe-cap test no longer exist. Comparing against v0 reports those as drift,
which is why it is no longer the reference.

Both files hold every `InterviewCoreEngine.handle_turn` call made by
`test_interview_engine.py`, `test_persona_simulations.py` and
`test_followup_transition_behavior.py`, recorded with
`RECORD_TURN_SNAPSHOT=<file>` (see `../conftest.py`).

It is the reference for INTERVIEW_AGENT_TEST_PLAN_V1 **T2.1.6** and **T2.D2.1**: the
engine's *current* behaviour, whatever the tests assert. Two uses:

1. **Drift detector** — re-record and diff; a change in `action`, `current_stage`,
   `message_text` or `metadata` (ignoring `time_remaining_seconds`, `started_at`,
   `elapsed_*`) is a behaviour change that must be intended.
2. **Dual-run gate (Pha 1)** — the recorder fixture runs the old engine and the new turn
   graph on the same mocked LLM and the same input, and asserts equality per test. The
   LLM responses are the test's own mocks, so a static replay is not enough; the fixture is.

Re-record only on purpose: `RECORD_TURN_SNAPSHOT=$PWD/tests/modules/interviews/snapshots/turn_outputs_v1.jsonl uv run ... pytest <the three files>`.

## Commands

```text
# drift check: record to a scratch file, then diff against the reference
RECORD_TURN_SNAPSHOT=/tmp/after.jsonl uv run ... pytest <the three files>
uv run python tests/modules/interviews/snapshots/compare.py \
    tests/modules/interviews/snapshots/turn_outputs_v1.jsonl /tmp/after.jsonl

# dual-run gate (T2.1.6): every handle_turn also runs the agent's turn graph on a
# deep copy of the engine and the input; outputs must agree
DUAL_RUN_TURNS=1 uv run ... pytest tests/modules/interviews tests/modules/voice/test_language.py
```

`compare.py` ignores `session_id` (tests draw `uuid4()` per run), `time_remaining_seconds`
and `metadata.decision` (added by the turn graph's `record` node). Verified 2026-11-24
after Pha 1: 38/38 turns identical against v0. Re-verified 2026-11-26 against v1:
35/35 turns identical, and the `DUAL_RUN_TURNS=1` gate passes (1141 tests).
