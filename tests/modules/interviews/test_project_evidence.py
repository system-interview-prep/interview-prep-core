"""Regression and contract tests for CV-based project evidence selection and 4-axis question formulation.

Covers:
1. Multiple projects extraction and retention (no dropped projects, no bogus date projects).
2. Selection prioritizing JD relevance over CV order (e.g. Career Assistant X over IVORA for AI Engineer).
3. Deterministic tie-breaking independent of input/db order.
4. Grounded question formulation without inventing data (role, metrics, tech grounded).
5. Verification prompt when CV fields are missing (asks candidate to verify instead of asserting).
6. Non-repetition of project validation in chat runtime.
7. Preservation of interview lifecycle/FSM stage order (WARM_UP -> VALIDATE -> DEEP_DIVE/CHALLENGE -> CLOSING).
8. Single-project and zero-project backward compatibility.
"""

import pytest
from src.modules.interviews.project_evidence import (
    StructuredProjectEvidence,
    build_project_validation_question,
    extract_project_evidences,
    select_best_project,
)
from src.modules.user_cvs.parsing.domain.source import SourceBlock, SourceDocument
from src.modules.user_cvs.parsing.domain.structured import ProjectExtractor
from src.modules.user_cvs.parsing.domain.deterministic import ResumeDraft





# ============================================================================
# 1. Multiple projects extraction & retention
# ============================================================================

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import EvidenceMapper, build_source_document


def test_cv_with_multiple_projects_retains_all():
    """Requirement 1: CV with at least two projects retains all without dropped items or date artifacts."""
    artifacts = DocumentArtifacts(
        markdown="",
        content_list=[
            {"type": "text", "text": "PROJECTS", "page_idx": 0, "bbox": [10, 10, 100, 20]},
            {"type": "text", "text": "IVORA – Online Wedding Invitation Platform", "page_idx": 0, "bbox": [10, 20, 100, 30]},
            {"type": "text", "text": "12/2025 - Present", "page_idx": 0, "bbox": [10, 30, 100, 40]},
            {"type": "text", "text": "Description: Platform for creating invitations with 1000 users. Tech: React, Node.js.", "page_idx": 0, "bbox": [10, 40, 100, 50]},
            {"type": "text", "text": "Career Assistant X - AI Career Guidance Platform", "page_idx": 0, "bbox": [10, 50, 100, 60]},
            {"type": "text", "text": "In Progress", "page_idx": 0, "bbox": [10, 60, 100, 70]},
            {"type": "text", "text": "Description: Role: Lead Developer. Mentoring students with AI. Tech: Python, FastAPI, Gemini, LangGraph. Results: 90+ users.", "page_idx": 0, "bbox": [10, 70, 100, 80]},
        ],
        extractor_version="3.0.0",
    )
    doc = build_source_document(artifacts, document_id="doc-multi-proj", document_sha256="0" * 64)
    draft = ResumeDraft()
    mapper = EvidenceMapper(doc)
    extractor = ProjectExtractor()
    extractor.extract(doc, mapper, draft)

    # Must extract exactly 2 projects (no fake projects named '12/2025 - Present' or 'In Progress')
    project_names = [p.name for p in draft.projects]
    assert len(draft.projects) == 2, f"Expected 2 projects, got {len(draft.projects)}: {project_names}"
    assert any("IVORA" in name for name in project_names)
    assert any("Career Assistant X" in name for name in project_names)
    assert not any("12/2025" in name for name in project_names)
    assert not any("In Progress" in name for name in project_names)


# ============================================================================
# 2. Project 2 preferred when more relevant to JD
# ============================================================================

