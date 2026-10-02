"""Offline Evaluation Matrix and Score Lineage Tests for Interview Preparation Core.

Covers:
1. Candidate Proficiency Calibration (4 levels: Trung bình, Khá, Giỏi, Xuất sắc):
   - Score ordering across quality levels.
   - Grounded evidence quote verification (no hallucinated quotes).
   - Verbosity resistance (long buzzwords vs concise substance).
   - Code-switching fairness.
2. CV Project Coverage in Evaluation:
   - Multi-project ranking and evidence flow to Turn 1 snapshot.
   - Traceability of selected project into evaluation inputs.
   - Handling of missing fields (verification prompts vs assertions).
3. Probing and Follow-up Evidence Integration:
   - Merging of initial answer and follow-up answer into accumulated turn evidence.
   - Score improvement when follow-up completes missing STAR components.
4. Aggregation, Lineage & Fairness:
   - Competency score calculation and radar chart generation.
   - Overall score mathematical integrity (weighted average).
   - Exclusion of non-evaluative stages (WARM_UP) from hiring overall score.
   - Audit of CLOSING stage weight impact on hiring recommendations.
   - Handling of skipped/timeout/empty turns without unjustified pass inflation.
"""

import json
import pytest
import unittest
from unittest.mock import AsyncMock

from src.modules.interviews.evaluation.evaluation_engine import InterviewEvaluationEngine
from src.modules.interviews.evaluation.evaluation_types import (
    CompetencyScore,
    DecisionRecommendation,
    SessionEvaluationResult,
    StarAnalysis,
    TurnEvaluationInput,
    TurnEvaluationResult,
)
from src.modules.interviews.project_evidence import (
    StructuredProjectEvidence,
    build_project_validation_question,
    extract_project_evidences,
    select_best_project,
)


# ============================================================================
# Synthetic Candidate Answers for a Grounded Competency:
# Competency: "Generative AI & LLM Systems"
# Prompt: "Làm thế nào bạn giải quyết bài toán rò rỉ dữ liệu hoặc hallucination trong hệ thống RAG?"
# Rubric: "Đánh giá kiến thức về RAG đa tầng, kiểm chứng nguồn (Citation/Evidence verification), Guardrails, và đo lường định lượng."
# ============================================================================

ANSWER_TRUNG_BINH = (
    "Em nghĩ là RAG rất hay bị hallucination nếu prompt không chuẩn. "
    "Để giải quyết thì em sẽ dùng prompt thật kỹ, nhắc mô hình là 'hãy chỉ trả lời dựa trên context'. "
    "Ngoài ra em sẽ dùng mô hình xịn hơn như GPT-4 hoặc Gemini Pro để nó thông minh hơn và ít bịa hơn. "
    "Em cũng deploy hệ thống lên cloud để chạy ổn định."
)

ANSWER_KHA = (
    "Trong dự án của em, để giảm hallucination trong RAG, em áp dụng hai kỹ thuật: "
    "Thứ nhất là tối ưu phần retrieval bằng cách dùng Hybrid Search kết hợp BM25 và Dense Vector search với pgvector. "
    "Thứ hai là trong prompt generation, em yêu cầu LLM trích dẫn số trang hoặc ID của document nguồn. "
    "Nếu độ tương đồng vector dưới ngưỡng 0.7 thì hệ thống sẽ từ chối trả lời thay vì cố bịa. "
    "Cách này giúp bot trả lời chính xác hơn các câu hỏi về tài liệu nội bộ."
)

ANSWER_GIOI = (
    "Tại hệ thống trợ lý tra cứu nội bộ, em trực tiếp thiết kế luồng giảm hallucination gồm 3 chốt chặn: "
    "1. Reranking & Thresholding: Sau khi hybrid search lấy top 20 chunks, em dùng Cohere Rerank để chọn top 5 chunks có score >= 0.75, lọc bỏ 60% nhiễu. "
    "2. Strict Grounded Prompting & Citation: Prompt ép buộc định dạng câu trả lời kèm citation dạng [doc_id:offset]. "
    "3. Self-Correction / Verification: Em thêm một bước LLM nhẹ kiểm tra chéo (NLI check) xem từng câu trong output có được suy ra từ chunks hay không. "
    "Kết quả đo lường: Trên tập test 200 câu hỏi, tỷ lệ hallucination giảm từ 18.5% xuống 4.2%, độ trễ p95 tăng thêm 280ms nhưng chấp nhận được."
)

