"""Public interview contract for other modules.

Job publishing (job_descriptions) and the Question Bank admin API use it to
pre-generate questions for a job's must-have skills and to read coverage; the
admin API reads a session's plan and question quality through it.
"""

from src.modules.interviews.application import interviewer_playbook
from src.modules.interviews.application.admin_actions import AdminActionError as InterviewAdminActionError
from src.modules.interviews.application.admin_actions import batch_state as admin_batch_grading_state
from src.modules.interviews.application.admin_actions import close_session as admin_close_interview_session
from src.modules.interviews.application.admin_actions import count_pending as admin_count_gradable_sessions
from src.modules.interviews.application.admin_actions import grading_state as admin_interview_grading_state
from src.modules.interviews.application.admin_actions import (
    start_batch_grading as admin_grade_pending_sessions,
)
from src.modules.interviews.application.admin_actions import start_grading as admin_grade_interview_session
from src.modules.interviews.application.pipeline_board import load_alerts as interview_ai_alerts
from src.modules.interviews.application.pipeline_board import load_live_sessions as live_interview_sessions
from src.modules.interviews.application.pipeline_health import load_health as interview_ai_health
from src.modules.interviews.application.pipeline_view import ai_call_view, summarize_ai_calls
from src.modules.interviews.application.pipeline_view import langfuse_links as interview_langfuse_links
from src.modules.interviews.application.pipeline_view import load_pipeline as session_pipeline
from src.modules.interviews.application.runtime_config import interview_config_groups
from src.modules.interviews.application.runtime_config import item as interview_config_item
from src.modules.interviews.application.speech_control_view import (
    load_speech_control as interview_speech_control,
)
from src.modules.interviews.application.speech_control_view import (
    review_speech_check as review_interview_speech_check,
)
from src.modules.interviews.evaluation.bank_insights import run_insights as run_question_bank_insights
from src.modules.interviews.evaluation.question_quality import (
    question_quality_report as interview_question_quality_report,
)
from src.modules.interviews.evaluation.question_quality import session_plan_audit
from src.modules.interviews.planning.question_coverage import (
    enqueue_question_coverage,
    prepare_job_question_coverage,
    question_coverage_report,
)
from src.modules.interviews.planning.question_generation import generate_and_file as generate_skill_questions

__all__ = [
    "interviewer_playbook",
    "generate_skill_questions",
    "run_question_bank_insights",
    "InterviewAdminActionError",
    "admin_close_interview_session",
    "admin_grade_interview_session",
    "admin_batch_grading_state",
    "admin_count_gradable_sessions",
    "admin_grade_pending_sessions",
    "admin_interview_grading_state",
    "interview_config_groups",
    "interview_config_item",
    "ai_call_view",
    "interview_ai_alerts",
    "interview_ai_health",
    "interview_langfuse_links",
    "interview_speech_control",
    "review_interview_speech_check",
    "live_interview_sessions",
    "session_pipeline",
    "summarize_ai_calls",
    "enqueue_question_coverage",
    "interview_question_quality_report",
    "prepare_job_question_coverage",
    "question_coverage_report",
    "session_plan_audit",
]
