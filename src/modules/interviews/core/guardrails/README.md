# Interview guardrails

The package separates the safety boundary by direction. It is the place to add
or review checks that decide whether candidate input may reach a model and
whether generated text may reach the candidate.

## Input boundary

`input.py` owns deterministic pre-model decisions. The engine calls it from
`turn_engine._guards_in` after abort and session-timeout checks. It intercepts
explicit system-probe and instruction-steering patterns before the agent or
answer evaluator sees them, and routes them to the fixed `decline_system_probe`
action. Other candidate text stays untrusted data and is wrapped by
`common.candidate_block` whenever it is inserted into a model prompt.

`input.py` owns the candidate-input patterns and classifiers used for abort,
skip, clarification, repeat, give-up, interviewer-directed questions,
injection, and system-probe detection. The input module is the orchestration
point for pre-model checks; do not add another pre-model branch directly in
the agent graph.

## Output boundary

`output.py` is the stable API for generated-line validation. Per-kind checks
(acknowledgement, probe, clarification, transition, and candidate Q&A) are
called during generation. `enforce_final_output_guards` runs after every action
handler for memory-quote and canary checks, including validation of replacement
text and a safe fallback.

Output patterns, generated-text checks, and the final output boundary live in
`output.py`. Interview action/evaluation schema and pacing decisions remain in
`conversation/turn_rules.py`; topic-transition knowledge remains in
`dialogue/topics.py`. Legacy imports from the original modules stay available
for compatibility.

## Agent behavior

The action menu is still a separate control: `agent/menu.py` defines which
actions are legal for the current state. In `live`, malformed, violating, or
out-of-menu model decisions fall back to the rule path. Input guardrails run
before that decision; output guardrails run after its action handler. Neither
the menu nor the prompt replaces these code checks.
