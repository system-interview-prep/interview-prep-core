import pytest

from src.modules.interviews.agents.interviewer.prompts.candidate_tone import (
    candidate_tone_instruction,
)
from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine


@pytest.mark.parametrize("tone", ["supportive", "strict"])
def test_candidate_tone_only_changes_candidate_facing_wording(tone: str) -> None:
    instruction = candidate_tone_instruction(tone, is_vi=True)

    assert f"Candidate-selected interviewer tone: {tone}" in instruction
    assert "candidate-facing wording" in instruction
    assert "assessment criteria" in instruction
    assert "scores" in instruction


def test_standard_tone_does_not_add_prompt_instructions() -> None:
    assert candidate_tone_instruction("standard", is_vi=True) == ""


@pytest.mark.asyncio
async def test_selected_tone_reaches_candidate_facing_interviewer_generation() -> None:
    class CapturingLLM:
        system_prompt = ""

        async def generate_text(self, *, system_prompt: str, user_content: str) -> str:
            self.system_prompt = system_prompt
            return "Cảm ơn bạn đã chia sẻ."

    llm = CapturingLLM()
    engine = InterviewCoreEngine(llm_client=llm, candidate_tone="supportive")

    await engine._generate_interviewer_line(
        purpose="acknowledge_candidate",
        context="Candidate described a project.",
        is_vi=True,
    )

    assert "Candidate-selected interviewer tone: supportive" in llm.system_prompt
