"""Interviews are spoken: short questions, and short list-style answers are fine."""

import pytest

from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from src.modules.interviews.core.contracts.interview import InterviewStage
from src.modules.interviews.agents.evaluator.grading import InterviewEvaluationEngine
from src.modules.interviews.agents.evaluator.evaluation_types import TurnEvaluationInput
from src.modules.interviews.agents.planner.selection import _behavioral_snapshot


class CapturingLLM:
    def __init__(self):
        self.system_prompts: list[str] = []

    async def generate_json(self, system_prompt: str, user_content: str):
        self.system_prompts.append(system_prompt)
        return {"intent": "ANSWER", "sufficiency_status": "SUFFICIENT", "acknowledgement": "Ok.", "score": 7}


def test_the_engine_no_longer_splices_speech_around_a_question() -> None:
    """ac91bd3 removed scripted speech rewriting.

    ``_synthesize_interviewer_speech`` used to glue an acknowledgement, a
    transition and the question into one string with blank lines between them.
    The model now writes the whole utterance, so no such assembler may come
    back: a reintroduced one would put code-written filler in front of every
    question again.
    """
    engine = InterviewCoreEngine(llm_client=CapturingLLM())
    assert not hasattr(engine, "_synthesize_interviewer_speech")


@pytest.mark.asyncio
async def test_sufficiency_judge_accepts_list_style_answers() -> None:
    llm = CapturingLLM()
    engine = InterviewCoreEngine(llm_client=llm)
    await engine._evaluate_candidate_response(
        candidate_text="- WHERE lọc dòng trước\n- HAVING lọc sau GROUP BY\n- vd: COUNT > 5",
        current_stage=InterviewStage.DEEP_DIVE,
        current_question={"main_prompt": "WHERE khác HAVING thế nào?"},
    )
    prompt = llm.system_prompts[0]
    assert "liệt kê ý" in prompt
    assert "KHÔNG theo văn phong hay độ dài" in prompt


def test_final_report_asks_star_only_for_experience_questions() -> None:
    engine = InterviewEvaluationEngine()
    technical = TurnEvaluationInput(
        turn_id="t1",
        turn_index=2,
        question_prompt="WHERE khác HAVING?",
        candidate_answer="- WHERE trước",
        stage="DEEP_DIVE",
    )
    instructions, user_content = engine.get_turn_evaluation_prompt(technical)
    assert "Giai đoạn: DEEP_DIVE" in user_content
    assert "Chỉ áp dụng cho câu hỏi kinh nghiệm" in instructions
    assert "không đòi STAR" in instructions
    assert "thiếu cấu trúc STAR" not in instructions


def test_preset_questions_are_short() -> None:
    behavioral = _behavioral_snapshot("vi-VN")
    assert len(behavioral["questionText"].split()) <= 30
    assert "STAR" not in behavioral["questionText"]
