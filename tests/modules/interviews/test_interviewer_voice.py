"""Lời nói của người phỏng vấn: tự nhiên hơn mà không đổi luồng hay lộ kết quả chấm.

Mỗi nhóm test ứng với một nguyên nhân trong
DOC_AND_PLAN/plan/interview/INTERVIEWER_NATURALNESS_2026-10-17.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.modules.interviews.application.runtime.chat_runtime import build_session_state_from_db
from src.modules.interviews.core.conversation.turn_engine import InterviewCoreEngine
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage, TurnAction
from src.modules.interviews.core.guardrails.output import is_evaluative
from src.modules.interviews.core.dialogue.policy import pick_variant
from src.modules.interviews.core.dialogue.opening import build_opening, warmup_question
from src.modules.interviews.application.runtime.spoken_questions import add_spoken_versions
from src.modules.interviews.agent.interviewer.spoken_questions import validate_spoken_question

LONG_ANSWER = (
    "Mình dùng Redis làm cache cho kết quả query nặng, đặt TTL năm phút cho từng key "
    "và xoá key khi dữ liệu trong database thay đổi để tránh đọc dữ liệu cũ."
)


class RecordingLLM:
    """Evaluator stub that records the prompts it was given."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.system_prompts: list[str] = []
        self.user_contents: list[str] = []

    async def generate_json(self, system_prompt: str, user_content: str):
        self.system_prompts.append(system_prompt)
        self.user_contents.append(user_content)
        return dict(self.result)

    async def generate_text(self, system_prompt: str, user_content: str):
        return "Khi Redis lỗi, hệ thống của bạn xử lý thế nào?"


def _state(**overrides: Any) -> dict[str, Any]:
    state = {
        "session_id": "sess-voice",
        "locale": "vi-VN",
        "current_stage": InterviewStage.DEEP_DIVE.value,
        "started_at": datetime.now(UTC),
        "target_duration_minutes": 25,
        "asked_question_ids": ["q-sql"],
        "current_turn_in_question": 0,
        "current_question_context": {
            "question_id": "q-sql",
            "main_prompt": "Trình bày cách tối ưu một truy vấn SQL chậm.",
            "competency": "SQL",
        },
        "questions_pool": {
            "DEEP_DIVE": [
                {
                    "question_id": "q-docker",
                    "competency": "Docker",
                    "stage": "DEEP_DIVE",
                    "main_prompt": "Trình bày sự khác nhau giữa image và container trong Docker.",
                    "spoken_prompt": "Theo bạn, image với container trong Docker khác nhau ở chỗ nào?",
                }
            ],
            "BEHAVIORAL": [
                {
                    "question_id": "q-beh",
                    "competency": "Teamwork",
                    "stage": "BEHAVIORAL",
                    "main_prompt": "Kể một lần bạn bất đồng trong team.",
                }
            ],
        },
    }
    state.update(overrides)
    return state


# --- Nguyên nhân gốc: tách vai giám khảo và người phỏng vấn --------------------


@pytest.mark.asyncio
async def test_evaluator_prompt_separates_grader_and_interviewer_roles() -> None:
    llm = RecordingLLM({"intent": "ANSWER", "sufficiency_status": "SUFFICIENT", "acknowledgement": "Ok.", "score": 7})
    engine = InterviewCoreEngine(llm_client=llm)
    await engine._evaluate_candidate_response(
        candidate_text=LONG_ANSWER,
        current_stage=InterviewStage.DEEP_DIVE,
        current_question={"main_prompt": "Bạn dùng Redis thế nào?"},
        recent_lines=["Ừ, mình hiểu ý bạn rồi.", "Ok, bạn dùng Docker Compose là chính."],
    )
    prompt = llm.system_prompts[0]
    assert "VAI 1 — GIÁM KHẢO" in prompt
    assert "VAI 2 — NGƯỜI PHỎNG VẤN" in prompt
    assert "NHÂN VẬT VÀ VĂN PHONG" in prompt
    # The copied example "Ok, mình hiểu rồi." is gone; the ack must name a detail.
    assert "Ok, mình hiểu rồi." not in prompt
    assert "khi phù hợp, một câu ngắn ghi nhận tự nhiên" in prompt
    assert "không tóm tắt toàn bộ câu trả lời" in prompt
    # Recent lines are passed so the model does not repeat them.
    assert "Ok, bạn dùng Docker Compose là chính." in llm.user_contents[0]


# --- A, B: câu ghi nhận cụ thể, rào chắn và câu dự phòng không lặp -------------


def test_dialogue_style_helpers_do_not_rewrite_generated_lines() -> None:
    from src.modules.interviews.core.conversation.turn_rules import normalize_candidate_pronoun

    line = "À, vậy là cache hết hạn sau năm phút nhé. Mình hỏi tiếp về tải cao nhé."
    assert normalize_candidate_pronoun(line) == line


