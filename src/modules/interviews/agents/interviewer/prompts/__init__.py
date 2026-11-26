"""Prompt gửi cho LLM trong các lượt hội thoại phỏng vấn.

Mỗi module ở đây chỉ dựng văn bản prompt; phần gọi model, kiểm tra kết quả và
quyết định hành động của phiên nằm trong ``turn_engine`` và
``response_generation``. Tách ra để sửa lời prompt mà không phải đọc logic.
"""

from src.modules.interviews.agents.interviewer.prompts.candidate_memory import block as candidate_memory_block
from src.modules.interviews.agents.interviewer.prompts.clarify import clarify_prompt
from src.modules.interviews.agents.interviewer.prompts.closing_qna import closing_qna_speech_prompt
from src.modules.interviews.agents.interviewer.prompts.evaluation import (
    SYSTEM_PROBE_EVAL_RULE_VI,
    WARM_UP_GUIDE_VI,
    evaluate_answer_prompt,
)
from src.modules.interviews.agents.interviewer.prompts.interviewer_line import (
    interviewer_line_prompt,
)
from src.modules.interviews.agents.interviewer.prompts.probe import (
    coding_probe_prompt,
    probe_prompt,
)
from src.modules.interviews.agents.interviewer.prompts.transition import transition_context

__all__ = [
    "SYSTEM_PROBE_EVAL_RULE_VI",
    "WARM_UP_GUIDE_VI",
    "candidate_memory_block",
    "clarify_prompt",
    "closing_qna_speech_prompt",
    "coding_probe_prompt",
    "evaluate_answer_prompt",
    "interviewer_line_prompt",
    "probe_prompt",
    "transition_context",
]