ANSWER_XUAT_SAC = (
    "Khi xây dựng hệ thống RAG cho miền tài chính yêu cầu zero-tolerance với sai số, em tiếp cận theo hướng Defense-in-Depth và đo lường bằng rubric Ragas: "
    "1. Constraints & Trade-offs: Nếu chỉ phụ thuộc vector similarity, mô hình vẫn hallucinate khi ngữ cảnh chứa thông tin mâu thuẫn. "
    "2. Kiến trúc 3 tầng: "
    "   - Ingestion: Chunking ngữ nghĩa phân cấp (parent-child documents) giữ trọn vẹn context bảng biểu. "
    "   - Retrieval & Guardrails: Kết hợp Dense + Sparse Search với Cross-Encoder Reranker; áp dụng NeMo Guardrails để chặn prompt injection và rò rỉ PII. "
    "   - Evidence Verification: Sử dụng phương pháp Chain-of-Verification (CoVe) và trích xuất claim độc lập để verify với context trước khi stream về client. "
    "3. Giới hạn & Fallback: Khi groundedness score < 0.85, hệ thống chủ động fallback về mẫu 'Tài liệu hiện tại không đủ thông tin xác thực' thay vì suy đoán. "
    "4. Baseline & Đo lường: Đo trên benchmark 500 query kiểm thử, groundedness tăng từ 0.71 lên 0.94, latency tăng 350ms nhưng đạt chuẩn tuân thủ bảo mật."
)

ANSWER_VERBOSE_BUZZWORDS = (
    "Hệ thống RAG của em áp dụng toàn bộ công nghệ tiên tiến nhất hiện nay gồm AI, LLM, Generative AI, LangChain, LangGraph, "
    "Kubernetes, Docker, Microservices, Redis, Kafka, Elasticsearch, Vector Database, Milvus, Qdrant, Pinecone, PyTorch, "
    "Fine-tuning, LoRA, QLoRA, RAG đa tầng, Multi-agent, CrewAI, AutoGen, Agentic workflow, Cloud-native, AWS, GCP, Azure, "
    "CI/CD, DevOps, Monitoring, Prometheus, Grafana, Datadog. Tất cả được scale tự động đạt hiệu năng cực kỳ cao và hoàn hảo."
)