def test_southern_voice_gets_southern_particles(monkeypatch) -> None:
    from src.modules.interviews.adapters.voice.conversation.answer_window import localize_particles

    assert localize_particles("Mình bắt đầu nhé.") == "Mình bắt đầu nhé."
    monkeypatch.setenv("INTERVIEWER_VOICE_DIALECT", "nam")
    assert localize_particles("Mình bắt đầu nhé. Hay nhỉ?") == "Mình bắt đầu nha. Hay hen?"


def test_polite_wording_is_not_rejected_as_a_style_violation() -> None:
    from src.modules.interviews.core.guardrails.output import breaks_house_style
    assert not breaks_house_style("Dạ, mình hiểu rồi.")
    assert not breaks_house_style("Mình hiểu rồi ạ.")
    assert not breaks_house_style("Mình hiểu rồi.")
    assert breaks_house_style("Mình ghi nhận điều đó.")


def test_prompts_share_one_layout_and_ask_for_the_candidate_words() -> None:
    from src.modules.interviews.core.dialogue.policy import speech_prompt

    prompt = speech_prompt(is_vi=True, role="R", task="T", rules=("X",), examples=("E",), output="O")
    for head in ("# Vai trò", "# Giọng văn", "# Nhiệm vụ", "# Luật bắt buộc", "# Câu mẫu", "# Đầu ra"):
        assert head in prompt
    assert "dùng đúng từ ứng viên đã dùng" in prompt


class _PromptLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.system_prompt = ""

    async def generate_text(self, system_prompt: str, user_content: str):
        self.system_prompt = system_prompt
        return self.reply


@pytest.mark.asyncio
async def test_follow_up_prompt_uses_the_shared_layout_and_logs_new_terms() -> None:
    llm = _PromptLLM("Khi Redis Sentinel lỗi, hệ thống của bạn xử lý thế nào?")
    engine = InterviewCoreEngine(llm_client=llm)
    probe = await engine._generate_probe_question(
        candidate_text=LONG_ANSWER, current_question={"main_prompt": "Bạn dùng Redis cache thế nào?"},
        missing_aspect="Khi Redis lỗi", seed="s:1",
    )
    assert probe == "Khi Redis Sentinel lỗi, hệ thống của bạn xử lý thế nào?"
    assert "# Nhiệm vụ" in llm.system_prompt and "Tech Lead" not in llm.system_prompt

    from src.modules.interviews.core.guardrails.output import new_terms
    assert new_terms(probe, "Bạn dùng Redis cache thế nào?", LONG_ANSWER) == ["Sentinel"]
    assert new_terms("Khi Redis lỗi thì sao, hay bạn retry?", "Redis", "") == ["retry"]


@pytest.mark.asyncio
async def test_turn_metadata_carries_the_speech_plan() -> None:
    llm = RecordingLLM({"intent": "ANSWER", "sufficiency_status": "SUFFICIENT",
                        "acknowledgement": "À, vậy là cache hết hạn sau năm phút.", "score": 8})
    output = await InterviewCoreEngine(llm_client=llm).handle_turn(
        CandidateTurnInput(session_id="sess-voice", turn_index=3, text_content=LONG_ANSWER), _state()
    )
    plan = output.metadata["speech_plan"]
    assert [p["act"] for p in plan] == ["ACK", "TRANSITION", "QUESTION"]
    assert plan[0]["source"] == "llm" and plan[2]["source"] == "pregenerated"


def test_closing_answer_names_only_what_the_posting_or_question_has() -> None:
    from src.modules.interviews.core.guardrails.output import qna_rejection_reason
    leak = lambda _value: False  # noqa: E731
    job = "Yêu cầu: Python, FastAPI. Địa điểm: Hà Nội"
    assert qna_rejection_reason("Theo mô tả, bạn làm Python ở Hà Nội.", job_context=job, rubric_leak=leak) is None
    assert qna_rejection_reason("Theo mô tả, team dùng Kafka.", job_context=job, rubric_leak=leak) == "unsupported_name"
    assert qna_rejection_reason(
        "Mô tả có nhắc Docker như bạn hỏi.", job_context=job, rubric_leak=leak, question="Có dùng Docker không?"
    ) is None


@pytest.mark.skip(reason="SCRIPT: fixed-phrase script pools removed in 75d3520 (speech v2); tests/TRIAGE_2026-11-24.md")
def test_scripted_pools_are_large_enough_not_to_repeat() -> None:
    from src.modules.interviews.core.dialogue import phrases as script

    assert len(script.ACK_FALLBACK_VI) >= 12
    for name in ("INTRO_ACK_VI", "NON_ANSWER_ACK_VI", "GIVE_UP_ACK_VI", "SKIP_ACK_VI", "SKIP_ACK_SHORT_VI", "PROBE_ACK_VI"):
        assert len(getattr(script, name)) >= 5, name
    for stage, lines in script.TRANSITION_STAGE_VI.items():
        assert any("nhé" not in line for line in lines), stage


