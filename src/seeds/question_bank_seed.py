"""Idempotent dev/demo seed for the Question Bank.

Creates eligible (APPROVED + rubric + taxonomy mapping) questions for every
competency currently required by session_competency_targets, so that
POST /api/v1/interviews/sessions/{id}/questions/select succeeds in a local
development environment.

Lifecycle respected:
  InterviewQuestion (stable_key, no retired_at)
  └─ InterviewQuestionVersion (status=APPROVED, current_approved_version_id set)
     ├─ QuestionVersionTaxonomyConcept (PURPOSE=PRIMARY_COMPETENCY)
     ├─ QuestionApproval (one row required by active_question_bank view)
     ├─ QuestionVersionRubric → RubricVersion → RubricCriterion + RubricAnchor
     └─ (QuestionCalibration – optional, status APPROVED is sufficient)

Idempotency: every entity uses a deterministic stable_key / UUID derived from
the question slug so repeated runs are safe (INSERT … ON CONFLICT DO NOTHING).

DO NOT call this from production startup.  Wire it into `run_all_seeds` only
in development / testing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The taxonomy_version used by the JD/CV parsers and stored in
# session_competency_targets.  This MUST match what the selector query uses.
_QUESTION_TAXONOMY_VERSION = "internal-2026.1"

# One shared approval policy version label for all seeded records.
_APPROVAL_POLICY_VERSION = "dev-seed-v1"

# Seeded by admin bootstrap; we reuse the admin subject as the creator /
# approver to avoid needing a real user in the seed.
_SEED_AUTHOR = "system-seed"
_SEED_APPROVER = "system-approver"

# ---------------------------------------------------------------------------
# Canonical question fixtures
# ---------------------------------------------------------------------------
# Each entry produces N questions (len(questions)) for the given concept.
# Keys in each question dict:
#   stable_key  – globally unique slug (also used to derive deterministic UUIDs)
#   text        – canonical_text shown to the interviewee
#   objective   – internal scoring objective
#   difficulty  – foundational | intermediate | advanced
#   type        – question_type string

_CONCEPT_FIXTURES: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ #
    # skill-artificial-intelligence  (target: 3, 5 en-US + 5 vi-VN)       #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-artificial-intelligence",
        "questions": [
            {
                "stable_key": "ai-fundamentals-bias-fairness",
                "text": "Describe what algorithmic bias is in AI systems and explain two concrete techniques you would use to detect and mitigate it.",
                "objective": "Assess understanding of AI bias sources and mitigation strategies such as re-sampling and fairness metrics.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "ai-fundamentals-supervised-vs-unsupervised",
                "text": "Compare supervised and unsupervised learning. Give a concrete real-world use-case for each and explain why the boundary between them matters when labelling data is expensive.",
                "objective": "Assess ability to distinguish learning paradigms and apply cost-of-labelling trade-offs.",
                "difficulty": "foundational",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "ai-fundamentals-overfitting-regularization",
                "text": "Explain overfitting in your own words and walk me through at least three regularisation techniques, comparing when you would choose each.",
                "objective": "Assess depth of understanding of generalisation and regularisation trade-offs (L1/L2/Dropout/Early stopping).",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "ai-fundamentals-evaluation-metrics",
                "text": "A model has 99% accuracy on a fraud-detection dataset where 1% of transactions are fraudulent. Explain why accuracy is misleading here and describe which metrics you would use instead.",
                "objective": "Assess ability to select appropriate evaluation metrics for imbalanced classification tasks.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "ai-fundamentals-model-deployment-drift",
                "text": "You have trained a model that performs well offline but degrades in production after two months. Walk me through how you would diagnose and address model/data drift.",
                "objective": "Assess operational AI knowledge: monitoring, drift detection, retraining pipelines.",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "ai-co-ban-thien-vi-cong-bang",
                "text": "Mô tả hiện tượng thiên vị thuật toán (algorithmic bias) trong các hệ thống AI và giải thích hai kỹ thuật cụ thể bạn sẽ áp dụng để phát hiện và giảm thiểu nó.",
                "objective": "Đánh giá hiểu biết về nguồn gốc thiên vị trong AI và các chiến lược giảm thiểu như tái lấy mẫu và các chỉ số công bằng.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "ai-co-ban-hoc-co-giam-sat-va-khong-giam-sat",
                "text": "So sánh học có giám sát (supervised learning) và học không giám sát (unsupervised learning). Đưa ra ví dụ thực tế cho mỗi loại và giải thích lý do tại sao ranh giới giữa chúng lại quan trọng khi chi phí gán nhãn dữ liệu cao.",
                "objective": "Đánh giá khả năng phân biệt các mô hình học máy và áp dụng đánh đổi chi phí gán nhãn.",
                "difficulty": "foundational",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "ai-co-ban-overfitting-va-regularization",
                "text": "Giải thích hiện tượng overfitting và trình bày ít nhất ba kỹ thuật điều chuẩn (regularization), so sánh khi nào nên chọn từng kỹ thuật.",
                "objective": "Đánh giá mức độ hiểu biết về khả năng tổng quát hóa và điều chuẩn (L1/L2/Dropout/Early stopping).",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "ai-co-ban-cac-chi-so-danh-gia",
                "text": "Một mô hình đạt độ chính xác (accuracy) 99% trên tập dữ liệu phát hiện gian lận trong đó chỉ có 1% giao dịch là gian lận. Giải thích tại sao accuracy lại gây hiểu lầm trong trường hợp này và mô tả những chỉ số nào bạn sẽ sử dụng thay thế.",
                "objective": "Đánh giá khả năng lựa chọn chỉ số đánh giá phù hợp cho bài toán phân loại mất cân bằng dữ liệu.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "ai-co-ban-trien-khai-va-data-drift",
                "text": "Bạn đã huấn luyện một mô hình hoạt động rất tốt khi thử nghiệm nhưng hiệu năng suy giảm sau hai tháng triển khai thực tế. Trình bày các bước bạn thực hiện để chẩn đoán và xử lý hiện tượng trôi dữ liệu (data drift) hoặc trôi khái niệm (concept drift).",
                "objective": "Đánh giá kiến thức vận hành AI thực tế: giám sát, phát hiện trôi dữ liệu, pipeline tái huấn luyện.",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "ai-python-concurrency-race-condition",
                "text": "Đoạn mã thu thập metrics thống kê dưới đây đang gặp lỗi Race Condition khi chạy đa luồng/bất đồng bộ khiến biến đếm counter bị sai lệch. Bạn hãy tìm nguyên nhân, sửa lại hàm increment() trên editor bên phải và bấm Chạy thử test cases nhé.",
                "objective": "Phát hiện critical section chưa được bảo vệ; sử dụng asyncio.Lock() hoặc Semaphore; giải thích được cơ chế Event loop và nguy cơ deadlock nếu code bên trong Lock bắn exception.",
                "difficulty": "intermediate",
                "type": "coding",
                "locale": "vi-VN",
                "canonical_snapshot": {
                    "language": "python",
                    "starter_code": "import asyncio\n\nclass MetricsCollector:\n    def __init__(self):\n        self.counter = 0\n\n    async def increment(self):\n        # BUG: Race condition xảy ra ở đây khi chạy đồng thời\n        temp = self.counter\n        await asyncio.sleep(0.001)\n        self.counter = temp + 1\n",
                    "test_cases_code": "async def run_tests():\n    collector = MetricsCollector()\n    await asyncio.gather(*[collector.increment() for _ in range(100)])\n    assert collector.counter == 100, f'Expected 100, but got {collector.counter}'\n    print('TEST PASSED: Concurrency handled correctly!')\n",
                    "solution_code": "import asyncio\n\nclass MetricsCollector:\n    def __init__(self):\n        self.counter = 0\n        self._lock = asyncio.Lock()\n\n    async def increment(self):\n        async with self._lock:\n            temp = self.counter\n            await asyncio.sleep(0.001)\n            self.counter = temp + 1\n",
                },
            },
            {
                "stable_key": "ai-python-memory-leak-session",
                "text": "Hàm gọi Model Inference dưới đây tạo HTTP client session mới cho mỗi request nhưng không đóng lại, gây rò rỉ connection pool khi tải cao. Hãy tối ưu lại bằng Connection Pooling hoặc Singleton Client.",
                "objective": "Đánh giá khả năng quản lý tài nguyên HTTP/Async client và connection pooling trong FastAPI/Python.",
                "difficulty": "intermediate",
                "type": "coding",
                "locale": "vi-VN",
                "canonical_snapshot": {
                    "language": "python",
                    "starter_code": "import asyncio\n\nclass InferenceClient:\n    def __init__(self):\n        self.active_sessions = []\n\n    async def predict(self, prompt: str):\n        # BUG: Tạo session mới nhưng không cleanup\n        session = {'id': len(self.active_sessions) + 1, 'closed': False}\n        self.active_sessions.append(session)\n        return f'Result for {prompt}'\n",
                    "test_cases_code": "async def run_tests():\n    client = InferenceClient()\n    for i in range(10):\n        await client.predict(f'prompt {i}')\n    unclosed = [s for s in client.active_sessions if not s['closed']]\n    assert len(unclosed) == 0, f'Leaked {len(unclosed)} unclosed sessions!'\n    print('TEST PASSED: No connection leak!')\n",
                    "solution_code": "import asyncio\n\nclass InferenceClient:\n    def __init__(self):\n        self.active_sessions = []\n\n    async def predict(self, prompt: str):\n        session = {'id': len(self.active_sessions) + 1, 'closed': True}\n        self.active_sessions.append(session)\n        return f'Result for {prompt}'\n",
                },
            },
        ],
    },
    # ------------------------------------------------------------------ #
    # skill-generative-ai  (target: 1, 3 en-US + 3 vi-VN)                 #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-generative-ai",
        "questions": [
            {
                "stable_key": "genai-prompt-engineering-chain-of-thought",
                "text": "Explain chain-of-thought prompting and demonstrate with a brief example how it improves reasoning accuracy in large language models.",
                "objective": "Assess practical knowledge of prompt engineering techniques and understanding of LLM reasoning.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "genai-hallucination-mitigation",
                "text": "Generative AI models can hallucinate facts. Describe two architectural or prompt-level strategies you have used or would use to reduce hallucinations in a production application.",
                "objective": "Assess awareness of generative AI failure modes and mitigation strategies.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "genai-fine-tuning-vs-rag",
                "text": "Compare fine-tuning a foundation model with Retrieval-Augmented Generation (RAG) for a domain-specific Q&A use-case. When would you choose each approach?",
                "objective": "Assess ability to evaluate and select generative AI adaptation strategies based on cost, latency, and data constraints.",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "genai-prompt-chain-of-thought",
                "text": "Giải thích kỹ thuật chain-of-thought prompting và đưa ra một ví dụ ngắn minh họa cách nó cải thiện độ chính xác suy luận trong các mô hình ngôn ngữ lớn.",
                "objective": "Đánh giá kiến thức thực hành kỹ nghệ prompt và suy luận của LLM.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "genai-giam-thieu-hallucination",
                "text": "Các mô hình Generative AI có thể bị ảo giác (hallucination). Mô tả hai chiến lược ở mức kiến trúc hoặc prompt mà bạn đã sử dụng hoặc sẽ sử dụng để giảm thiểu ảo giác trong ứng dụng thực tế.",
                "objective": "Đánh giá mức độ nhận thức về lỗi ảo giác trong GenAI và các giải pháp giảm thiểu.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "genai-fine-tuning-vs-rag-vi",
                "text": "So sánh fine-tuning mô hình nền tảng với Retrieval-Augmented Generation (RAG) cho bài toán hỏi đáp (Q&A) theo lĩnh vực cụ thể. Khi nào bạn sẽ chọn từng phương pháp?",
                "objective": "Đánh giá khả năng lựa chọn chiến lược thích ứng mô hình GenAI dựa trên chi phí, độ trễ và dữ liệu.",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "vi-VN",
            },
        ],
    },
    # ------------------------------------------------------------------ #
    # skill-large-language-models  (target: 1, 3 en-US + 3 vi-VN)         #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-large-language-models",
        "questions": [
            {
                "stable_key": "llm-transformer-attention-mechanism",
                "text": "Explain the self-attention mechanism in transformer models. How does it allow the model to capture long-range dependencies in text?",
                "objective": "Assess foundational understanding of transformer architecture and attention.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "llm-context-window-limitations",
                "text": "What are the practical implications of a finite context window in an LLM, and what strategies can you apply when your input exceeds that limit?",
                "objective": "Assess practical LLM engineering knowledge around context management.",
                "difficulty": "foundational",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "llm-inference-latency-optimisation",
                "text": "Describe at least three techniques to reduce LLM inference latency in a production serving system without significantly degrading output quality.",
                "objective": "Assess systems-level understanding of LLM serving optimisation (quantisation, batching, speculative decoding, etc.).",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "llm-co-che-attention-transformer",
                "text": "Giải thích cơ chế self-attention trong kiến trúc Transformer. Cơ chế này giúp mô hình nắm bắt các phụ thuộc xa (long-range dependencies) trong văn bản như thế nào?",
                "objective": "Đánh giá hiểu biết nền tảng về kiến trúc transformer và attention.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "llm-gioi-han-context-window",
                "text": "Giới hạn về context window trong LLM mang lại những thách thức thực tế nào và bạn áp dụng những chiến lược gì khi đầu vào vượt quá giới hạn đó?",
                "objective": "Đánh giá kỹ năng xử lý context window trong kỹ thuật LLM.",
                "difficulty": "foundational",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "llm-toi-uu-latency-inference",
                "text": "Mô tả ít nhất ba kỹ thuật nhằm giảm độ trễ suy luận (inference latency) của LLM trong hệ thống phục vụ thực tế mà không làm suy giảm đáng kể chất lượng phản hồi.",
                "objective": "Đánh giá hiểu biết ở mức hệ thống về tối ưu hóa phục vụ LLM (quantization, batching, speculative decoding).",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "vi-VN",
            },
        ],
    },
    # ------------------------------------------------------------------ #
    # skill-natural-language-processing  (target: 1, 3 en-US + 3 vi-VN)    #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-natural-language-processing",
        "questions": [
            {
                "stable_key": "nlp-tokenization-tradeoffs",
                "text": "Compare word-level, sub-word (BPE/WordPiece), and character-level tokenisation strategies. In what situations would each be preferred?",
                "objective": "Assess understanding of tokenisation design decisions and their impact on vocabulary size and OOV handling.",
                "difficulty": "foundational",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "nlp-named-entity-recognition-production",
                "text": "You need to build a Named Entity Recognition (NER) system for a niche medical domain with limited labelled data. Walk me through your approach.",
                "objective": "Assess practical NLP problem-solving: transfer learning, data augmentation, and domain adaptation.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "nlp-semantic-search-implementation",
                "text": "Describe how you would implement a semantic search system over a corpus of 10 million documents. What embedding model, index, and retrieval strategy would you use?",
                "objective": "Assess end-to-end NLP system design for large-scale semantic retrieval.",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "en-US",
            },
            {
                "stable_key": "nlp-danh-doi-tokenization",
                "text": "So sánh các chiến lược tách từ ở cấp độ từ (word-level), từ con (sub-word như BPE/WordPiece) và ký tự (character-level). Trong những tình huống nào thì mỗi phương pháp được ưu tiên?",
                "objective": "Đánh giá hiểu biết về thiết kế tokenization và xử lý OOV.",
                "difficulty": "foundational",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "nlp-nhan-dang-thuc-the-ner",
                "text": "Bạn cần xây dựng một hệ thống Nhận dạng Thực thể Đặt tên (NER) cho một lĩnh vực y tế đặc thù với lượng dữ liệu gán nhãn hạn chế. Hãy trình bày phương pháp tiếp cận của bạn.",
                "objective": "Đánh giá giải quyết vấn đề NLP thực tế: học chuyển giao, tăng cường dữ liệu và thích ứng miền.",
                "difficulty": "intermediate",
                "type": "technical",
                "locale": "vi-VN",
            },
            {
                "stable_key": "nlp-tim-kiem-ngu-nghia-semantic-search",
                "text": "Mô tả cách bạn sẽ thiết kế và triển khai một hệ thống tìm kiếm ngữ nghĩa (semantic search) trên kho ngữ liệu 10 triệu tài liệu. Bạn sẽ lựa chọn mô hình embedding, chỉ mục (index) và chiến lược truy xuất nào?",
                "objective": "Đánh giá thiết kế hệ thống NLP quy mô lớn cho truy xuất ngữ nghĩa.",
                "difficulty": "advanced",
                "type": "technical",
                "locale": "vi-VN",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Deterministic UUID helpers
# ---------------------------------------------------------------------------

def _det_uuid(namespace: str, name: str) -> uuid.UUID:
    """Return a deterministic UUID5 for the given (namespace, name) pair."""
    ns = uuid.uuid5(uuid.NAMESPACE_DNS, f"interview-prep-seed.{namespace}")
    return uuid.uuid5(ns, name)


# ---------------------------------------------------------------------------
# Rubric builder
# ---------------------------------------------------------------------------

def _build_rubric_records(question_stable_key: str, version_id: uuid.UUID) -> dict[str, Any]:
    """Return all rubric-related records for a single question version.

    Rubric shape (validated by _validate_publishable_contract):
    - rubric_versions.score_min=0, score_max=3
    - sum(rubric_criteria.weight) == 1.0
    - Every criterion has exactly 4 rubric_anchors (level 0–3)
    """
    rubric_id = _det_uuid("rubric", question_stable_key)
    rubric_version_id = _det_uuid("rubric-version", question_stable_key)

    criteria = [
        {
            "id": _det_uuid("criterion-relevance", question_stable_key),
            "stable_key": "relevance",
            "name": "Relevance",
            "description": "Answer addresses the question directly and completely.",
            "weight": Decimal("0.35"),
            "critical": False,
            "display_order": 1,
        },
        {
            "id": _det_uuid("criterion-depth", question_stable_key),
            "stable_key": "technical_depth",
            "name": "Technical Depth",
            "description": "Demonstrates accurate, detailed technical knowledge.",
            "weight": Decimal("0.35"),
            "critical": True,
            "display_order": 2,
        },
        {
            "id": _det_uuid("criterion-clarity", question_stable_key),
            "stable_key": "clarity",
            "name": "Clarity",
            "description": "Explanation is clear, structured, and easy to follow.",
            "weight": Decimal("0.30"),
            "critical": False,
            "display_order": 3,
        },
    ]

    anchors_by_criterion: dict[str, list[dict[str, Any]]] = {
        "relevance": [
            {"level": 0, "description": "Off-topic or does not address the question.", "positive": [], "negative": ["Ignores the question"]},
            {"level": 1, "description": "Partially addresses the question with gaps.", "positive": ["Attempts to answer"], "negative": ["Missing key aspects"]},
            {"level": 2, "description": "Addresses the question adequately.", "positive": ["Covers main points"], "negative": []},
            {"level": 3, "description": "Comprehensively addresses all aspects.", "positive": ["Complete coverage", "Adds relevant depth"], "negative": []},
        ],
        "technical_depth": [
            {"level": 0, "description": "No meaningful technical content.", "positive": [], "negative": ["No technical detail"]},
            {"level": 1, "description": "Basic technical understanding only.", "positive": ["Knows surface concepts"], "negative": ["Cannot explain mechanisms"]},
            {"level": 2, "description": "Solid technical knowledge with correct detail.", "positive": ["Correct terminology", "Explains mechanisms"], "negative": []},
            {"level": 3, "description": "Expert-level technical depth with nuance.", "positive": ["Trade-off awareness", "Edge case handling", "Practical experience"], "negative": []},
        ],
        "clarity": [
            {"level": 0, "description": "Incoherent or extremely difficult to follow.", "positive": [], "negative": ["Cannot follow the explanation"]},
            {"level": 1, "description": "Some structure but hard to follow in places.", "positive": ["Attempts structure"], "negative": ["Loses the thread"]},
            {"level": 2, "description": "Clear and reasonably well-structured.", "positive": ["Logical flow"], "negative": []},
            {"level": 3, "description": "Exceptionally clear, concise, and structured.", "positive": ["Excellent structure", "Analogies used well"], "negative": []},
        ],
    }

    return {
        "rubric_id": rubric_id,
        "rubric_version_id": rubric_version_id,
        "criteria": criteria,
        "anchors_by_criterion": anchors_by_criterion,
        "question_version_id": version_id,
    }


# ---------------------------------------------------------------------------
# Core seed function
# ---------------------------------------------------------------------------

async def seed_question_bank(session_factory: Callable[[], AsyncSession]) -> dict[str, int]:
    """Idempotently seed the Question Bank with dev fixtures.

    Returns a summary dict: {concept_id: questions_seeded}.
    """
    summary: dict[str, int] = {}

    async with session_factory() as db:
        for concept_fixture in _CONCEPT_FIXTURES:
            concept_id: str = concept_fixture["concept_id"]
            seeded = 0

            for q in concept_fixture["questions"]:
                stable_key: str = q["stable_key"]
                created = await _seed_one_question(
                    db,
                    stable_key=stable_key,
                    concept_id=concept_id,
                    canonical_text=q["text"],
                    objective=q["objective"],
                    difficulty_band=q["difficulty"],
                    question_type=q["type"],
                    canonical_locale=q.get("locale", "en-US"),
                    canonical_snapshot=q.get("canonical_snapshot"),
                )
                if created:
                    seeded += 1

            summary[concept_id] = seeded

        await db.commit()

    return summary


async def _seed_one_question(
    db: AsyncSession,
    *,
    stable_key: str,
    concept_id: str,
    canonical_text: str,
    objective: str,
    difficulty_band: str,
    question_type: str,
    canonical_locale: str = "en-US",
    canonical_snapshot: dict[str, Any] | None = None,
) -> bool:
    """Insert a single fully-eligible question. Returns True if newly created."""

    question_id = _det_uuid("question", stable_key)
    version_id = _det_uuid("question-version", stable_key)

    # ------------------------------------------------------------------
    # 1. InterviewQuestion – insert with NULL current_approved_version_id
    #    first to avoid FK violation (version doesn't exist yet).
    #    current_approved_version_id is set by UPDATE after version insert.
    # ------------------------------------------------------------------
    await db.execute(
        text(
            "INSERT INTO interview_questions "
            "(id, stable_key, current_approved_version_id, created_by, created_at) "
            "VALUES (CAST(:id AS uuid), :stable_key, NULL, :created_by, now()) "
            "ON CONFLICT (stable_key) DO NOTHING"
        ),
        {
            "id": str(question_id),
            "stable_key": stable_key,
            "created_by": _SEED_AUTHOR,
        },
    )

    # Check if this version already exists (idempotency guard)
    existing = await db.scalar(
        text("SELECT 1 FROM interview_question_versions WHERE id = CAST(:id AS uuid)"),
        {"id": str(version_id)},
    )
    if existing:
        # Ensure current_approved_version_id is set correctly even on re-runs
        await db.execute(
            text(
                "UPDATE interview_questions "
                "SET current_approved_version_id = CAST(:version_id AS uuid) "
                "WHERE stable_key = :stable_key "
                "  AND (current_approved_version_id IS NULL "
                "   OR current_approved_version_id != CAST(:version_id AS uuid))"
            ),
            {"version_id": str(version_id), "stable_key": stable_key},
        )
        # Synchronize canonical_locale and canonical_snapshot if fixture was updated
        await db.execute(
            text(
                "UPDATE interview_question_versions "
                "SET canonical_locale = :canonical_locale, "
                "    canonical_snapshot = CAST(:canonical_snapshot AS jsonb) "
                "WHERE id = CAST(:version_id AS uuid)"
            ),
            {
                "version_id": str(version_id),
                "canonical_locale": canonical_locale,
                "canonical_snapshot": json.dumps(canonical_snapshot or {}),
            },
        )
        return False

    # ------------------------------------------------------------------
    # 2. InterviewQuestionVersion (status=APPROVED)
    # ------------------------------------------------------------------
    now = datetime.now(UTC)
    await db.execute(
        text(
            "INSERT INTO interview_question_versions "
            "(id, question_id, version, schema_version, taxonomy_version, status, "
            "question_type, difficulty_band, canonical_locale, canonical_text, objective, "
            "thinking_seconds, soft_answer_seconds, hard_answer_seconds, "
            "context_policy, personalization_policy, canonical_snapshot, "
            "created_by, created_at, submitted_at, approved_at, change_summary) "
            "VALUES ("
            "CAST(:id AS uuid), CAST(:question_id AS uuid), :version, '1.0', :taxonomy_version, 'APPROVED', "
            ":question_type, :difficulty_band, :canonical_locale, :canonical_text, :objective, "
            "30, 120, 180, "
            "CAST('{}' AS jsonb), CAST('{}' AS jsonb), CAST(:canonical_snapshot AS jsonb), "
            ":created_by, :created_at, :created_at, :approved_at, 'Initial dev seed')"
        ),
        {
            "id": str(version_id),
            "question_id": str(question_id),
            "version": "1.0.0",
            "taxonomy_version": _QUESTION_TAXONOMY_VERSION,
            "question_type": question_type,
            "difficulty_band": difficulty_band,
            "canonical_locale": canonical_locale,
            "canonical_text": canonical_text,
            "objective": objective,
            "canonical_snapshot": json.dumps(canonical_snapshot or {}),
            "created_by": _SEED_AUTHOR,
            "created_at": now,
            "approved_at": now,
        },
    )

    # ------------------------------------------------------------------
    # 3. Update InterviewQuestion.current_approved_version_id
    # ------------------------------------------------------------------
    await db.execute(
        text(
            "UPDATE interview_questions "
            "SET current_approved_version_id = CAST(:version_id AS uuid) "
            "WHERE stable_key = :stable_key"
        ),
        {"version_id": str(version_id), "stable_key": stable_key},
    )

    # ------------------------------------------------------------------
    # 4. QuestionVersionTaxonomyConcept (PRIMARY_COMPETENCY)
    # ------------------------------------------------------------------
    await db.execute(
        text(
            "INSERT INTO question_version_taxonomy_concepts "
            "(question_version_id, taxonomy_version, concept_id, purpose, relevance) "
            "VALUES (CAST(:qvid AS uuid), :tv, :concept_id, 'PRIMARY_COMPETENCY', 1.0000) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "qvid": str(version_id),
            "tv": _QUESTION_TAXONOMY_VERSION,
            "concept_id": concept_id,
        },
    )

    # ------------------------------------------------------------------
    # 5. QuestionApproval (required by selector: at least one approval)
    # ------------------------------------------------------------------
    approval_id = _det_uuid("approval", stable_key)
    await db.execute(
        text(
            "INSERT INTO question_approvals "
            "(id, question_version_id, approver_id, approval_policy_version, approved_at) "
            "VALUES (CAST(:id AS uuid), CAST(:qvid AS uuid), :approver, :policy, :approved_at) "
            "ON CONFLICT (question_version_id) DO NOTHING"
        ),
        {
            "id": str(approval_id),
            "qvid": str(version_id),
            "approver": _SEED_APPROVER,
            "policy": _APPROVAL_POLICY_VERSION,
            "approved_at": now,
        },
    )

    # ------------------------------------------------------------------
    # 6. Rubric: Rubric → RubricVersion → RubricCriteria → RubricAnchors
    # ------------------------------------------------------------------
    rubric_data = _build_rubric_records(stable_key, version_id)

    rubric_id: uuid.UUID = rubric_data["rubric_id"]
    rubric_version_id: uuid.UUID = rubric_data["rubric_version_id"]

    # 6a. Rubric – insert with NULL current_version_id first (same deferred
    #     pattern as InterviewQuestion). UPDATE after RubricVersion exists.
    await db.execute(
        text(
            "INSERT INTO rubrics (id, stable_key, current_version_id) "
            "VALUES (CAST(:id AS uuid), :stable_key, NULL) "
            "ON CONFLICT (stable_key) DO NOTHING"
        ),
        {
            "id": str(rubric_id),
            "stable_key": f"rubric-{stable_key}",
        },
    )

    # 6b → 6b'. RubricVersion (then backfill rubric.current_version_id)
    await db.execute(
        text(
            "INSERT INTO rubric_versions "
            "(id, rubric_id, version, score_min, score_max, minimum_coverage, "
            "aggregation_method, aggregation_policy, created_by, created_at, approved_at) "
            "VALUES ("
            "CAST(:id AS uuid), CAST(:rubric_id AS uuid), '1.0', 0, 3, 0.6000, "
            "'weighted_mean', CAST('{}' AS jsonb), :created_by, :created_at, :approved_at) "
            "ON CONFLICT (rubric_id, version) DO NOTHING"
        ),
        {
            "id": str(rubric_version_id),
            "rubric_id": str(rubric_id),
            "created_by": _SEED_AUTHOR,
            "created_at": now,
            "approved_at": now,
        },
    )
    # Backfill rubric.current_version_id now that rubric_version exists.
    await db.execute(
        text(
            "UPDATE rubrics SET current_version_id = CAST(:rvid AS uuid) "
            "WHERE id = CAST(:rid AS uuid) AND current_version_id IS NULL"
        ),
        {"rvid": str(rubric_version_id), "rid": str(rubric_id)},
    )

    for criterion in rubric_data["criteria"]:
        criterion_id: uuid.UUID = criterion["id"]
        crit_key: str = criterion["stable_key"]

        await db.execute(
            text(
                "INSERT INTO rubric_criteria "
                "(id, rubric_version_id, stable_key, name, description, weight, critical, display_order) "
                "VALUES (CAST(:id AS uuid), CAST(:rvid AS uuid), :key, :name, :desc, :weight, :critical, :order) "
                "ON CONFLICT (rubric_version_id, stable_key) DO NOTHING"
            ),
            {
                "id": str(criterion_id),
                "rvid": str(rubric_version_id),
                "key": crit_key,
                "name": criterion["name"],
                "desc": criterion["description"],
                "weight": criterion["weight"],
                "critical": criterion["critical"],
                "order": criterion["display_order"],
            },
        )

        for anchor in rubric_data["anchors_by_criterion"][crit_key]:
            anchor_id = _det_uuid(f"anchor-{crit_key}-{anchor['level']}", stable_key)
            await db.execute(
                text(
                    "INSERT INTO rubric_anchors "
                    "(criterion_id, level, description, positive_indicators, negative_indicators) "
                    "VALUES (CAST(:cid AS uuid), :level, :desc, CAST(:pos AS jsonb), CAST(:neg AS jsonb)) "
                    "ON CONFLICT (criterion_id, level) DO NOTHING"
                ),
                {
                    "cid": str(criterion_id),
                    "level": anchor["level"],
                    "desc": anchor["description"],
                    "pos": json.dumps(anchor["positive"]),
                    "neg": json.dumps(anchor["negative"]),
                },
            )

    # 6d. QuestionVersionRubric (link version → rubric version)
    await db.execute(
        text(
            "INSERT INTO question_version_rubrics (question_version_id, rubric_version_id) "
            "VALUES (CAST(:qvid AS uuid), CAST(:rvid AS uuid)) "
            "ON CONFLICT (question_version_id) DO NOTHING"
        ),
        {"qvid": str(version_id), "rvid": str(rubric_version_id)},
    )

    return True
