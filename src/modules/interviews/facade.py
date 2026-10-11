"""Public interview contract for other modules.

Job publishing (job_descriptions) and the Question Bank admin API use it to
pre-generate questions for a job's must-have skills and to read coverage.
"""

from src.modules.interviews.planning.question_coverage import (
    enqueue_question_coverage,
    prepare_job_question_coverage,
    question_coverage_report,
)

__all__ = ["enqueue_question_coverage", "prepare_job_question_coverage", "question_coverage_report"]
