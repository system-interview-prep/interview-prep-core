"""Demo sessions: the short package used to present every interview stage live.

A demo session follows its frozen turns instead of the clock. With the regular
time-scaled pacing a 3-minute session had room for one technical question, so
it never reached CHALLENGE, and LLM latency consumed the reserve that gates
the closing Q&A. The planner, the question selector and the core engine all
read this module so the rule lives in one place.
"""

# The frontend's "Demo" package (questionBudget.ts DEMO_DURATION_MINUTES).
DEMO_DURATION_MINUTES = 3

# Technical questions frozen for a demo: one DEEP_DIVE and one CHALLENGE.
DEMO_TECHNICAL_QUESTIONS = 2

# A demo may run past its nominal duration by this much before the hard
# timeout closes it, so a presenter who pastes answers at a normal pace still
# reaches BEHAVIORAL and the closing Q&A.
DEMO_OVERTIME_GRACE_SECONDS = 180


def is_demo_duration(duration_minutes: int | float | None) -> bool:
    try:
        return 0 < float(duration_minutes or 0) <= DEMO_DURATION_MINUTES
    except (TypeError, ValueError):
        return False