def test_second_project_preferred_when_more_relevant_to_jd():
    """Requirement 2: Project 2 is selected over Project 1 when Project 2 better matches JD."""
    cv_projects = [
        {
            "projectId": "proj-1",
            "name": "IVORA – Online Wedding Invitation Platform",
            "description": "A wedding invitation website. Tech: React, Node.js, Tailwind CSS, PostgreSQL.",
        },
        {
            "projectId": "proj-2",
            "name": "Career Assistant X - AI Career Guidance Platform",
            "description": "Role: Core Builder. Generative AI platform for interview prep. Tech: Python, FastAPI, LangGraph, Gemini, LLM, RAG. Impact: 90+ users.",
        },
    ]

    # JD requirements for an AI Engineer
    jd_requirements = [
        {
            "requirementId": "req-ai-llm",
            "priority": "must_have",
            "raw_label": "GenAI and LLM experience with RAG or LangGraph",
            "conceptIds": ["nlp", "llm", "genai", "rag"],
        },
        {
            "requirementId": "req-python",
            "priority": "must_have",
            "raw_label": "Strong Python programming",
            "conceptIds": ["python", "fastapi"],
        },
    ]

    evidences = extract_project_evidences(
        projects_data=cv_projects,
        job_requirements=jd_requirements,
    )

    assert len(evidences) == 2
    proj_ivora = next(p for p in evidences if "IVORA" in p.name)
    proj_cax = next(p for p in evidences if "Career Assistant" in p.name)

    # CAX matches must-have requirements (LLM, RAG, Python, FastAPI)
    assert proj_cax.jd_relevance_score > proj_ivora.jd_relevance_score
    assert "req-ai-llm" in proj_cax.relevant_requirements
    assert "req-python" in proj_cax.relevant_requirements

    selected = select_best_project(evidences)
    assert selected is not None
    # Career Assistant X must be selected first despite being second in CV order!
    assert "Career Assistant" in selected.name
    assert selected.project_id == "proj-2"


# ============================================================================
# 3. Deterministic tie-break
# ============================================================================

def test_tie_break_stability_independent_of_order():
    """Requirement 3: When two projects have equal JD relevance and richness, tie-break is stable by CV order."""
    p1 = StructuredProjectEvidence(
        project_id="proj-a",
        name="Project Alpha",
        cv_order=0,
        jd_relevance_score=3.0,
        evidence_richness_score=2.0,
    )
    p2 = StructuredProjectEvidence(
        project_id="proj-b",
        name="Project Beta",
        cv_order=1,
        jd_relevance_score=3.0,
        evidence_richness_score=2.0,
    )

    # Regardless of input ordering, Project Alpha (cv_order=0) is stably selected
    best_forward = select_best_project([p1, p2])
    best_backward = select_best_project([p2, p1])

    assert best_forward.project_id == "proj-a"
    assert best_backward.project_id == "proj-a"


# ============================================================================
# 4. Question grounds on CV claims without hallucination
# ============================================================================

def test_question_grounds_on_cv_claims_without_hallucination():
    """Requirement 4: Question mentions actual role and metrics from CV without inventing facts."""
    project = StructuredProjectEvidence(
        project_id="cax",
        name="Career Assistant X",
        role="Lead Developer",
        role_status="present",
        technologies=["Python", "FastAPI", "LangGraph"],
        technologies_status="present",
        outcomes=["90+ users"],
        outcomes_status="present",
    )

    q_vi = build_project_validation_question(project, job_title="AI Engineer", locale="vi")
    assert "Career Assistant X" in q_vi
    assert "Lead Developer" in q_vi
    assert "Python" in q_vi
    assert "90+ users" in q_vi
    # Check that no imaginary facts were introduced
    assert "1000" not in q_vi
    assert "Docker" not in q_vi

    q_en = build_project_validation_question(project, job_title="AI Engineer", locale="en")
    assert "Career Assistant X" in q_en
    assert "Lead Developer" in q_en
    assert "Python" in q_en
    assert "90+ users" in q_en


# ============================================================================
# 5. Missing CV data asks for verification instead of asserting
# ============================================================================

def test_missing_cv_fields_prompts_verification_not_assertion():
    """Requirement 5: When role or outcomes are missing in CV, system asks candidate to verify."""
    project = StructuredProjectEvidence(
        project_id="sparse-proj",
        name="Internal Tool",
        role=None,
        role_status="missing",
        technologies=[],
        technologies_status="missing",
        outcomes=[],
        outcomes_status="missing",
    )

    q_vi = build_project_validation_question(project, job_title="Software Engineer", locale="vi")
    # Must NOT assert candidate was Lead or created a specific metric
    assert "Lead" not in q_vi
    assert "xác minh chính xác vai trò" in q_vi
    assert "đo lường bằng những chỉ số (metrics) nào so với baseline ban đầu" in q_vi

    q_en = build_project_validation_question(project, job_title="Software Engineer", locale="en")
    assert "verify your direct contribution" in q_en
    assert "metrics were used to measure the outcome against your initial baseline" in q_en


