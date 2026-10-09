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

from src.seeds.question_bank_seed_vi import QUESTION_TEXT_VI

# The taxonomy_version used by the JD/CV parsers and stored in
# session_competency_targets.  This MUST match what the selector query uses.
_QUESTION_TAXONOMY_VERSION = "internal-2026.1"

# Career classification concepts live under their own taxonomy version. P1 emits
# these codes when a JD has no taxonomy-backed skill requirement, and P2 accepts
# only TARGET_ROLE mappings for that fallback.
_CAREER_TAXONOMY_VERSION = "internal-career-2026.1"

# (thinking, soft, hard) seconds per question type. The dynamic selector costs
# a question as thinking + soft and requires each target to reach the TEXT
# floor (>= 180s); the old flat 30/120 (150s) never could, so every dynamic
# plan failed closed. `soft` is only read by that selector; the runtime paces
# on `hard`, which stays 180 so legacy session timing is unchanged.
# CODING targets need >= 360s, which conflicts with legacy pacing (a 420s hard
# limit would overrun the 25-minute envelope); that is an open policy item, so
# coding questions keep the default timing for now.
_SEED_TIMINGS: dict[str, tuple[int, int, int]] = {}
_DEFAULT_SEED_TIMING = (30, 150, 180)


def _seed_timing(question_type: str) -> tuple[int, int, int]:
    return _SEED_TIMINGS.get(question_type, _DEFAULT_SEED_TIMING)

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
        "role_concepts": ("technology.artificial-intelligence",),
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
        "role_concepts": ("technology.artificial-intelligence",),
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
        "role_concepts": ("technology.artificial-intelligence",),
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
        "role_concepts": ("technology.artificial-intelligence",),
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
    # ------------------------------------------------------------------ #
    # Backend engineering stack                                           #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-python",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "python-mutable-default-and-scoping",
                "text": "Explain what happens when a mutable object is used as a default argument in a Python function, why it surprises people, and how you would write it correctly.",
                "objective": "Assess understanding of Python evaluation semantics and defensive function design.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "python-generators-vs-lists",
                "text": "When would you choose a generator over building a list in Python? Walk through a case where that choice changed memory or latency for you.",
                "objective": "Assess practical knowledge of lazy evaluation and memory behaviour.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "python-asyncio-blocking-call",
                "text": "You have an async FastAPI endpoint that became slow after someone added a synchronous database driver call. Explain what goes wrong in the event loop and how you would fix it.",
                "objective": "Assess understanding of the asyncio event loop and blocking-call isolation.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-fastapi",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "fastapi-dependency-injection",
                "text": "Explain how FastAPI dependency injection works and how you would use it to share a database session safely across requests.",
                "objective": "Assess understanding of request-scoped dependencies and resource lifecycle.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "fastapi-pydantic-validation-boundary",
                "text": "How do you decide what belongs in a Pydantic request model versus in service-layer validation? Give an example where putting a rule in the wrong place caused a problem.",
                "objective": "Assess API boundary design and separation of validation concerns.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "fastapi-background-work-and-timeouts",
                "text": "A FastAPI endpoint must trigger work that takes two minutes. Describe the options you would consider, the trade-offs, and how you would report status back to the client.",
                "objective": "Assess long-running work design, timeouts and client contract thinking.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-postgresql",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "postgresql-index-selection",
                "text": "A query filtering on two columns and sorting on a third is slow. Describe how you would read the execution plan and decide which index to create.",
                "objective": "Assess query-plan reading and index design reasoning.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "postgresql-transaction-isolation",
                "text": "Explain the difference between READ COMMITTED and SERIALIZABLE in PostgreSQL, and describe a bug you would expect to see at the weaker level.",
                "objective": "Assess transaction isolation knowledge and concurrency failure modes.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "postgresql-schema-migration-safety",
                "text": "How would you add a NOT NULL column with a default to a large, live table without taking the service down?",
                "objective": "Assess safe migration practice on production data.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-java",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "java-equals-hashcode-contract",
                "text": "Explain the contract between equals and hashCode in Java and what breaks when a class violates it.",
                "objective": "Assess core object-model knowledge and collection behaviour.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "java-collections-concurrency",
                "text": "When would you reach for ConcurrentHashMap instead of synchronizing access to a HashMap? What does it actually guarantee?",
                "objective": "Assess concurrency primitives and their guarantees.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "java-exception-design",
                "text": "How do you decide between a checked and an unchecked exception in a service layer? Give an example from your own code.",
                "objective": "Assess error-handling design judgement.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-spring-boot",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "spring-boot-bean-scopes",
                "text": "Explain the difference between singleton and prototype beans in Spring, and describe a bug caused by injecting the wrong scope.",
                "objective": "Assess container lifecycle understanding.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "spring-boot-transactional-pitfalls",
                "text": "Describe how @Transactional actually works in Spring and name a situation where it silently does not apply.",
                "objective": "Assess proxy-based transaction semantics and their edge cases.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "spring-boot-configuration-profiles",
                "text": "How do you structure configuration across local, staging and production in a Spring Boot service without leaking secrets?",
                "objective": "Assess configuration and secret-handling practice.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    # ------------------------------------------------------------------ #
    # Frontend engineering stack                                          #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-javascript",
        "role_concepts": ("technology.software-engineering.frontend",),
        "questions": [
            {
                "stable_key": "javascript-event-loop-microtasks",
                "text": "Explain the difference between a microtask and a macrotask in the JavaScript event loop, and how that ordering can surprise you in practice.",
                "objective": "Assess runtime execution model knowledge.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "javascript-closures-in-loops",
                "text": "Explain how closures capture variables in JavaScript and why loops with var historically produced the wrong value.",
                "objective": "Assess scoping and closure fundamentals.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "javascript-promise-error-handling",
                "text": "Describe how errors propagate through promise chains and async/await, and a case where an error was swallowed.",
                "objective": "Assess asynchronous error-handling discipline.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-typescript",
        "role_concepts": ("technology.software-engineering.frontend",),
        "questions": [
            {
                "stable_key": "typescript-structural-typing",
                "text": "TypeScript uses structural typing. Explain what that means in practice and a case where it let an unsafe value through.",
                "objective": "Assess type-system understanding beyond annotation syntax.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "typescript-unknown-vs-any",
                "text": "When would you use unknown instead of any, and how do you narrow it safely at an API boundary?",
                "objective": "Assess type-safety discipline at untrusted boundaries.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "typescript-generics-in-practice",
                "text": "Describe a generic type or function you wrote that genuinely earned its complexity, and how you would know if it had not.",
                "objective": "Assess judgement about abstraction cost in a type system.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-react",
        "role_concepts": ("technology.software-engineering.frontend",),
        "questions": [
            {
                "stable_key": "react-rendering-and-memo",
                "text": "Explain when React re-renders a component and how you would investigate a page that re-renders too often. When is memoisation the wrong fix?",
                "objective": "Assess render-model understanding and performance diagnosis.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "react-effect-dependencies",
                "text": "Describe how you decide the dependency array of useEffect, and a bug you have hit from getting it wrong.",
                "objective": "Assess effect lifecycle and stale-closure awareness.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "react-state-placement",
                "text": "How do you decide whether a piece of state belongs in a component, in a shared store, or on the server? Walk through a real decision you made.",
                "objective": "Assess state architecture judgement.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    # ------------------------------------------------------------------ #
    # Cloud and DevOps stack                                              #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-docker",
        "role_concepts": ("technology.cloud-devops",),
        "questions": [
            {
                "stable_key": "docker-image-layers-and-size",
                "text": "Explain how Docker image layers and the build cache work, and how you would cut a 1.5 GB image down meaningfully.",
                "objective": "Assess image build model and optimisation practice.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "docker-container-networking",
                "text": "Two containers in the same compose project cannot reach each other. Walk through how you would diagnose it.",
                "objective": "Assess container networking troubleshooting.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "docker-runtime-config-and-secrets",
                "text": "How do you pass configuration and secrets into a container without baking them into the image?",
                "objective": "Assess secure runtime configuration practice.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-kubernetes",
        "role_concepts": ("technology.cloud-devops",),
        "questions": [
            {
                "stable_key": "kubernetes-probes",
                "text": "Explain the difference between a liveness and a readiness probe, and what goes wrong when they are configured the same way.",
                "objective": "Assess workload health-check design.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "kubernetes-pod-crashloop-diagnosis",
                "text": "A deployment is in CrashLoopBackOff after a release. Walk through your diagnosis, in order.",
                "objective": "Assess systematic cluster troubleshooting.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "kubernetes-resource-requests-limits",
                "text": "Explain the difference between resource requests and limits, and what happens to a pod that exceeds each.",
                "objective": "Assess scheduling and resource-governance knowledge.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-aws",
        "role_concepts": ("technology.cloud-devops",),
        "questions": [
            {
                "stable_key": "aws-iam-least-privilege",
                "text": "How would you grant a service access to exactly one S3 bucket prefix, and how do you verify the policy is not broader than intended?",
                "objective": "Assess least-privilege access design and verification.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "aws-compute-choice",
                "text": "For a new HTTP service with spiky traffic, how would you choose between Lambda, ECS and EC2? What would change your mind?",
                "objective": "Assess cloud architecture trade-off reasoning.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "aws-cost-investigation",
                "text": "The monthly bill doubled with no traffic change. Describe how you would find the cause.",
                "objective": "Assess cost ownership and investigation method.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    # ------------------------------------------------------------------ #
    # Cross-cutting fundamentals that appear in almost every JD            #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-sql",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "sql-join-semantics",
                "text": "Explain the difference between an INNER JOIN and a LEFT JOIN, and describe a reporting bug caused by picking the wrong one.",
                "objective": "Assess relational query fundamentals and their practical consequences.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "sql-aggregation-and-grouping",
                "text": "Walk me through how GROUP BY and HAVING differ from WHERE, and give an example where moving a condition between them changed the result.",
                "objective": "Assess aggregation semantics and query-ordering understanding.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "sql-n-plus-one-queries",
                "text": "Describe the N+1 query problem, how you would spot it in a running service, and two different ways to fix it.",
                "objective": "Assess data-access performance diagnosis.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-git",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "git-merge-vs-rebase",
                "text": "When do you rebase and when do you merge? Describe a case where the choice mattered to your team.",
                "objective": "Assess version-control workflow judgement.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "git-recovering-a-bad-commit",
                "text": "You pushed a commit that broke the main branch and others have already pulled it. Walk through how you would recover.",
                "objective": "Assess recovery practice and awareness of shared-history risk.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "git-branching-strategy",
                "text": "Describe the branching strategy on your last project and one thing you would change about it.",
                "objective": "Assess collaboration workflow and reflective judgement.",
                "difficulty": "foundational",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-http",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "http-status-code-semantics",
                "text": "How do you decide between 400, 401, 403, 404 and 409 for a failing API request? Give an example of each being the right answer.",
                "objective": "Assess HTTP semantics and API error design.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "http-idempotency-and-retries",
                "text": "Which HTTP methods should be idempotent, and how would you make a payment endpoint safe to retry?",
                "objective": "Assess idempotency design under client retries.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "http-caching-headers",
                "text": "Explain how ETag and Cache-Control work together, and a case where caching served a stale response.",
                "objective": "Assess HTTP caching mechanics and failure modes.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-json",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "json-schema-evolution",
                "text": "How do you add a field to a JSON API response without breaking existing clients? What changes are not safe?",
                "objective": "Assess backward-compatible payload design.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "json-parsing-untrusted-input",
                "text": "What do you validate when parsing JSON that came from an untrusted client, and where in the stack do you do it?",
                "objective": "Assess input-validation discipline at trust boundaries.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "json-numeric-precision",
                "text": "Describe what can go wrong when large integers or money amounts travel through JSON, and how you handle it.",
                "objective": "Assess awareness of serialization precision pitfalls.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-xml",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "xml-namespaces-and-xsd-validation",
                "text": "A partner sends you XML (for example a SOAP message or an invoice feed). How do you handle namespaces and validate the payload against an XSD before processing it?",
                "objective": "Assess practical XML integration: namespaces and schema validation at the boundary.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "xml-vs-json-mapping",
                "text": "When would you still choose XML over JSON for data exchange, and what information (attributes, ordering, mixed content, types) can be lost when converting between them?",
                "objective": "Assess format trade-offs and lossy-conversion awareness.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "xml-xxe-and-parser-hardening",
                "text": "Explain how an XML External Entity (XXE) or entity-expansion attack works against an endpoint that accepts XML, and how you configure the parser to prevent it.",
                "objective": "Assess XML parser security on untrusted input.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-machine-learning",
        "role_concepts": ("technology.artificial-intelligence",),
        "questions": [
            {
                "stable_key": "ml-train-test-leakage",
                "text": "Explain what data leakage is in a machine-learning pipeline, how you would detect it, and a case where it inflated your metrics.",
                "objective": "Assess experimental rigour and evaluation hygiene.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "ml-metric-selection",
                "text": "For a heavily imbalanced classification problem, which metric would you optimise and why is accuracy misleading?",
                "objective": "Assess metric selection under class imbalance.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "ml-model-in-production",
                "text": "A model that performed well offline degrades in production. Walk through how you would investigate.",
                "objective": "Assess production ML debugging and drift awareness.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    # ------------------------------------------------------------------ #
    # Mobile and game stacks                                              #
    # ------------------------------------------------------------------ #
    {
        "concept_id": "skill-android",
        "role_concepts": ("technology.software-engineering.mobile",),
        "questions": [
            {
                "stable_key": "android-activity-lifecycle",
                "text": "Walk through the Android activity lifecycle and explain where you would save state so a rotation does not lose it.",
                "objective": "Assess platform lifecycle understanding.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "android-background-work",
                "text": "How do you run work that must survive the app being backgrounded or the device rebooting?",
                "objective": "Assess background execution and platform constraints.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "android-memory-leaks",
                "text": "What commonly leaks memory in an Android app, and how would you confirm a leak rather than guess?",
                "objective": "Assess memory diagnosis practice.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-ios",
        "role_concepts": ("technology.software-engineering.mobile",),
        "questions": [
            {
                "stable_key": "ios-arc-and-retain-cycles",
                "text": "Explain how ARC manages memory in iOS and how a retain cycle forms. How do you break one?",
                "objective": "Assess memory-management fundamentals on the platform.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "ios-view-lifecycle",
                "text": "Describe the UIViewController lifecycle and where you would put work that must not run on every appearance.",
                "objective": "Assess platform lifecycle understanding.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "ios-main-thread-responsiveness",
                "text": "The UI stutters while loading data. Explain what is likely happening and how you would fix it.",
                "objective": "Assess concurrency and UI responsiveness practice.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-kotlin",
        "role_concepts": ("technology.software-engineering.mobile",),
        "questions": [
            {
                "stable_key": "kotlin-null-safety",
                "text": "Explain how Kotlin's type system handles null, and when you would still reach for a non-null assertion.",
                "objective": "Assess null-safety fundamentals and pragmatic judgement.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "kotlin-coroutines-scope",
                "text": "What problem does structured concurrency solve in Kotlin coroutines, and what happens when you launch in the wrong scope?",
                "objective": "Assess coroutine lifecycle and cancellation understanding.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "kotlin-data-classes-and-equality",
                "text": "What does a Kotlin data class give you for free, and when is it the wrong choice?",
                "objective": "Assess language-feature judgement.",
                "difficulty": "foundational",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-cpp",
        "questions": [
            {
                "stable_key": "cpp-raii-and-ownership",
                "text": "Explain RAII and how smart pointers express ownership. When would you still use a raw pointer?",
                "objective": "Assess resource-management fundamentals.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "cpp-undefined-behaviour",
                "text": "Give two concrete examples of undefined behaviour in C++ and explain why they are dangerous rather than merely wrong.",
                "objective": "Assess language-semantics depth and risk awareness.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "cpp-copy-vs-move",
                "text": "Explain the difference between copy and move semantics, and a case where getting it wrong cost you performance.",
                "objective": "Assess value-semantics understanding.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-unity",
        "role_concepts": ("technology.game-development",),
        "questions": [
            {
                "stable_key": "unity-update-loop-performance",
                "text": "What runs every frame in Unity, and how would you find out why a scene dropped below the target frame rate?",
                "objective": "Assess engine loop understanding and performance profiling.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "unity-prefabs-and-scene-structure",
                "text": "How do you structure prefabs and scenes so several people can work on a game without constant conflicts?",
                "objective": "Assess project organisation and team workflow.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "unity-physics-and-fixedupdate",
                "text": "Explain the difference between Update and FixedUpdate, and a bug caused by putting physics code in the wrong one.",
                "objective": "Assess engine timing model understanding.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    # Skills the JD parser recognises in real postings (monitoring, Linux,
    # CI/CD, ...). Without at least one approved question per concept, any JD
    # that requires them fails selection with question_bank_insufficient.
    {
        "concept_id": "skill-monitoring",
        "role_concepts": ("technology.cloud-devops",),
        "questions": [
            {
                "stable_key": "monitoring-metrics-logs-traces",
                "text": "What is the difference between metrics, logs and traces, and which would you reach for first when an API suddenly gets slow?",
                "objective": "Assess observability fundamentals and debugging approach.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "monitoring-alerting-on-symptoms",
                "text": "How do you decide what deserves an alert that pages someone at night, and how do you avoid alert fatigue?",
                "objective": "Assess alert design and operational judgement.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "monitoring-slo-error-budget",
                "text": "Explain SLIs, SLOs and error budgets, and how an error budget changes what a team works on.",
                "objective": "Assess reliability engineering concepts.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-linux",
        "role_concepts": ("technology.cloud-devops",),
        "questions": [
            {
                "stable_key": "linux-process-and-signals",
                "text": "A process on a Linux server is using 100% CPU and will not stop. Walk me through how you investigate it and how you would stop it safely.",
                "objective": "Assess process management and troubleshooting.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "linux-permissions-and-ownership",
                "text": "Explain Linux file permissions and ownership, and why running a service as root is a risk.",
                "objective": "Assess permission model and security awareness.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "linux-disk-full-incident",
                "text": "A server reports that its disk is full but you cannot find large files. What could cause this and how do you find out?",
                "objective": "Assess filesystem knowledge and incident debugging.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-cicd",
        "role_concepts": ("technology.cloud-devops",),
        "questions": [
            {
                "stable_key": "cicd-pipeline-stages",
                "text": "Describe the stages of a CI/CD pipeline you have worked with, and what should make the pipeline fail.",
                "objective": "Assess pipeline design and quality gates.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "cicd-safe-deployments",
                "text": "Compare rolling, blue-green and canary deployments. Which would you choose for a risky change, and how would you roll back?",
                "objective": "Assess deployment strategy and risk management.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "cicd-flaky-and-slow-builds",
                "text": "Your team's pipeline takes 40 minutes and fails randomly. How would you make it fast and trustworthy again?",
                "objective": "Assess pipeline optimisation and test reliability.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-container-orchestrator",
        "role_concepts": ("technology.cloud-devops",),
        "questions": [
            {
                "stable_key": "orchestrator-why-orchestrate",
                "text": "What problems does a container orchestrator such as Kubernetes or Docker Swarm solve compared with running containers by hand?",
                "objective": "Assess orchestration fundamentals.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "orchestrator-health-and-rollout",
                "text": "How does an orchestrator decide a container is healthy, and what goes wrong during a rollout when health checks are missing?",
                "objective": "Assess health checks and rollout behaviour.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "orchestrator-scaling-stateful",
                "text": "Why is scaling a stateful service such as a database harder than a stateless API on an orchestrator?",
                "objective": "Assess state management in distributed deployments.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-csharp",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "csharp-value-vs-reference",
                "text": "Explain value types and reference types in C#, and a bug that comes from mixing them up.",
                "objective": "Assess type system fundamentals.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "csharp-async-await",
                "text": "How does async/await work in C#, and why can calling .Result on a task cause a deadlock?",
                "objective": "Assess asynchronous programming understanding.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "csharp-idisposable-and-gc",
                "text": "When do you need IDisposable in C# if the garbage collector already frees memory?",
                "objective": "Assess resource management.",
                "difficulty": "intermediate",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-dotnet",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "dotnet-dependency-injection-lifetimes",
                "text": "Explain the Singleton, Scoped and Transient lifetimes in ASP.NET Core dependency injection, and a bug caused by choosing the wrong one.",
                "objective": "Assess DI lifetimes and their consequences.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "dotnet-middleware-pipeline",
                "text": "How does the ASP.NET Core middleware pipeline process a request, and why does the order of middleware matter?",
                "objective": "Assess request pipeline understanding.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "dotnet-ef-core-performance",
                "text": "An Entity Framework Core endpoint is slow. What would you look for first, and how would you fix an N+1 query?",
                "objective": "Assess ORM performance diagnosis.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-redis",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "redis-cache-invalidation",
                "text": "How would you cache API responses in Redis, and how do you keep the cache consistent when the underlying data changes?",
                "objective": "Assess caching strategy and invalidation.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "redis-data-structures",
                "text": "Name three Redis data structures besides plain strings and a real use case for each.",
                "objective": "Assess Redis feature knowledge.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "redis-cache-stampede",
                "text": "What is a cache stampede, and how would you protect the database when a hot Redis key expires?",
                "objective": "Assess high-load caching pitfalls.",
                "difficulty": "advanced",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-mysql",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "mysql-index-design",
                "text": "How do you decide which indexes a MySQL table needs, and how do you check whether a query actually uses them?",
                "objective": "Assess indexing and EXPLAIN usage.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "mysql-transactions-isolation",
                "text": "Explain transaction isolation levels in MySQL InnoDB and an anomaly that a weaker level allows.",
                "objective": "Assess transaction semantics.",
                "difficulty": "advanced",
                "type": "technical",
            },
            {
                "stable_key": "mysql-joins-and-normalisation",
                "text": "When would you denormalise a MySQL schema instead of joining tables, and what does it cost you?",
                "objective": "Assess schema design trade-offs.",
                "difficulty": "foundational",
                "type": "technical",
            },
        ],
    },
    {
        "concept_id": "skill-kafka",
        "role_concepts": ("technology.software-engineering.backend",),
        "questions": [
            {
                "stable_key": "kafka-partitions-and-ordering",
                "text": "How do partitions affect ordering and throughput in Kafka, and how would you keep events for one user in order?",
                "objective": "Assess partitioning and ordering guarantees.",
                "difficulty": "intermediate",
                "type": "technical",
            },
            {
                "stable_key": "kafka-consumer-groups",
                "text": "Explain consumer groups and offsets in Kafka. What happens when a consumer crashes before committing its offset?",
                "objective": "Assess consumption semantics.",
                "difficulty": "foundational",
                "type": "technical",
            },
            {
                "stable_key": "kafka-idempotent-processing",
                "text": "Kafka can deliver a message more than once. How do you design a consumer so duplicates do not cause wrong results?",
                "objective": "Assess delivery guarantees and idempotency.",
                "difficulty": "advanced",
                "type": "technical",
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


async def _upsert_taxonomy_mappings(
    db: AsyncSession,
    *,
    version_id: uuid.UUID,
    concept_id: str,
    role_concepts: tuple[str, ...] | list[str] = (),
) -> None:
    """Map a question version onto the taxonomy.

    Two purposes, because the selector treats them as different things:
    - PRIMARY_COMPETENCY for the skill concept, which is what P1 emits for a
      taxonomy-backed JD requirement;
    - TARGET_ROLE for the career code, which is the ONLY purpose accepted when
      P1 falls back to career classification. Without these rows that fallback
      always failed closed at P2, so a JD with no resolvable skill requirement
      could never start an interview.
    """
    mappings: list[tuple[str, str, str]] = [
        (_QUESTION_TAXONOMY_VERSION, concept_id, "PRIMARY_COMPETENCY")
    ]
    mappings += [
        (_CAREER_TAXONOMY_VERSION, role_code, "TARGET_ROLE") for role_code in role_concepts
    ]

    for taxonomy_version, mapped_concept, purpose in mappings:
        await db.execute(
            text(
                "INSERT INTO question_version_taxonomy_concepts "
                "(question_version_id, taxonomy_version, concept_id, purpose, relevance) "
                "VALUES (CAST(:qvid AS uuid), :tv, :concept_id, :purpose, :relevance) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "qvid": str(version_id),
                "tv": taxonomy_version,
                "concept_id": mapped_concept,
                "purpose": purpose,
                # A role mapping is a weaker signal than the skill it came from.
                "relevance": 1.0000 if purpose == "PRIMARY_COMPETENCY" else 0.7000,
            },
        )


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
                    role_concepts=tuple(concept_fixture.get("role_concepts", ())),
                )
                if created:
                    seeded += 1
                if q.get("locale", "en-US") != "vi-VN" and stable_key in QUESTION_TEXT_VI:
                    await _upsert_vi_localization(db, stable_key, QUESTION_TEXT_VI[stable_key])

            summary[concept_id] = seeded

        await db.commit()

    return summary


async def _upsert_vi_localization(db: AsyncSession, stable_key: str, question_text: str) -> None:
    """Approved Vietnamese wording of an English seed question (see question_bank_seed_vi)."""
    await db.execute(
        text(
            "INSERT INTO question_localizations "
            "(id, question_version_id, locale, question_text, status, reviewed_by, reviewed_at) "
            "SELECT CAST(:id AS uuid), q.current_approved_version_id, 'vi-VN', :question_text, "
            "'APPROVED', :reviewer, now() "
            "FROM interview_questions q "
            "WHERE q.stable_key = :stable_key AND q.current_approved_version_id IS NOT NULL "
            "ON CONFLICT (question_version_id, locale) DO UPDATE SET "
            "question_text = EXCLUDED.question_text, status = 'APPROVED'"
        ),
        {
            "id": str(_det_uuid("question-localization-vi", stable_key)),
            "question_text": question_text,
            "reviewer": _SEED_APPROVER,
            "stable_key": stable_key,
        },
    )


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
    role_concepts: tuple[str, ...] = (),
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
        # Mappings are re-applied on every run. They are ON CONFLICT DO NOTHING,
        # and skipping them here meant a mapping added to an existing fixture
        # (for example a new TARGET_ROLE row) never reached an already-seeded
        # database.
        await _upsert_taxonomy_mappings(
            db,
            version_id=version_id,
            concept_id=concept_id,
            role_concepts=role_concepts,
        )
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
        # Synchronize canonical_locale, canonical_snapshot and timings if fixture was updated
        thinking, soft, hard = _seed_timing(question_type)
        await db.execute(
            text(
                "UPDATE interview_question_versions "
                "SET canonical_locale = :canonical_locale, "
                "    canonical_snapshot = CAST(:canonical_snapshot AS jsonb), "
                "    thinking_seconds = :thinking, soft_answer_seconds = :soft, "
                "    hard_answer_seconds = :hard "
                "WHERE id = CAST(:version_id AS uuid)"
            ),
            {
                "version_id": str(version_id),
                "canonical_locale": canonical_locale,
                "canonical_snapshot": json.dumps(canonical_snapshot or {}),
                "thinking": thinking,
                "soft": soft,
                "hard": hard,
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
            ":thinking, :soft, :hard, "
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
            **dict(zip(("thinking", "soft", "hard"), _seed_timing(question_type), strict=True)),
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
    # 4. QuestionVersionTaxonomyConcept (PRIMARY_COMPETENCY [+ TARGET_ROLE])
    # ------------------------------------------------------------------
    await _upsert_taxonomy_mappings(
        db,
        version_id=version_id,
        concept_id=concept_id,
        role_concepts=role_concepts,
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
