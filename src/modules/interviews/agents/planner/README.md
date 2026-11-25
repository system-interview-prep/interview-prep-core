# Planner Agent

`PlannerAgent` is a compiled LangGraph workflow in `planner_graph.py` and owns
pre-interview P1/P2 preparation. Its request-scoped state carries the command
and operation inputs; PostgreSQL remains the durable source for plans,
contract snapshots, and frozen turns. It resolves the selected
Admin blueprint version (or the active SQL system default), validates a typed
contract snapshot, and keeps that immutable policy with the session plan.
`planner.py` builds the competency plan, `question_selector.py` selects and
freezes the question queue, and `project_evidence.py` asks the Planner Agent to
extract CV project facts through a bounded tool. Code accepts only exact source
quotes and known project/requirement IDs, scores the validated evidence, and
persists it in the SQL plan context. P2 reuses that persisted result; it does
not parse project descriptions with regex or call a second extractor.

The default contract is seeded as an ACTIVE version in SQL by migration
`20261124_0043`; runtime contract resolution does not silently replace a missing
SQL default with in-memory defaults. The deterministic CV–JD entry gate remains
in `planning/entry_gate.py`.
Question Bank job coverage and draft generation remain under `planning/` because
they serve job-level and background workflows. P2 does not generate new
Question Bank entries; uncovered targets are returned for post-session
background suggestions.

For P1, code derives the eligible competency targets from the job and match
result. The Planner Agent receives those targets, the SQL-frozen contract,
duration, and question budget, then uses the `rank_competency_targets` function
tool to propose an ordering. Code validates
that the proposal is an exact permutation, forces MUST targets ahead of
optional ones, and applies the contract's deterministic eligibility, budget,
allocation, and validation rules. Invalid output or provider failure falls
back to the deterministic order and is traced. The model cannot create targets,
change the contract, or choose counts.

For P2, code filters Question Bank candidates and computes feasible selections
under target, type, time, repeat, and coverage constraints. A LangGraph tool
loop lets the Planner Agent call `search_question_bank` on that bounded,
read-only candidate catalog and submit `submit_question_bank_selection`. The
agent may choose among feasible subsets (or eligible candidate IDs in legacy
plans). Code validates exact target coverage,
candidate membership, counts, required re-asks, and global uniqueness before
freezing. Invalid output or provider failure uses the existing deterministic
solver/selection and is traced. The Agent cannot author, edit, or introduce a
Question Bank question during freeze.

The SQL plan row is the durable record for the contract snapshot and P1/P2
planner decisions; no separate mutable agent memory is authoritative.
