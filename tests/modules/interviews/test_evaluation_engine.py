# tests/modules/interviews/test_evaluation_engine.py
import json
import unittest
from unittest.mock import AsyncMock

from src.modules.interviews.evaluation.evaluation_engine import InterviewEvaluationEngine
from src.modules.interviews.evaluation.evaluation_types import (
    CompetencyScore,
    DecisionRecommendation,
    StarAnalysis,
    TurnEvaluationInput,
    TurnEvaluationResult,
)


class MockLLM:
    def __init__(self, json_payload=None, text_payload=None):
        self.json_payload = json_payload
        self.text_payload = text_payload

    async def generate_json(self, system_prompt: str, user_content: str):
        if self.json_payload is not None:
            return self.json_payload
        if self.text_payload is not None:
            return json.loads(self.text_payload)
        return {}

    async def generate_text(self, system_prompt: str, user_content: str):
        if self.text_payload is not None:
            return self.text_payload
        if self.json_payload is not None:
            return json.dumps(self.json_payload)
        return "{}"


class TestInterviewEvaluationEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sample_turn_input = TurnEvaluationInput(
            turn_id="turn-1",
            turn_index=0,
            competency="PostgreSQL Optimization",
            question_prompt="Hãy chia sẻ về một lần bạn tối ưu truy vấn cơ sở dữ liệu chậm trong dự án thực tế.",
            rubric_criteria="Đánh giá khả năng phân tích EXPLAIN ANALYZE, đánh chỉ mục Index và giải pháp phân vùng Partitioning.",
            candidate_answer=(
                "Ở dự án trước, API tra cứu lịch sử đơn hàng bị timeout 504 khi dữ liệu đạt 10 triệu bản ghi. "
                "Tôi đã dùng EXPLAIN ANALYZE để phân tích và phát hiện Seq Scan trên bảng orders. "
                "Sau đó, tôi tạo Composite Index (user_id, created_at) và cấu hình Partitioning theo tháng. "
                "Kết quả là thời gian phản hồi giảm từ 4.2s xuống còn 45ms, throughput tăng gấp 8 lần."
            ),
            weight=1.0,
        )

    async def test_evaluate_turn_star_extraction_success(self):
        """1. Kiểm tra trích xuất thành công 4 thành phần STAR và chứng cứ trích dẫn."""
        mock_response = {
            "score": 9.2,
            "star_analysis": {
                "situation": "Hệ thống tra cứu đơn hàng bị timeout khi dữ liệu vượt 10 triệu bản ghi.",
                "task": "Tối ưu câu truy vấn đang bị Seq Scan để hạ response time.",
                "action": "Dùng EXPLAIN ANALYZE, tạo Composite Index và phân vùng dữ liệu theo tháng.",
                "result": "Response time giảm từ 4.2s xuống 45ms, throughput tăng 8 lần.",
                "is_star_complete": True,
            },
            "evidence_quotes": [
                "API tra cứu lịch sử đơn hàng bị timeout 504 khi dữ liệu đạt 10 triệu bản ghi",
                "thời gian phản hồi giảm từ 4.2s xuống còn 45ms, throughput tăng gấp 8 lần",
            ],
            "feedback": "Ứng viên nắm rất vững phương pháp tối ưu PostgreSQL và có số liệu định lượng rõ ràng.",
            "strengths": ["Thành thạo EXPLAIN ANALYZE", "Tư duy tối ưu có số liệu đo lường cụ thể"],
            "weaknesses": [],
        }

        llm = MockLLM(json_payload=mock_response)
        engine = InterviewEvaluationEngine(llm_client=llm)

        result = await engine.evaluate_turn(self.sample_turn_input)

        self.assertEqual(result.turn_id, "turn-1")
        self.assertEqual(result.score, 9.2)
        self.assertTrue(result.star_analysis.is_star_complete)
        self.assertEqual(result.star_analysis.result, "Response time giảm từ 4.2s xuống 45ms, throughput tăng 8 lần.")
        self.assertEqual(len(result.evidence_quotes), 2)
        self.assertIn("45ms", result.evidence_quotes[1])
        self.assertIn("Thành thạo EXPLAIN ANALYZE", result.strengths)

    def test_code_switching_fairness_instruction_in_prompt(self):
        """2. Kiểm tra System Prompt bắt buộc chứa chỉ thị công bằng với hiện tượng chêm từ tiếng Anh (Code-switching)."""
        engine = InterviewEvaluationEngine()
        instructions, user_content = engine.get_turn_evaluation_prompt(self.sample_turn_input)

        # Kiểm tra chỉ thị code-switching và bảo toàn thuật ngữ kỹ thuật
        self.assertIn("Code-switching fairness", instructions)
        self.assertIn("không trừ điểm", instructions)
        self.assertIn("Deploy", instructions)
        self.assertIn("Redis", instructions)
        self.assertIn("Kubernetes", instructions)
        self.assertIn("Microservices", instructions)

    async def test_evaluate_turn_fallback_on_invalid_json(self):
        """3. Kiểm tra cơ chế Fallback an toàn khi LLM trả về dữ liệu lỗi / JSON không hợp lệ."""
        mock_llm = MockLLM(text_payload="Invalid non-json output from LLM...")
        engine = InterviewEvaluationEngine(llm_client=mock_llm)

        result = await engine.evaluate_turn(self.sample_turn_input)

        # Engine không được quăng exception mà phải trả về fallback object an toàn
        self.assertEqual(result.turn_id, "turn-1")
        self.assertEqual(result.score, 5.0)
        self.assertFalse(result.star_analysis.is_star_complete)
        self.assertIn("Không thể phân tích dữ liệu", result.feedback)

    async def test_aggregate_session_scoring_and_radar_chart(self):
        """4. Kiểm tra tổng hợp điểm theo Competency phục vụ vẽ Radar Chart trên Frontend."""
        engine = InterviewEvaluationEngine()

        turn_inputs = [
            TurnEvaluationInput(
                turn_id="t1",
                turn_index=0,
                competency="PostgreSQL",
                question_prompt="Q1",
                candidate_answer="A1",
                weight=1.0,
            ),
            TurnEvaluationInput(
                turn_id="t2",
                turn_index=1,
                competency="PostgreSQL",
                question_prompt="Q2",
                candidate_answer="A2",
                weight=1.0,
            ),
            TurnEvaluationInput(
                turn_id="t3",
                turn_index=2,
                competency="System Design",
                question_prompt="Q3",
                candidate_answer="A3",
                weight=2.0,
            ),
        ]

        turn_evaluations = [
            TurnEvaluationResult(turn_id="t1", score=8.0),
            TurnEvaluationResult(turn_id="t2", score=9.0),
            TurnEvaluationResult(turn_id="t3", score=7.0),
        ]

        comp_scores = engine.compute_competency_scores(turn_evaluations, turn_inputs)
        comp_map = {c.competency: c.score for c in comp_scores}

        # PostgreSQL: (8.0 + 9.0) / 2 = 8.5
        self.assertEqual(comp_map["PostgreSQL"], 8.5)
        # System Design: 7.0
        self.assertEqual(comp_map["System Design"], 7.0)

    def test_decision_thresholds(self):
        """5. Kiểm tra phân loại ngưỡng quyết định (STRONG_PASS, PASS, CONSIDER, REJECT)."""
        engine = InterviewEvaluationEngine()

        self.assertEqual(engine.calculate_decision_recommendation(9.5), DecisionRecommendation.STRONG_PASS)
        self.assertEqual(engine.calculate_decision_recommendation(8.5), DecisionRecommendation.STRONG_PASS)
        self.assertEqual(engine.calculate_decision_recommendation(7.5), DecisionRecommendation.PASS)
        self.assertEqual(engine.calculate_decision_recommendation(7.0), DecisionRecommendation.PASS)
        self.assertEqual(engine.calculate_decision_recommendation(6.2), DecisionRecommendation.CONSIDER)
        self.assertEqual(engine.calculate_decision_recommendation(5.0), DecisionRecommendation.CONSIDER)
        self.assertEqual(engine.calculate_decision_recommendation(4.9), DecisionRecommendation.REJECT)
        self.assertEqual(engine.calculate_decision_recommendation(2.0), DecisionRecommendation.REJECT)

    async def test_evaluate_session_end_to_end(self):
        """6. Kiểm tra luồng đánh giá toàn phiên End-to-End và báo cáo 2 chiều (Recruiter & Candidate)."""
        turn_inputs = [
            self.sample_turn_input,
            TurnEvaluationInput(
                turn_id="turn-2",
                turn_index=1,
                competency="System Architecture",
                question_prompt="Thiết kế hệ thống chống quá tải (Rate Limiter).",
                candidate_answer="Tôi dùng Redis Token Bucket để giới hạn 100 req/s cho mỗi IP.",
                weight=1.0,
            ),
        ]

        # Giả lập phản hồi đánh giá turn và báo cáo toàn phiên
        llm = MockLLM(
            json_payload={
                "score": 8.0,
                "star_analysis": {"situation": "S", "task": "T", "action": "A", "result": "R", "is_star_complete": True},
                "evidence_quotes": ["Redis Token Bucket 100 req/s"],
                "feedback": "Tốt",
                "strengths": ["Hiểu rõ Token Bucket"],
                "weaknesses": [],
                "recruiter_summary": "Ứng viên có kỹ năng xử lý hệ thống tốt, nắm vững cơ chế tối ưu.",
                "candidate_feedback": "Bạn thể hiện rất tốt phần kỹ thuật, tiếp tục phát huy.",
                "next_round_topics": ["Distributed Caching", "Event-driven"],
                "red_flags": [],
            }
        )

        engine = InterviewEvaluationEngine(llm_client=llm)
        session_result = await engine.evaluate_session(session_id="sess-test-123", turns_input=turn_inputs)

        self.assertEqual(session_result.session_id, "sess-test-123")
        self.assertEqual(session_result.overall_score, 8.0)
        self.assertEqual(session_result.decision_recommendation, DecisionRecommendation.PASS)
        self.assertEqual(len(session_result.turn_evaluations), 2)
        self.assertIn("kỹ năng xử lý hệ thống tốt", session_result.recruiter_summary)
        self.assertIn("tiếp tục phát huy", session_result.candidate_feedback)
        self.assertEqual(len(session_result.next_round_topics), 2)


if __name__ == "__main__":
    unittest.main()