# --- Trang quản trị "Lời nói AI": mỗi lượt ghi lại việc các rào chắn đã làm (25/10) ----


def test_speech_checks_collect_only_inside_a_turn() -> None:
    from src.modules.interviews.core.dialogue import speech_checks

    speech_checks.record("ack", "reject", "parroting", "x")  # no turn open: ignored, no error
    records = speech_checks.begin()
    speech_checks.record("ack", "pass", None, "not kept on pass")
    speech_checks.record("probe", "reject", "too_long", "chữ " * 200)
    assert records[0] == {"kind": "ack", "outcome": "pass"}
    assert records[1]["reason"] == "too_long" and len(records[1]["draft"]) == speech_checks.DRAFT_CHARS
    assert speech_checks.collected() == records


@pytest.mark.xfail(strict=True, reason="STALE: expectation predates 75d3520 / warm-up redesign; re-examined at Pha 1 snapshot; tests/TRIAGE_2026-11-24.md")
@pytest.mark.asyncio
async def test_probe_turn_records_ack_fix_and_cut_question() -> None:
    from src.modules.interviews.core.dialogue import speech_checks

    engine = InterviewCoreEngine(
        llm_client=RecordingLLM(
            {
                "intent": "ANSWER",
                "sufficiency_status": "INSUFFICIENT",
                "acknowledgement": "Mình hình dung được rồi.",
                "missing_aspect": "chọn cột cho index",
                "probe_question": "Bạn đánh index trên cột nào? Vì sao chọn cột đó?",
                "score": 5,
            }
        )
    )
    records = speech_checks.begin()
    output = await engine.handle_turn(
        CandidateTurnInput(session_id="sess-voice", turn_index=3, text_content=LONG_ANSWER), _state()
    )
    assert output.action == TurnAction.PROBE
    seen = [(r["kind"], r["outcome"], r.get("reason")) for r in records]
    assert ("ack", "pass", None) in seen
    assert ("ack", "fix", "closed_before_probe") in seen
    assert ("probe", "fix", "extra_question") in seen
    assert ("probe", "pass", "from_evaluation") in seen
    assert output.message_text.endswith("Bạn đánh index trên cột nào?")


@pytest.mark.asyncio
async def test_rejected_acknowledgement_draft_is_recorded() -> None:
    from src.modules.interviews.core.dialogue import speech_checks

    engine = InterviewCoreEngine(
        llm_client=RecordingLLM(
            {
                "intent": "ANSWER",
                "sufficiency_status": "SUFFICIENT",
                "acknowledgement": "Câu trả lời rất tốt!",
                "score": 8,
            }
        )
    )
    records = speech_checks.begin()
    await engine.handle_turn(
        CandidateTurnInput(session_id="sess-voice", turn_index=3, text_content=LONG_ANSWER), _state()
    )
    ack = [r for r in records if r["kind"] == "ack"]
    assert ack[0]["outcome"] == "reject" and ack[0]["draft"] == "Câu trả lời rất tốt!"


def test_spoken_turn_trace_carries_speech_control(monkeypatch) -> None:
    from src.modules.interviews.application.runtime import chat_runtime
    from src.modules.interviews.core.dialogue import speech_checks
    from src.modules.interviews.core.contracts.interview import InterviewerTurnOutput

    captured: list[dict] = []
    monkeypatch.setattr(chat_runtime, "trace_event", lambda wf, ev, **f: captured.append(f))
    speech_checks.begin()
    speech_checks.record("ack", "reject", "parroting", "Bạn đặt TTL năm phút.")
    plan = [{"act": "ACK", "source": "fallback"}, {"act": "QUESTION", "source": "llm"}]
    output = InterviewerTurnOutput(
        session_id="s1", turn_index=4, message_text="Ừm, mình hiểu.\n\nVì sao năm phút?",
        action=TurnAction.PROBE, current_stage=InterviewStage.DEEP_DIVE,
        metadata={"speech_plan": plan, "new_terms": ["TTL"], "ack_source": "fallback"},
    )
    chat_runtime._trace_spoken_turn(
        session_id="s1", turn_index=3, candidate_text="Em đặt TTL 5 phút.", modality="CHAT",
        core_output=output, recent_lines=[], core_ms=900,
    )
    record = captured[0]
    assert record["speech_plan"] == plan and record["new_terms"] == ["TTL"]
    assert record["speech_checks"][0]["reason"] == "parroting"
    assert record["transition"] is False
