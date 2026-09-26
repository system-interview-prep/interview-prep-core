# src/modules/interviews/evaluation/evaluation_engine.py
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.modules.ai.facade import generate_text
from src.modules.interviews.evaluation.evaluation_types import (
    CompetencyScore,
    DecisionRecommendation,
    SessionEvaluationResult,
    StarAnalysis,
    TurnEvaluationInput,
    TurnEvaluationResult,
)

logger = logging.getLogger("InterviewEvaluationEngine")


class InterviewEvaluationEngine:
    """
    P4 Evaluation Engine:
    - Bóc tách cấu trúc STAR (Situation, Task, Action, Result).
    - So khớp với Rubric tiêu chí và neo điểm chuẩn (Anchor Rubric).
    - Bảo đảm công bằng với hiện tượng chêm từ tiếng Anh chuyên ngành (Code-switching).
    - Trích xuất trích dẫn nguyên văn (Evidence-based Auditing).
    - Tổng hợp điểm theo competency và phân ngưỡng quyết định (STRONG_PASS, PASS, CONSIDER, REJECT).
    - Sinh báo cáo 2 chiều: Recruiter View và Candidate Coaching Feedback.
    """

    def __init__(self, llm_client=None, ai_generator=None):
        self.llm = llm_client
        self._generate_fn = ai_generator or generate_text

    def get_turn_evaluation_prompt(self, turn_input: TurnEvaluationInput, is_vi: bool = True) -> tuple[str, str]:
        """Tạo prompt chuẩn hóa cho Turn Evaluation, bao gồm chỉ thị Code-switching và Anchor Rubric."""
        instructions = (
            "Bạn là chuyên gia thẩm định và đánh giá phỏng vấn nhân sự cấp cao.\n"
            "Nhiệm vụ: Phân tích câu trả lời của ứng viên cho câu hỏi phỏng vấn theo khung năng lực và phương pháp STAR.\n\n"
            "NGUYÊN TẮC ĐÁNH GIÁ QUAN TRỌNG:\n"
            "1. Code-switching fairness: Tuyệt đối không trừ điểm hoặc đánh giá tiêu cực khi ứng viên sử dụng thuật ngữ tiếng Anh chuyên ngành công nghệ (Deploy, Cache, Redis, Kubernetes, Sharding, Microservices, Scale...).\n"
            "2. Anchor Rubric Calibration: Tránh xu hướng chấm điểm an toàn (7-8 điểm). Hãy sử dụng đầy đủ thang điểm từ 0.0 đến 10.0:\n"
            "   - 9.0 - 10.0 (Xuất sắc): Trả lời đúng trọng tâm, giải pháp vượt trội, cấu trúc STAR hoàn chỉnh, có số liệu minh chứng đo lường thực tế.\n"
            "   - 7.0 - 8.9 (Đạt yêu cầu): Nắm vững kiến thức, giải pháp rõ ràng, có kinh nghiệm thực tế nhưng chưa có số liệu đo lường sâu.\n"
            "   - 5.0 - 6.9 (Cần cân nhắc): Trả lời chung chung, thiếu cấu trúc STAR, vai trò cá nhân chưa rõ ràng.\n"
            "   - 0.0 - 4.9 (Không đạt): Sai lệch kiến thức cơ bản, né tránh câu hỏi hoặc không trả lời được.\n"
            "3. STAR Extraction: Trích xuất chính xác 4 cấu phần: Situation, Task, Action, Result. Nếu thiếu Result hoặc Action, đánh dấu is_star_complete = false.\n"
            "4. Evidence Quotes: Trích dẫn nguyên văn ít nhất 1-3 câu nói then chốt của ứng viên chứng minh cho điểm số đã chấm.\n"
            "5. Miễn trừ điểm kỹ thuật cho phần Khởi động (WARM_UP): Nếu câu hỏi là chào hỏi / giới thiệu bản thân ban đầu, chỉ đánh giá sự tự tin, rành mạch và tác phong giao tiếp. Tuyệt đối không đòi hỏi giải pháp công nghệ phức tạp và không áp dụng tiêu chí kỹ thuật hóc búa cho lượt này.\n\n"
            "Định dạng phản hồi: Bắt buộc trả về đúng 1 JSON duy nhất với cấu trúc:\n"
            "{\n"
            '  "score": <float từ 0.0 đến 10.0>,\n'
            '  "star_analysis": {\n'
            '    "situation": "<bối cảnh>",\n'
            '    "task": "<nhiệm vụ>",\n'
            '    "action": "<hành động của ứng viên>",\n'
            '    "result": "<kết quả đạt được>",\n'
            '    "is_star_complete": <true/false>\n'
            "  },\n"
            '  "evidence_quotes": ["<trích dẫn 1>", "<trích dẫn 2>"],\n'
            '  "feedback": "<nhận xét tổng quan cho câu trả lời này>",\n'
            '  "strengths": ["<điểm mạnh 1>", "<điểm mạnh 2>"],\n'
            '  "weaknesses": ["<điểm cần cải thiện 1>"]\n'
            "}"
        )

        user_content = (
            f"Chủ đề năng lực (Competency): {turn_input.competency}\n"
            f"Câu hỏi phỏng vấn: {turn_input.question_prompt}\n"
            f"Tiêu chí đánh giá (Rubric Criteria): {turn_input.rubric_criteria or 'Đánh giá kiến thức thực tế và giải pháp kỹ thuật'}\n\n"
            f"Câu trả lời của ứng viên:\n<candidate_response>\n{turn_input.candidate_answer}\n</candidate_response>"
        )

        return instructions, user_content

    @staticmethod
    def _clean_and_parse_json(raw: str) -> dict:
        """Làm sạch và bóc tách JSON an toàn, hỗ trợ Markdown fences và sửa unclosed quotes."""
        if not raw or not raw.strip():
            return {}
        text = raw.strip()
        # Loại bỏ markdown code blocks
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Thử parse chuẩn
        try:
            return json.loads(text)
        except Exception:
            pass

        # Tìm cụm { ... } ngoài cùng
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass

        # Thử đóng ngoặc cho JSON bị ngắt giữa chừng
        if start != -1:
            truncated = text[start:]
            for suffix in ['"}]}', '"}', '"]}', '}']:
                try:
                    return json.loads(truncated + suffix)
                except Exception:
                    continue

        return {}

    async def evaluate_turn(self, turn_input: TurnEvaluationInput, is_vi: bool = True) -> TurnEvaluationResult:
        """Đánh giá chi tiết một lượt trả lời của ứng viên."""
        instructions, user_content = self.get_turn_evaluation_prompt(turn_input, is_vi=is_vi)

        try:
            if hasattr(self.llm, "generate_json"):
                data = await self.llm.generate_json(system_prompt=instructions, user_content=user_content)
                if not isinstance(data, dict):
                    data = self._clean_and_parse_json(str(data))
            elif hasattr(self.llm, "generate_text"):
                raw = await self.llm.generate_text(system_prompt=instructions, user_content=user_content)
                data = self._clean_and_parse_json(raw)
            else:
                raw = await self._generate_fn(
                    instructions=instructions,
                    input_text=user_content,
                    max_output_tokens=2500,
                    temperature=0.1,
                )
                data = self._clean_and_parse_json(raw)

            star_data = data.get("star_analysis") or {}
            star_analysis = StarAnalysis(
                situation=str(star_data.get("situation", "")),
                task=str(star_data.get("task", "")),
                action=str(star_data.get("action", "")),
                result=str(star_data.get("result", "")),
                is_star_complete=bool(star_data.get("is_star_complete", False)),
            )

            score = float(data.get("score", 5.0))
            score = max(0.0, min(10.0, score))

            return TurnEvaluationResult(
                turn_id=turn_input.turn_id,
                score=score,
                star_analysis=star_analysis,
                evidence_quotes=[str(q) for q in data.get("evidence_quotes", [])],
                feedback=str(data.get("feedback", "Câu trả lời đã được ghi nhận.")),
                strengths=[str(s) for s in data.get("strengths", [])],
                weaknesses=[str(w) for w in data.get("weaknesses", [])],
            )
        except Exception as exc:
            logger.warning(f"Fallback turn evaluation used due to: {exc}")
            return TurnEvaluationResult(
                turn_id=turn_input.turn_id,
                score=5.0,
                star_analysis=StarAnalysis(
                    situation="",
                    task="",
                    action="",
                    result="",
                    is_star_complete=False,
                ),
                evidence_quotes=[],
                feedback="Không thể phân tích dữ liệu đánh giá chi tiết cho lượt này.",
                strengths=[],
                weaknesses=["Câu trả lời chưa đủ rõ ràng để trích xuất cấu trúc STAR."],
            )

    def calculate_decision_recommendation(self, score: float) -> DecisionRecommendation:
        """Phân loại quyết định tuyển dụng dựa trên ngưỡng điểm chuẩn."""
        if score >= 8.5:
            return DecisionRecommendation.STRONG_PASS
        if score >= 7.0:
            return DecisionRecommendation.PASS
        if score >= 5.0:
            return DecisionRecommendation.CONSIDER
        return DecisionRecommendation.REJECT

    def compute_competency_scores(
        self,
        turn_evaluations: List[TurnEvaluationResult],
        turns_input: List[TurnEvaluationInput],
    ) -> List[CompetencyScore]:
        """Tổng hợp điểm theo từng competency để vẽ Radar Chart."""
        turn_map = {ti.turn_id: ti for ti in turns_input}
        comp_scores: Dict[str, List[float]] = {}
        comp_weights: Dict[str, float] = {}

        for te in turn_evaluations:
            ti = turn_map.get(te.turn_id)
            comp_name = ti.competency if ti else "Chuyên môn"
            weight = ti.weight if ti else 1.0

            if comp_name not in comp_scores:
                comp_scores[comp_name] = []
                comp_weights[comp_name] = weight
            comp_scores[comp_name].append(te.score)

        result: List[CompetencyScore] = []
        for comp_name, scores in comp_scores.items():
            avg_score = round(sum(scores) / len(scores), 2)
            result.append(
                CompetencyScore(
                    competency=comp_name,
                    score=avg_score,
                    weight=comp_weights.get(comp_name, 1.0),
                )
            )

        return result

    async def aggregate_session_evaluation(
        self,
        session_id: str,
        turn_evaluations: List[TurnEvaluationResult],
        turns_input: List[TurnEvaluationInput],
        is_vi: bool = True,
    ) -> SessionEvaluationResult:
        """Tổng hợp kết quả toàn phiên, tính điểm tổng thể và sinh báo cáo 2 chiều."""
        if not turn_evaluations:
            return SessionEvaluationResult(
                session_id=session_id,
                overall_score=0.0,
                decision_recommendation=DecisionRecommendation.REJECT,
                competency_scores=[],
                turn_evaluations=[],
                recruiter_summary="Phiên phỏng vấn không có lượt câu hỏi nào được hoàn thành.",
                candidate_feedback="Chưa có đủ dữ liệu để đưa ra nhận xét chi tiết.",
                next_round_topics=[],
                red_flags=["Phiên phỏng vấn chưa hoàn tất câu hỏi."],
                evaluated_at=datetime.now(timezone.utc),
            )

        # 1. Tính toán điểm competency và điểm tổng thể
        competency_scores = self.compute_competency_scores(turn_evaluations, turns_input)

        # Weighted overall score (miễn trừ các competency có trọng số <= 0 như WARM_UP)
        technical_comps = [cs for cs in competency_scores if cs.weight > 0]
        if not technical_comps:
            technical_comps = competency_scores
        total_weight = sum(cs.weight for cs in technical_comps) or 1.0
        weighted_sum = sum(cs.score * cs.weight for cs in technical_comps)
        overall_score = round(weighted_sum / total_weight, 2)
        decision = self.calculate_decision_recommendation(overall_score)

        # 2. Gom dữ liệu điểm mạnh, điểm yếu và bằng chứng
        all_strengths: List[str] = []
        all_weaknesses: List[str] = []
        all_quotes: List[str] = []

        for te in turn_evaluations:
            all_strengths.extend(te.strengths)
            all_weaknesses.extend(te.weaknesses)
            all_quotes.extend(te.evidence_quotes)

        # 3. Sinh báo cáo kép (Recruiter Summary & Candidate Coaching)
        instructions = (
            "Bạn là chuyên gia tư vấn tuyển dụng và đánh giá năng lực.\n"
            "Dựa trên bảng điểm và dữ liệu phỏng vấn được cung cấp, hãy tổng hợp báo cáo 2 chiều dạng JSON:\n"
            "1. 'recruiter_summary': Báo cáo ngắn gọn (2-3 đoạn) cho Nhà tuyển dụng, đánh giá đúng năng lực thực tế, ưu điểm nổi trội và rủi ro nếu tuyển.\n"
            "2. 'candidate_feedback': Nhận xét mang tính huấn luyện (Coaching) cho Ứng viên, chỉ ra điểm làm tốt và hành động cụ thể để cải thiện kỹ năng.\n"
            "3. 'next_round_topics': Danh sách 2-4 chủ đề kỹ thuật cần phỏng vấn sâu hơn ở vòng sau.\n"
            "4. 'red_flags': Danh sách các cảnh báo rủi ro hoặc lỗ hổng kiến thức nghiêm trọng (nếu có, để trống nếu không có).\n\n"
            "Trả về đúng JSON duy nhất với 4 keys trên."
        )

        user_content = (
            f"Điểm tổng thể: {overall_score}/10.0 ({decision.value})\n"
            f"Điểm kỹ năng: {[f'{c.competency}: {c.score}' for c in competency_scores]}\n"
            f"Điểm mạnh ghi nhận: {all_strengths[:6]}\n"
            f"Điểm cần cải thiện: {all_weaknesses[:6]}\n"
            f"Bằng chứng phát biểu: {all_quotes[:4]}"
        )

        try:
            if hasattr(self.llm, "generate_json"):
                data = await self.llm.generate_json(system_prompt=instructions, user_content=user_content)
                if not isinstance(data, dict):
                    data = self._clean_and_parse_json(str(data))
            elif hasattr(self.llm, "generate_text"):
                raw = await self.llm.generate_text(system_prompt=instructions, user_content=user_content)
                data = self._clean_and_parse_json(raw)
            else:
                raw = await self._generate_fn(
                    instructions=instructions,
                    input_text=user_content,
                    max_output_tokens=2500,
                    temperature=0.2,
                )
                data = self._clean_and_parse_json(raw)

            recruiter_summary = str(data.get("recruiter_summary", "")).strip()
            candidate_feedback = str(data.get("candidate_feedback", "")).strip()
            next_topics = [str(t) for t in data.get("next_round_topics", [])]
            red_flags = [str(r) for r in data.get("red_flags", [])]
        except Exception as exc:
            logger.warning(f"Fallback synthesis used due to: {exc}")
            recruiter_summary = (
                f"Ứng viên đạt kết quả {overall_score}/10.0, được xếp loại {decision.value}. "
                f"Thể hiện năng lực nổi bật ở các kỹ năng: {', '.join([c.competency for c in competency_scores if c.score >= 7.0]) or 'Chưa rõ'}. "
                f"Cần cân nhắc rà soát thêm ở vòng phỏng vấn kỹ thuật trực tiếp."
            )
            candidate_feedback = (
                f"Bạn đã hoàn thành phiên phỏng vấn với điểm số {overall_score}/10.0. "
                "Điểm mạnh của bạn là trình bày rõ ràng kinh nghiệm thực tế. "
                "Để đạt kết quả cao hơn, bạn nên bổ sung thêm các số liệu đo lường cụ thể cho phần kết quả (Result) theo mô hình STAR."
            )
            next_topics = [c.competency for c in competency_scores if c.score < 7.0] or ["System Design", "Tối ưu hiệu năng"]
            red_flags = [] if overall_score >= 5.0 else ["Kiến thức nền tảng chưa vững ở các chủ đề cốt lõi."]

        return SessionEvaluationResult(
            session_id=session_id,
            overall_score=overall_score,
            decision_recommendation=decision,
            competency_scores=competency_scores,
            turn_evaluations=turn_evaluations,
            recruiter_summary=recruiter_summary,
            candidate_feedback=candidate_feedback,
            next_round_topics=next_topics,
            red_flags=red_flags,
            evaluated_at=datetime.now(timezone.utc),
        )

    async def evaluate_session(
        self,
        session_id: str,
        turns_input: List[TurnEvaluationInput],
        is_vi: bool = True,
    ) -> SessionEvaluationResult:
        """Đánh giá toàn diện toàn bộ phiên phỏng vấn từ danh sách các lượt hỏi đáp."""
        turn_evaluations: List[TurnEvaluationResult] = []
        for ti in turns_input:
            te = await self.evaluate_turn(ti, is_vi=is_vi)
            turn_evaluations.append(te)

        return await self.aggregate_session_evaluation(
            session_id=session_id,
            turn_evaluations=turn_evaluations,
            turns_input=turns_input,
            is_vi=is_vi,
        )
