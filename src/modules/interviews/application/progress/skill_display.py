"""How a skill is named and grouped for the candidate on the progress page.

The progress page used the labels of the system's skill taxonomy and of its
preset questions: "Giới thiệu & Khởi động" (the greeting, not a skill),
"Xác thực CV & Dự án" (reads as checking whether the candidate lied),
"Kỹ năng mềm & Tình huống (STAR)", "Backend Engineering" (the job family, used
when a JD yields no skill), "Kubernetes or Docker Swarm", "Monitoring". It also
listed every skill on one flat level, so Python, FastAPI, PostgreSQL and Redis
stood side by side and the candidate could not see which area is strong or
weak (research 20/10, PROGRESS_EVIDENCE_RESEARCH).

Here: which concepts are not skills, a plain name for internal or unclear
labels, and the area each skill belongs to. Tool names (Python, Redis) stay as
they are: IT candidates know them by those names.
"""

from __future__ import annotations

from src.modules.interviews.core.interview_rounds import AREAS as ROUND_AREAS
from src.modules.interviews.core.interview_rounds import area_of

# Concepts that are part of the interview but not a skill to track.
HIDDEN_CONCEPTS = frozenset({"warmup"})
HIDDEN_STAGES = frozenset({"WARM_UP", "CLOSING"})

# Rounds (areas) and the concept -> round map live with the planner, which
# spreads questions over them.
AREAS = ROUND_AREAS

# Plain names for labels a candidate would not read as a skill.
_DISPLAY_NAMES: dict[str, str] = {
    "cv-validation": "Kể về dự án trong CV",
    "soft-skill.star": "Kể kinh nghiệm làm việc và xử lý tình huống",
    "skill-container-orchestrator": "Điều phối container (Kubernetes, Docker Swarm)",
    "skill-monitoring": "Giám sát hệ thống (monitoring)",
    "skill-cicd": "CI/CD: tự động build và triển khai",
    "skill-rest-api": "Thiết kế REST API",
    "skill-http": "HTTP",
    "skill-artificial-intelligence": "Trí tuệ nhân tạo (AI)",
    "skill-machine-learning": "Học máy (machine learning)",
    "skill-natural-language-processing": "Xử lý ngôn ngữ tự nhiên (NLP)",
    "skill-generative-ai": "AI tạo sinh (generative AI)",
    "skill-large-language-models": "Mô hình ngôn ngữ lớn (LLM)",
    "skill-retrieval-augmented-generation": "RAG: kết hợp tìm kiếm với LLM",
    "skill-semantic-search": "Tìm kiếm ngữ nghĩa (semantic search)",
    "skill-named-entity-recognition": "Nhận diện thực thể (NER)",
    "skill-oracle": "Oracle Database",
    "skill-gcp": "Google Cloud",
}
# Used when the JD gave no skill and the interview fell back to the job family.
GENERAL_NAME = "Kiến thức chuyên môn chung của vị trí"


def is_tracked(concept_id: str | None, stage: str | None) -> bool:
    """A turn counts toward a skill unless it is the greeting or the closing Q&A."""
    if str(stage or "").upper() in HIDDEN_STAGES:
        return False
    return str(concept_id or "") not in HIDDEN_CONCEPTS


def display_name(concept_id: str | None, label: str | None) -> str:
    """The name the candidate sees; the taxonomy label when it is already clear."""
    key = str(concept_id or "")
    if key in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[key]
    if area_of(key) == "general":
        return GENERAL_NAME
    return str(label or key)