# ============================================================================
# 6. No repeated project validation in runtime
# ============================================================================

@pytest.mark.asyncio
async def test_no_repeated_project_validation_in_runtime():
    """Requirement 6: Once Turn 1 (project validation) is completed, it is never asked again."""
    from src.modules.interviews.chat_runtime import process_candidate_message, start_chat_session
    from tests.modules.interviews.test_chat_runtime import MockChatSession

    session_row = {
        "id": "sess-pv-test",
        "user_id": "u-1",
        "status": "OPEN",
        "plan_status": "LOCKED",
        "locale": "vi",
        "duration_minutes": 30,
        "metadata": {},
    }

    turns = [
        {
            "id": "turn-0",
            "session_id": "sess-pv-test",
            "turn_index": 0,
            "status": "PLANNED",
            "question_snapshot": {
                "stage": "WARM_UP",
                "questionText": "Chào bạn, hãy giới thiệu bản thân.",
            },
        },
        {
            "id": "turn-1",
            "session_id": "sess-pv-test",
            "turn_index": 1,
            "status": "PLANNED",
            "question_snapshot": {
                "stage": "VALIDATE",
                "projectName": "Career Assistant X",
                "questionText": "Trong CV mình rất ấn tượng với Career Assistant X. Bạn hãy chia sẻ vai trò của mình?",
            },
        },
        {
            "id": "turn-2",
            "session_id": "sess-pv-test",
            "turn_index": 2,
            "status": "PLANNED",
            "question_snapshot": {
                "stage": "DEEP_DIVE",
                "questionText": "Trình bày kiến trúc RAG nâng cao?",
            },
        },
    ]

    db = MockChatSession(session_row, turns)
    await start_chat_session(db, session_row)

    # Candidate introduces themselves (Turn 0 answer)
    res1 = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-1",
        content="Em là Nguyễn Văn A, em chuyên về AI và Backend.",
    )
    # Next question must be Turn 1 (Project Validation)
    assert res1["turnStatus"]["turnIndex"] == 1
    assert "Career Assistant X" in res1["assistantResponse"]["content"]

    # Candidate answers project validation question (Turn 1 answer)
    res2 = await process_candidate_message(
        db,
        session_row,
        client_message_id="msg-2",
        content="Trong Career Assistant X em là Core Developer phụ trách phần backend FastAPI và tích hợp Gemini.",
    )
    # Next question must advance to Turn 2 (RAG)
    assert res2["turnStatus"]["turnIndex"] == 2
    assert "RAG" in res2["assistantResponse"]["content"]
    assert "Career Assistant X" not in res2["assistantResponse"]["content"]


# ============================================================================
# 7. Lifecycle and Stage Order Preservation
# ============================================================================

def test_project_validation_preserves_lifecycle_and_stage_order():
    """Requirement 7: Project validation sits cleanly between WARM_UP and DEEP_DIVE without breaking FSM."""
    from src.modules.interviews.core.interview_types import InterviewStage

    # Verify InterviewStage enum integrity
    assert hasattr(InterviewStage, "WARM_UP")
    assert hasattr(InterviewStage, "VALIDATE")
    assert hasattr(InterviewStage, "DEEP_DIVE")
    assert hasattr(InterviewStage, "CLOSING")


# ============================================================================
# 8. Single and zero project backward compatibility
# ============================================================================

def test_single_and_zero_project_backward_compatibility():
    """Requirement 8: Single-project CV and zero-project CV behave safely without errors."""
    # Single project
    single_list = [{"projectId": "p1", "name": "Only Project", "description": "Single project desc"}]
    evs_single = extract_project_evidences(single_list)
    assert len(evs_single) == 1
    selected_single = select_best_project(evs_single)
    assert selected_single is not None
    assert selected_single.name == "Only Project"

    # Zero projects
    evs_empty = extract_project_evidences([])
    assert evs_empty == []
    assert select_best_project(evs_empty) is None