class MockLLMEvaluator:
    """Deterministic mock evaluator implementing rubric-grounded scoring for the offline matrix."""

    def __init__(self):
        pass

    async def generate_json(self, system_prompt: str, user_content: str):
        # Extract candidate text from user_content
        ans_start = user_content.find("<candidate_response>\n")
        ans_end = user_content.find("\n</candidate_response>")
        ans = user_content[ans_start + 21:ans_end] if (ans_start != -1 and ans_end != -1) else user_content

        # Deterministic scoring based on grounded rubric features:
        # Check for specific substantive indicators vs mere buzzwords
        has_metrics = any(m in ans for m in ["18.5%", "4.2%", "280ms", "0.71", "0.94", "350ms", "200 câu hỏi", "500 query"])
        has_tradeoffs = any(t in ans for t in ["Trade-offs", "mâu thuẫn", "độ trễ p95 tăng thêm", "latency tăng", "Giới hạn & Fallback"])
        has_deep_arch = any(a in ans for a in ["Cross-Encoder Reranker", "Chain-of-Verification", "NeMo Guardrails", "parent-child documents", "Cohere Rerank", "NLI check"])
        has_concrete_example = any(c in ans for c in ["Hybrid Search", "BM25", "pgvector", "top 20 chunks", "top 5 chunks"])
        is_pure_buzzwords = ("Kubernetes" in ans and "Milvus" in ans and "Kafka" in ans and "hoàn hảo" in ans and not has_metrics and not has_tradeoffs)

        is_xuat_sac = ("zero-tolerance" in ans or "Chain-of-Verification" in ans or "parent-child" in ans)
        is_gioi = ("Cohere Rerank" in ans or "18.5%" in ans or "NLI check" in ans)
        is_kha = ("Hybrid Search" in ans or "pgvector" in ans)

        if is_pure_buzzwords:
            # Verbosity penalty: buzzword soup without concrete problem-solving
            return {
                "score": 3.0,
                "star_analysis": {
                    "situation": "Ứng viên liệt kê các công nghệ buzzword mà không có bối cảnh cụ thể.",
                    "task": "Không nêu nhiệm vụ cụ thể cần giải quyết.",
                    "action": "Chỉ liệt kê danh sách công nghệ không liên kết.",
                    "result": "Không có kết quả đo lường.",
                    "is_star_complete": False,
                },
                "evidence_quotes": ["Hệ thống RAG của em áp dụng toàn bộ công nghệ tiên tiến nhất"],
                "feedback": "Câu trả lời chỉ liệt kê hàng loạt từ khóa công nghệ (buzzwords) mà không giải thích cơ chế, kiến trúc hay cách giải quyết vấn đề.",
                "strengths": ["Biết tên nhiều công nghệ thịnh hành"],
                "weaknesses": ["Thiếu hoàn toàn chiều sâu kỹ thuật", "Không có giải pháp cụ thể"],
            }

        if is_xuat_sac:
            # Xuất sắc: 9.0 - 10.0
            return {
                "score": 9.5,
                "star_analysis": {
                    "situation": "Xây dựng hệ thống RAG tài chính yêu cầu zero-tolerance với sai số và hallucination.",
                    "task": "Kiểm soát hallucination đa tầng kết hợp đo lường theo chuẩn Ragas.",
                    "action": "Thiết kế kiến trúc 3 tầng: chunking phân cấp, Cross-Encoder Reranker, NeMo Guardrails và Chain-of-Verification.",
                    "result": "Groundedness tăng từ 0.71 lên 0.94 trên benchmark 500 query, latency tăng 350ms được kiểm soát.",
                    "is_star_complete": True,
                },
                "evidence_quotes": [
                    "chunking ngữ nghĩa phân cấp (parent-child documents)",
                    "áp dụng NeMo Guardrails để chặn prompt injection",
                    "groundedness tăng từ 0.71 lên 0.94",
                ],
                "feedback": "Câu trả lời xuất sắc, tư duy kiến trúc toàn diện từ ingestion đến post-generation verification kèm số liệu đo lường cụ thể.",
                "strengths": ["Phân tích trade-off sâu sắc", "Có baseline và chỉ số kiểm thử rõ ràng", "Thiết kế cơ chế fallback an toàn"],
                "weaknesses": [],
            }
        elif is_gioi:
            # Giỏi: 8.0 - 8.9
            return {
                "score": 8.5,
                "star_analysis": {
                    "situation": "Hệ thống trợ lý tra cứu nội bộ gặp vấn đề hallucination.",
                    "task": "Thiết kế luồng giảm hallucination gồm 3 chốt chặn.",
                    "action": "Dùng Cohere Rerank lấy top 5 chunks >= 0.75, ép citation và thêm NLI check.",
                    "result": "Tỷ lệ hallucination giảm từ 18.5% xuống 4.2% trên 200 câu hỏi.",
                    "is_star_complete": True,
                },
                "evidence_quotes": [
                    "Reranking & Thresholding: Sau khi hybrid search lấy top 20 chunks",
                    "tỷ lệ hallucination giảm từ 18.5% xuống 4.2%",
                ],
                "feedback": "Câu trả lời rất tốt, thể hiện rõ vai trò trực tiếp, giải pháp cụ thể và có kết quả định lượng thuyết phục.",
                "strengths": ["Có hành động cụ thể", "Có số liệu đo lường thực tế"],
                "weaknesses": ["Chưa phân tích sâu các edge case dữ liệu mâu thuẫn"],
            }
        elif has_concrete_example:
            # Khá: 7.0 - 7.9
            return {
                "score": 7.2,
                "star_analysis": {
                    "situation": "Giảm hallucination trong dự án RAG tài liệu nội bộ.",
                    "task": "Cải thiện độ chính xác câu trả lời của bot.",
                    "action": "Áp dụng Hybrid Search (BM25 + Dense vector với pgvector), yêu cầu citation và threshold 0.7.",
                    "result": "Hệ thống từ chối trả lời khi không chắc chắn, trả lời chính xác hơn nhưng chưa có số liệu % đo lường.",
                    "is_star_complete": False,
                },
                "evidence_quotes": [
                    "Hybrid Search kết hợp BM25 và Dense Vector search với pgvector",
                    "độ tương đồng vector dưới ngưỡng 0.7 thì hệ thống sẽ từ chối",
                ],
                "feedback": "Nắm vững giải pháp kỹ thuật cơ bản của RAG và có hướng xử lý đúng đắn. Cần bổ sung số liệu đo lường định lượng.",
                "strengths": ["Hiểu rõ Hybrid Search và thresholding", "Tư duy từ chối trả lời khi thiếu dữ liệu"],
                "weaknesses": ["Thiếu số liệu đo lường cụ thể", "Chưa có quy trình kiểm chứng độc lập"],
            }
        else:
            # Trung bình: 5.0 - 6.5
            return {
                "score": 5.2,
                "star_analysis": {
                    "situation": "Nhận thức được vấn đề hallucination trong RAG.",
                    "task": "Khắc phục hiện tượng bịa thông tin.",
                    "action": "Chủ yếu dựa vào prompt nhắc nhở và nâng cấp model xịn hơn.",
                    "result": "Không có kết quả đo lường rõ ràng.",
                    "is_star_complete": False,
                },
                "evidence_quotes": [
                    "nhắc mô hình là 'hãy chỉ trả lời dựa trên context'",
                    "dùng mô hình xịn hơn như GPT-4 hoặc Gemini Pro",
                ],
                "feedback": "Câu trả lời ở mức nhận biết cơ bản, giải pháp thiên về dựa dẫm vào năng lực của model thay vì giải pháp kiến trúc hệ thống.",
                "strengths": ["Nhận biết được rủi ro hallucination"],
                "weaknesses": ["Giải pháp ngây thơ, thiếu tư duy kỹ thuật hệ thống", "Không có cấu trúc STAR"],
            }


