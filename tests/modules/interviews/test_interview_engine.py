# tests/modules/interviews/test_interview_engine.py
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from src.modules.interviews.core.interview_types import (
    CandidateTurnInput,
    InterviewStage,
    SessionExitReason,
    TurnAction,
)
from src.modules.interviews.core.interview_engine import (
    SAFE_FALLBACK_PROBE_VI,
    InterviewCoreEngine,
    is_abort_request,
    is_skip_request,
)


class MockLLM:
    def __init__(self, eval_result=None, probe_text=None):
        self.eval_result = eval_result or {
            "score": 8.0,
            "is_sufficient": True,
            "acknowledgement": "Cảm ơn câu trả lời rất rõ ràng.",
        }
        self.probe_text = probe_text or "Bạn có thể nói sâu hơn về kiến trúc này không?"

    async def generate_json(self, system_prompt: str, user_content: str):
        return self.eval_result

    async def generate_text(self, system_prompt: str, user_content: str):
        return self.probe_text


class TestInterviewCoreEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session_id = uuid4()
        self.sample_questions_pool = {
            "WARM_UP": [
                {
                    "question_id": "q-warmup-1",
                    "competency": "Intro",
                    "stage": "WARM_UP",
                    "main_prompt": "Hãy giới thiệu về bản thân bạn.",
                }
            ],
            "VALIDATE": [
                {
                    "question_id": "q-val-1",
                    "competency": "CV Verification",
                    "stage": "VALIDATE",
                    "main_prompt": "Bạn có ghi từng tối ưu Redis, bạn có thể nói chi tiết không?",
                },
                {
                    "question_id": "q-val-2",
                    "competency": "CV Verification 2",
                    "stage": "VALIDATE",
                    "main_prompt": "Dự án Microservices bạn làm có bao nhiêu service?",
                },
            ],
            "DEEP_DIVE": [
                {
                    "question_id": "q-deep-1",
                    "competency": "System Design",
                    "stage": "DEEP_DIVE",
                    "main_prompt": "Làm thế nào để handle 10,000 req/sec với Postgres?",
                },
                {
                    "question_id": "q-deep-2",
                    "competency": "Database Indexing",
                    "stage": "DEEP_DIVE",
                    "main_prompt": "B-Tree và Hash Index khác nhau như thế nào?",
                },
            ],
            "CLOSING": [
                {
                    "question_id": "q-close-1",
                    "competency": "Q&A",
                    "stage": "CLOSING",
                    "main_prompt": "Bạn có câu hỏi nào cho chúng tôi không?",
                }
            ],
        }

    async def test_normal_transition_next_question(self):
        """1. Trả lời đạt và đủ ý -> Core chuyển ngay sang câu hỏi tiếp theo (NEXT_QUESTION)."""
        llm = MockLLM(eval_result={"score": 8.5, "is_sufficient": True, "acknowledgement": "Rất tốt."})
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=0,
            text_content="Tôi có 3 năm kinh nghiệm lập trình backend với Python và FastAPI.",
        )
        session_state = {
            "current_stage": InterviewStage.WARM_UP.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-warmup-1"],
            "current_turn_in_question": 0,
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.NEXT_QUESTION)
        self.assertEqual(output.current_stage, InterviewStage.VALIDATE)
        self.assertFalse(output.is_session_finished)
        self.assertIn("Bạn có ghi từng tối ưu Redis", output.message_text)

    async def test_probe_when_insufficient_on_turn_0(self):
        """2. Trả lời chưa đủ ý ở lượt đầu -> Kích hoạt đúng 1 câu hỏi đào sâu (PROBE)."""
        llm = MockLLM(
            eval_result={"score": 5.0, "is_sufficient": False, "acknowledgement": "Cảm ơn bạn."},
            probe_text="Bạn có thể phân tích rõ hơn về caching strategy bạn dùng không?",
        )
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=1,
            text_content="Tôi chỉ cài Redis rồi dùng thôi.",
        )
        session_state = {
            "current_stage": InterviewStage.VALIDATE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": [],
            "current_turn_in_question": 0,
            "current_question_context": {
                "main_prompt": "Bạn có ghi từng tối ưu Redis, bạn có thể nói chi tiết không?"
            },
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.PROBE)
        self.assertEqual(output.current_stage, InterviewStage.VALIDATE)
        self.assertEqual(output.metadata.get("turn_in_question"), 1)
        self.assertIn("caching strategy", output.message_text)

    async def test_no_probe_when_turn_in_question_is_already_1(self):
        """3. Đã ở lượt probe (turn_in_question=1) -> Bắt buộc chuyển câu hỏi tiếp theo, không probe lặp."""
        llm = MockLLM(eval_result={"score": 5.0, "is_sufficient": False, "acknowledgement": "Ghi nhận ý kiến."})
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=2,
            text_content="Tôi vẫn chưa nhớ rõ cụ thể thuật toán lúc đó.",
        )
        session_state = {
            "current_stage": InterviewStage.VALIDATE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-val-1"],
            "current_turn_in_question": 1,
            "current_question_context": {"main_prompt": "Bạn có ghi từng tối ưu Redis..."},
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.NEXT_QUESTION)
        self.assertEqual(output.metadata.get("turn_in_question"), 0)
        self.assertIn("Microservices", output.message_text)

    async def test_early_exit_2_strikes_in_validation_stage(self):
        """4. 2 điểm trừ liên tiếp ở vòng Validate -> Kích hoạt dừng sớm FAST_FAIL_VALIDATION."""
        llm = MockLLM(eval_result={"score": 2.5, "is_sufficient": False, "acknowledgement": "Cảm ơn bạn."})
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=2,
            text_content="Dự án đó tôi không trực tiếp làm, tôi chỉ nghe kể lại.",
        )
        session_state = {
            "current_stage": InterviewStage.VALIDATE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "consecutive_fails": 1,  # Đã có 1 strike trước đó
            "allow_early_exit": True,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-val-1"],
            "current_turn_in_question": 0,
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.TERMINATE)
        self.assertEqual(output.exit_reason, SessionExitReason.FAST_FAIL_VALIDATION)
        self.assertTrue(output.is_session_finished)
        self.assertEqual(output.current_stage, InterviewStage.CLOSED)

    async def test_early_exit_disabled_in_mock_practice_mode(self):
        """5. Khi allow_early_exit = False (chế độ luyện tập) -> Không ngắt phiên dù trượt 2 lần."""
        llm = MockLLM(eval_result={"score": 2.0, "is_sufficient": True, "acknowledgement": "Cảm ơn bạn."})
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=2,
            text_content="Tôi không biết làm bài này.",
        )
        session_state = {
            "current_stage": InterviewStage.VALIDATE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "consecutive_fails": 1,
            "allow_early_exit": False,  # Mock practice mode
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-val-1"],
            "current_turn_in_question": 0,
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertNotEqual(output.action, TurnAction.TERMINATE)
        self.assertFalse(output.is_session_finished)

    async def test_dynamic_pacing_suppresses_probe_when_behind_schedule(self):
        """6. Thời gian đã vượt quá 80% ngân sách -> Khóa probe để đuổi kịp tiến độ."""
        llm = MockLLM(
            eval_result={"score": 5.0, "is_sufficient": False, "acknowledgement": "Cảm ơn."},
            probe_text="Đáng lẽ sẽ probe câu này...",
        )
        engine = InterviewCoreEngine(llm_client=llm)

        # 21 phút trên tổng 25 phút = 84% > 80%
        started_at = datetime.now(timezone.utc) - timedelta(minutes=21)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=3,
            text_content="Tôi dùng index thông thường.",
        )
        session_state = {
            "current_stage": InterviewStage.DEEP_DIVE.value,
            "started_at": started_at,
            "target_duration_minutes": 25,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-deep-1"],
            "current_turn_in_question": 0,
            "current_question_context": {"main_prompt": "Làm thế nào để handle 10,000 req/sec..."},
        }

        output = await engine.handle_turn(turn_input, session_state)

        # Vì trễ giờ nên bị ép chuyển câu (NEXT_QUESTION) thay vì PROBE
        self.assertEqual(output.action, TurnAction.NEXT_QUESTION)
        self.assertIn("B-Tree và Hash Index", output.message_text)

    async def test_hard_timeout_terminates_session(self):
        """7. Thời lượng còn lại <= 30 giây -> Dừng phiên ngay lập tức (HARD_TIMEOUT)."""
        llm = MockLLM()
        engine = InterviewCoreEngine(llm_client=llm)

        # Bắt đầu từ 24 phút 40 giây trước -> Còn 20 giây (< 30s)
        started_at = datetime.now(timezone.utc) - timedelta(minutes=24, seconds=40)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=5,
            text_content="Câu trả lời cuối cùng...",
        )
        session_state = {
            "current_stage": InterviewStage.DEEP_DIVE.value,
            "started_at": started_at,
            "target_duration_minutes": 25,
            "questions_pool": self.sample_questions_pool,
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.TERMINATE)
        self.assertEqual(output.exit_reason, SessionExitReason.HARD_TIMEOUT)
        self.assertTrue(output.is_session_finished)

    async def test_probe_guardrail_prevents_rubric_leak(self):
        """8. LLM sinh từ khóa nhạy cảm (barem, thang điểm 10) -> Guardrail chặn và thay bằng probe an toàn."""
        leaked_prompt = "Theo barem và thang điểm 10 của chúng tôi, bạn có thể nói rõ hơn không?"
        llm = MockLLM(
            eval_result={"score": 5.0, "is_sufficient": False, "acknowledgement": "Cảm ơn bạn."},
            probe_text=leaked_prompt,
        )
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=1,
            text_content="Tôi chỉ làm theo tài liệu hướng dẫn.",
        )
        session_state = {
            "current_stage": InterviewStage.VALIDATE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": [],
            "current_turn_in_question": 0,
            "current_question_context": {"main_prompt": "Chi tiết triển khai..."},
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.PROBE)
        self.assertEqual(output.message_text, SAFE_FALLBACK_PROBE_VI)
        self.assertNotIn("barem", output.message_text)
        self.assertNotIn("thang điểm 10", output.message_text)

    async def test_coding_turn_triggers_tech_lead_probe_on_passed_tests(self):
        """9. Ứng viên nộp giải pháp coding qua test -> Kích hoạt Tech Lead probe phản biện kiến trúc."""
        tech_lead_probe = "Bạn có thể phân tích đánh đổi giữa việc dùng Lock so với Semaphore trong hàm này không?"
        llm = MockLLM(
            probe_text=tech_lead_probe,
        )
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=3,
            text_content="Tôi đã bọc critical section bằng asyncio.Lock() để tránh race condition.",
            telemetry={
                "is_coding_turn": True,
                "code_diff": "+ async with self._lock:\n+     self.counter += 1",
                "test_passed": True,
            },
        )
        session_state = {
            "current_stage": InterviewStage.CHALLENGE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-warmup-1", "q-val-1"],
            "current_turn_in_question": 0,
            "current_question_context": {
                "question_id": "q-coding-1",
                "stage": "CHALLENGE",
                "main_prompt": "Sửa lỗi race condition trong MetricsCollector.",
                "rubric_criteria": "Sử dụng Lock hoặc Semaphore.",
            },
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.PROBE)
        self.assertIn("Lock", output.message_text)
        self.assertEqual(output.metadata.get("pre_score"), 8.5)

    def test_is_yes_no_question_patterns(self):
        """10. Nhận diện chính xác câu hỏi Yes/No vs câu hỏi tự luận mở."""
        from src.modules.interviews.core.interview_engine import is_yes_no_question

        self.assertTrue(is_yes_no_question("Bạn đã từng làm việc trực tiếp với Kubernetes trên môi trường Production chưa?"))
        self.assertTrue(is_yes_no_question("Bạn có kinh nghiệm deploy Redis Cluster không?"))
        self.assertTrue(is_yes_no_question("Có phải bạn từng tối ưu hóa database không?"))
        self.assertTrue(is_yes_no_question("Have you worked with Docker Swarm before?"))
        self.assertTrue(is_yes_no_question("Did you setup the CI/CD pipeline?"))

        self.assertFalse(is_yes_no_question("Data drift là gì?"))
        self.assertFalse(is_yes_no_question("Hãy giải thích cơ chế B-tree index trong PostgreSQL."))
        self.assertFalse(is_yes_no_question("Theo bạn sự khác biệt giữa threading và multiprocessing là gì?"))

    def test_classify_candidate_intent_distinguishes_yes_no_from_give_up(self):
        """11. Phân loại ý định: 'Không'/'Chưa' cho câu hỏi Yes/No là VALID_ANSWER, cho câu hỏi lý thuyết là GIVE_UP."""
        from src.modules.interviews.core.interview_engine import classify_candidate_intent

        yes_no_q = "Bạn đã từng làm việc với Kubernetes trên Production chưa?"
        open_q = "Giải thích cơ chế B-tree index trong database?"

        # Yes/No question
        self.assertEqual(classify_candidate_intent("Không", yes_no_q), "VALID_ANSWER")
        self.assertEqual(classify_candidate_intent("Chưa", yes_no_q), "VALID_ANSWER")
        self.assertEqual(classify_candidate_intent("chưa từng", yes_no_q), "VALID_ANSWER")
        self.assertEqual(classify_candidate_intent("dạ chưa", yes_no_q), "VALID_ANSWER")
        self.assertEqual(classify_candidate_intent("Không, em mới chỉ chạy trên minikube local thôi", yes_no_q), "VALID_ANSWER")

        # Open theoretical question
        self.assertEqual(classify_candidate_intent("Không", open_q), "GIVE_UP")
        self.assertEqual(classify_candidate_intent("Chưa", open_q), "GIVE_UP")
        self.assertEqual(classify_candidate_intent("ừm", open_q), "GIVE_UP")
        self.assertEqual(classify_candidate_intent("em chịu", open_q), "GIVE_UP")
        self.assertEqual(classify_candidate_intent("ko biết", open_q), "GIVE_UP")

        # Edge cases: Voluntary Abort & Skip Request
        self.assertEqual(
            classify_candidate_intent("Xin lỗi anh, em có việc đột xuất gia đình xin phép dừng phỏng vấn ạ"),
            "CANDIDATE_ABORT"
        )
        self.assertEqual(
            classify_candidate_intent("em có việc bận gia đình xin dừng lại đây"),
            "CANDIDATE_ABORT"
        )
        self.assertTrue(is_abort_request("em có việc bận gia đình xin dừng lại đây"))
        self.assertTrue(is_abort_request("em có việc bận xin dừng"))
        self.assertTrue(is_abort_request("dừng lại đây thôi"))
        self.assertTrue(is_abort_request("I have an emergency, please stop the interview"))
        self.assertTrue(is_skip_request("câu này em chưa làm qua câu khác nhé"))
        self.assertTrue(is_skip_request("skip this question"))
        self.assertEqual(
            classify_candidate_intent("Câu này em chưa làm thực tế, anh cho em xin đổi sang câu hỏi khác nhé"),
            "SKIP_REQUEST"
        )

    async def test_yes_no_honest_answer_does_not_trigger_early_exit(self):
        """12. Ứng viên trả lời 'Không' chân thật cho câu hỏi Yes/No không bị tính bỏ cuộc."""
        llm = MockLLM(eval_result={"score": 6.0, "is_sufficient": True, "acknowledgement": "Mình đã ghi nhận."})
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=1,
            text_content="Không",
        )
        session_state = {
            "current_stage": InterviewStage.VALIDATE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "consecutive_fails": 0,
            "allow_early_exit": True,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-val-1"],
            "current_turn_in_question": 0,
            "current_question_context": {
                "question_id": "q-val-1",
                "main_prompt": "Bạn đã từng trực tiếp cấu hình Kubernetes trên Production chưa?",
            },
        }

        output = await engine.handle_turn(turn_input, session_state)

        # Không bị terminate
        self.assertNotEqual(output.action, TurnAction.TERMINATE)
        self.assertEqual(output.metadata.get("consecutive_fails"), 0)

    async def test_voluntary_abort_triggers_confirm_abort_modal(self):
        """13. Ứng viên xin phép dừng phỏng vấn vì lý do cá nhân -> Kích hoạt CONFIRM_ABORT modal."""
        engine = InterviewCoreEngine(llm_client=MockLLM())

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=1,
            text_content="Em xin lỗi, em có việc đột xuất gia đình gấp nên xin phép dừng phỏng vấn tại đây ạ.",
        )
        session_state = {
            "current_stage": InterviewStage.DEEP_DIVE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "consecutive_fails": 0,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-warmup-1"],
            "current_turn_in_question": 0,
            "current_question_context": {"main_prompt": "Phân tích Redis clustering..."},
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.CONFIRM_ABORT)
        self.assertFalse(output.is_session_finished)
        self.assertIn("hộp thoại xác nhận kết thúc", output.message_text)
        self.assertTrue(output.metadata.get("requires_abort_confirmation"))

    async def test_skip_question_request_advances_to_next_topic(self):
        """14. Ứng viên xin đổi câu hỏi -> Chuyển sang chủ đề tiếp theo lịch sự."""
        engine = InterviewCoreEngine(llm_client=MockLLM())

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=1,
            text_content="Phần này em chưa có kinh nghiệm thực tế, anh có thể cho em xin đổi sang câu hỏi khác được không ạ?",
        )
        session_state = {
            "current_stage": InterviewStage.VALIDATE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "consecutive_fails": 0,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-val-1"],
            "current_turn_in_question": 0,
            "current_question_context": {"main_prompt": "Chi tiết triển khai..."},
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.NEXT_QUESTION)
        self.assertIn("Được chứ, không sao cả", output.message_text)

    async def test_warmup_turn_0_candidate_abort_triggers_confirm_abort_modal(self):
        """15. Turn 0 (WARM_UP): Ứng viên gõ 'em có việc bận gia đình xin dừng lại đây' -> Kích hoạt CONFIRM_ABORT modal, không chuyển sang Validate."""
        engine = InterviewCoreEngine(llm_client=MockLLM())

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=0,
            text_content="em có việc bận gia đình xin dừng lại đây",
        )
        session_state = {
            "current_stage": InterviewStage.WARM_UP.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "consecutive_fails": 0,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-warmup-1"],
            "current_turn_in_question": 0,
            "current_question_context": {"main_prompt": "Hãy giới thiệu về bản thân bạn."},
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.CONFIRM_ABORT)
        self.assertFalse(output.is_session_finished)
        self.assertIn("hộp thoại xác nhận kết thúc", output.message_text)
        self.assertNotIn("IVORA", output.message_text)
        self.assertTrue(output.metadata.get("requires_abort_confirmation"))

    async def test_llm_layer_2_intent_abort_triggers_confirm_abort_modal(self):
        """16. Bảo hiểm tầng 2: LLM Evaluator phát hiện intent CANDIDATE_ABORT -> Kích hoạt CONFIRM_ABORT modal."""
        llm = MockLLM(eval_result={"intent": "CANDIDATE_ABORT", "score": 1.0, "is_sufficient": False, "acknowledgement": "Ghi nhận."})
        engine = InterviewCoreEngine(llm_client=llm)

        turn_input = CandidateTurnInput(
            session_id=self.session_id,
            turn_index=1,
            text_content="Dạ em có lý do tế nhị không tiện nói tiếp nữa, xin phép ban giám khảo.",
        )
        session_state = {
            "current_stage": InterviewStage.DEEP_DIVE.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "consecutive_fails": 0,
            "questions_pool": self.sample_questions_pool,
            "asked_question_ids": ["q-deep-1"],
            "current_turn_in_question": 0,
            "current_question_context": {"main_prompt": "Làm thế nào để handle 10,000 req/sec với Postgres?"},
        }

        output = await engine.handle_turn(turn_input, session_state)

        self.assertEqual(output.action, TurnAction.CONFIRM_ABORT)
        self.assertFalse(output.is_session_finished)
        self.assertIn("hộp thoại xác nhận kết thúc", output.message_text)
        self.assertTrue(output.metadata.get("requires_abort_confirmation"))


if __name__ == "__main__":
    unittest.main()