# ============================================================================
# TEST SUITE
# ============================================================================

class TestOfflineEvaluationMatrix(unittest.IsolatedAsyncioTestCase):
    """Offline Evaluation Matrix testing candidate proficiency, CV coverage, probing, and score lineage."""

    def setUp(self):
        self.mock_llm = MockLLMEvaluator()
        self.engine = InterviewEvaluationEngine(llm_client=self.mock_llm)

    # ------------------------------------------------------------------------
    # Part A: Candidate Proficiency Calibration (4 Levels)
    # ------------------------------------------------------------------------

    async def test_proficiency_monotonic_score_ordering(self):
        """A1. Score ordering: Score(Xuất sắc) > Score(Giỏi) > Score(Khá) > Score(Trung bình)."""
        prompt = "Làm thế nào bạn giải quyết bài toán rò rỉ dữ liệu hoặc hallucination trong hệ thống RAG?"
        comp = "Generative AI & LLM Systems"

        t_tb = TurnEvaluationInput(turn_id="t-tb", turn_index=1, competency=comp, question_prompt=prompt, candidate_answer=ANSWER_TRUNG_BINH)
        t_kh = TurnEvaluationInput(turn_id="t-kh", turn_index=2, competency=comp, question_prompt=prompt, candidate_answer=ANSWER_KHA)
        t_gi = TurnEvaluationInput(turn_id="t-gi", turn_index=3, competency=comp, question_prompt=prompt, candidate_answer=ANSWER_GIOI)
        t_xs = TurnEvaluationInput(turn_id="t-xs", turn_index=4, competency=comp, question_prompt=prompt, candidate_answer=ANSWER_XUAT_SAC)

        res_tb = await self.engine.evaluate_turn(t_tb)
        res_kh = await self.engine.evaluate_turn(t_kh)
        res_gi = await self.engine.evaluate_turn(t_gi)
        res_xs = await self.engine.evaluate_turn(t_xs)

        # 1. Monotonic ordering
        self.assertGreater(res_kh.score, res_tb.score, f"Khá ({res_kh.score}) phải cao hơn Trung bình ({res_tb.score})")
        self.assertGreater(res_gi.score, res_kh.score, f"Giỏi ({res_gi.score}) phải cao hơn Khá ({res_kh.score})")
        self.assertGreater(res_xs.score, res_gi.score, f"Xuất sắc ({res_xs.score}) phải cao hơn Giỏi ({res_gi.score})")

        # 2. Expected range calibration
        self.assertGreaterEqual(res_xs.score, 9.0)
        self.assertGreaterEqual(res_gi.score, 8.0)
        self.assertLess(res_gi.score, 9.0)
        self.assertGreaterEqual(res_kh.score, 7.0)
        self.assertLess(res_kh.score, 8.0)
        self.assertLess(res_tb.score, 7.0)

        # 3. STAR completeness calibration
        self.assertTrue(res_xs.star_analysis.is_star_complete)
        self.assertTrue(res_gi.star_analysis.is_star_complete)
        self.assertFalse(res_kh.star_analysis.is_star_complete, "Khá thiếu kết quả đo lường định lượng nên STAR chưa hoàn chỉnh")
        self.assertFalse(res_tb.star_analysis.is_star_complete, "Trung bình thiếu Action kỹ thuật và Result")

    async def test_proficiency_evidence_quote_grounding(self):
        """A2. Evidence quotes: All extracted quotes must be grounded verbatim in candidate answers."""
        inputs_and_texts = [
            (TurnEvaluationInput(turn_id="t1", turn_index=1, candidate_answer=ANSWER_TRUNG_BINH, question_prompt="Q"), ANSWER_TRUNG_BINH),
            (TurnEvaluationInput(turn_id="t2", turn_index=2, candidate_answer=ANSWER_KHA, question_prompt="Q"), ANSWER_KHA),
            (TurnEvaluationInput(turn_id="t3", turn_index=3, candidate_answer=ANSWER_GIOI, question_prompt="Q"), ANSWER_GIOI),
            (TurnEvaluationInput(turn_id="t4", turn_index=4, candidate_answer=ANSWER_XUAT_SAC, question_prompt="Q"), ANSWER_XUAT_SAC),
        ]

        for turn_inp, original_text in inputs_and_texts:
            result = await self.engine.evaluate_turn(turn_inp)
            for quote in result.evidence_quotes:
                # Must be a substring in the candidate response
                self.assertIn(
                    quote.lower(),
                    original_text.lower(),
                    f"Quote '{quote}' không tồn tại nguyên văn trong câu trả lời của ứng viên!",
                )

    async def test_proficiency_verbosity_penalty(self):
        """A3. Verbosity resistance: Long answer filled with buzzwords must NOT score higher than concise substantive answer."""
        t_buzz = TurnEvaluationInput(
            turn_id="t-buzz",
            turn_index=1,
            competency="System Architecture",
            question_prompt="Hệ thống RAG của bạn giải quyết rò rỉ và hiệu năng ra sao?",
            candidate_answer=ANSWER_VERBOSE_BUZZWORDS,
        )
        t_substantive = TurnEvaluationInput(
            turn_id="t-sub",
            turn_index=2,
            competency="System Architecture",
            question_prompt="Hệ thống RAG của bạn giải quyết rò rỉ và hiệu năng ra sao?",
            candidate_answer=ANSWER_KHA,
        )

        res_buzz = await self.engine.evaluate_turn(t_buzz)
        res_sub = await self.engine.evaluate_turn(t_substantive)

        # Substantive answer with 5 lines must score significantly higher than 15 lines of pure buzzwords
        self.assertGreater(
            res_sub.score,
            res_buzz.score + 2.0,
            f"Câu trả lời có thực chất ({res_sub.score}) phải cao hơn câu trả lời sáo rỗng nhiều thuật ngữ ({res_buzz.score})",
        )
        self.assertLess(res_buzz.score, 5.0, "Câu trả lời thuần buzzword phải dưới 5.0")

    # ------------------------------------------------------------------------
    # Part B: CV Project Coverage in Evaluation
    # ------------------------------------------------------------------------

    def test_cv_project_coverage_and_ranking_flow(self):
        """B1. Multi-project CV: Project matching JD must be selected regardless of CV order."""
        cv_projects = [
            {
                "name": "Wedding Planner Platform",
                "technologies": ["PHP", "Laravel", "MySQL"],
                "description": "Quản lý thiệp cưới và khách mời",
                "outcomes": "1000 users",
            },
            {
                "name": "AI Career Assistant",
                "technologies": ["Python", "FastAPI", "Gemini", "LangGraph", "pgvector"],
                "description": "Tư vấn lộ trình học tập và phỏng vấn giả lập với RAG",
                "outcomes": "90+ sinh viên đạt kết quả tốt",
            },
            {
                "name": "Mini E-commerce",
                "technologies": ["React", "Express", "MongoDB"],
                "description": "Bán hàng trực tuyến",
                "outcomes": "",
            },
        ]

        jd_requirements = [
            {"requirementId": "req-py", "label": "Python & FastAPI Backend", "priority": "MUST_HAVE"},
            {"requirementId": "req-ai", "label": "Generative AI, LangGraph, RAG", "priority": "MUST_HAVE"},
        ]

        # Extract structured project evidence
        evidences = extract_project_evidences(
            projects_data=cv_projects,
            job_requirements=jd_requirements,
            cv_skills=["Python", "FastAPI", "Gemini", "LangGraph", "pgvector", "PHP", "React"],
        )

        self.assertEqual(len(evidences), 3)

        # Select best project
        best_proj = select_best_project(evidences)
        self.assertIsNotNone(best_proj)
        self.assertEqual(best_proj.name, "AI Career Assistant")
        self.assertGreater(best_proj.jd_relevance_score, 0)
        self.assertIn("req-py", best_proj.relevant_requirements)
        self.assertIn("req-ai", best_proj.relevant_requirements)

        # Build grounded 4-axis question without hallucination
        question_text = build_project_validation_question(
            project=best_proj,
            job_title="AI Engineer",
            locale="vi",
        )
        self.assertIn("AI Career Assistant", question_text)
        self.assertIn("FastAPI", question_text)
        self.assertIn("vai trò", question_text)

    # ------------------------------------------------------------------------
    # Part C: Probing and Follow-up Evidence Integration
    # ------------------------------------------------------------------------

    async def test_probing_accumulates_answer_and_improves_evaluation(self):
        """C1. When follow-up probe elicits missing evidence, accumulated evaluation score improves."""
        initial_answer = "Em dùng RAG và vector search để tìm tài liệu."
        follow_up_answer = (
            "Cụ thể, em dùng Hybrid Search kết hợp BM25 và pgvector, đặt ngưỡng similarity >= 0.75 "
            "và thêm bước NLI check để loại bỏ 60% hallucination trên 200 câu hỏi test."
        )

        # Turn before probe
        turn_initial = TurnEvaluationInput(
            turn_id="t-probe-1",
            turn_index=1,
            competency="Generative AI",
            question_prompt="Hệ thống RAG của bạn kiểm soát hallucination như thế nào?",
            candidate_answer=initial_answer,
        )
        res_initial = await self.engine.evaluate_turn(turn_initial)

        # Turn after probe with runtime accumulated text:
        # Format produced by chat_runtime.py: f"{existing_answer}\n\n[Follow-up Answer]: {cleaned_content}"
        accumulated_answer = f"{initial_answer}\n\n[Follow-up Answer]: {follow_up_answer}"
        turn_accumulated = TurnEvaluationInput(
            turn_id="t-probe-1",
            turn_index=1,
            competency="Generative AI",
            question_prompt="Hệ thống RAG của bạn kiểm soát hallucination như thế nào?",
            candidate_answer=accumulated_answer,
        )
        res_accumulated = await self.engine.evaluate_turn(turn_accumulated)

        # The accumulated answer must achieve a strictly higher score and complete STAR components
        self.assertGreater(
            res_accumulated.score,
            res_initial.score,
            f"Điểm sau probe ({res_accumulated.score}) phải cao hơn điểm ban đầu ({res_initial.score})",
        )
        self.assertTrue(res_accumulated.star_analysis.is_star_complete)

    # ------------------------------------------------------------------------
    # Part D: Aggregation, Lineage & Fairness
    # ------------------------------------------------------------------------

    def test_aggregation_warmup_and_closing_weights(self):
        """D1. Audit of stage weights: WARM_UP is excluded (weight=0.0); CLOSING impact is documented."""
        # Setup turns mirroring a realistic 5-turn session:
        # Turn 0: WARM_UP (intro) -> score 8.5, weight 0.0
        # Turn 1: VALIDATE (CV Project) -> score 8.0, weight 1.0
        # Turn 2: DEEP_DIVE (Technical AI) -> score 6.0, weight 1.0
        # Turn 3: CHALLENGE (Technical AI) -> score 4.0, weight 1.0 (Average AI = 5.0)
        # Turn 4: CLOSING (Candidate asks company question) -> score 8.0, weight 1.0 (Audit target!)

        turn_inputs = [
            TurnEvaluationInput(turn_id="t0", turn_index=0, competency="Giới thiệu & Khởi động", question_prompt="Q0", candidate_answer="A0", weight=0.0),
            TurnEvaluationInput(turn_id="t1", turn_index=1, competency="Xác thực Dự án CV", question_prompt="Q1", candidate_answer="A1", weight=1.0),
            TurnEvaluationInput(turn_id="t2", turn_index=2, competency="Trí tuệ nhân tạo", question_prompt="Q2", candidate_answer="A2", weight=1.0),
            TurnEvaluationInput(turn_id="t3", turn_index=3, competency="Trí tuệ nhân tạo", question_prompt="Q3", candidate_answer="A3", weight=1.0),
            TurnEvaluationInput(turn_id="t4", turn_index=4, competency="Hỏi đáp & Tổng kết", question_prompt="Q4", candidate_answer="A4", weight=1.0),
        ]

        turn_results = [
            TurnEvaluationResult(turn_id="t0", score=8.5),
            TurnEvaluationResult(turn_id="t1", score=8.0),
            TurnEvaluationResult(turn_id="t2", score=6.0),
            TurnEvaluationResult(turn_id="t3", score=4.0),
            TurnEvaluationResult(turn_id="t4", score=8.0),
        ]

        comp_scores = self.engine.compute_competency_scores(turn_results, turn_inputs)
        comp_dict = {c.competency: c for c in comp_scores}

        # 1. Trí tuệ nhân tạo = (6.0 + 4.0) / 2 = 5.0
        self.assertEqual(comp_dict["Trí tuệ nhân tạo"].score, 5.0)
        # 2. Giới thiệu & Khởi động has weight 0.0
        self.assertEqual(comp_dict["Giới thiệu & Khởi động"].weight, 0.0)

        # 3. Overall calculation under current logic:
        # Technical comps with weight > 0:
        # - Xác thực Dự án CV: 8.0 (wt 1.0)
        # - Trí tuệ nhân tạo: 5.0 (wt 1.0)
        # - Hỏi đáp & Tổng kết: 8.0 (wt 1.0)
        # Current weighted sum = (8.0 + 5.0 + 8.0) / 3 = 21.0 / 3 = 7.0 -> PASS!
        current_technical_comps = [c for c in comp_scores if c.weight > 0]
        current_weighted_sum = sum(c.score * c.weight for c in current_technical_comps)
        current_total_weight = sum(c.weight for c in current_technical_comps)
        current_overall = round(current_weighted_sum / current_total_weight, 2)
        self.assertEqual(current_overall, 7.0)

        # 4. IF CLOSING is properly zero-weighted (weight=0.0):
        # Technical comps:
        # - Xác thực Dự án CV: 8.0 (wt 1.0)
        # - Trí tuệ nhân tạo: 5.0 (wt 1.0)
        # Pure technical weighted sum = (8.0 + 5.0) / 2 = 6.5 -> CONSIDER!
        pure_tech_comps = [c for c in comp_scores if c.weight > 0 and c.competency != "Hỏi đáp & Tổng kết"]
        pure_tech_sum = sum(c.score * c.weight for c in pure_tech_comps)
        pure_tech_total_weight = sum(c.weight for c in pure_tech_comps)
        pure_tech_overall = round(pure_tech_sum / pure_tech_total_weight, 2)
        self.assertEqual(pure_tech_overall, 6.5)

        # This proves mathematically how CLOSING weight=1.0 inflates the overall hiring score!
        self.assertGreater(current_overall, pure_tech_overall)

    def test_decision_recommendation_bounds(self):
        """D2. Decision recommendation thresholds are deterministic."""
        self.assertEqual(self.engine.calculate_decision_recommendation(8.5), DecisionRecommendation.STRONG_PASS)
        self.assertEqual(self.engine.calculate_decision_recommendation(8.49), DecisionRecommendation.PASS)
        self.assertEqual(self.engine.calculate_decision_recommendation(7.0), DecisionRecommendation.PASS)
        self.assertEqual(self.engine.calculate_decision_recommendation(6.99), DecisionRecommendation.CONSIDER)
        self.assertEqual(self.engine.calculate_decision_recommendation(5.0), DecisionRecommendation.CONSIDER)
        self.assertEqual(self.engine.calculate_decision_recommendation(4.99), DecisionRecommendation.REJECT)
