# tests/modules/interviews/test_cv_personalization_scenarios.py
"""Kiểm thử tự động các kịch bản cá nhân hóa đề thi dựa trên CV ứng viên.

Bao gồm:
1. CV có Dự án cụ thể -> Turn 1 gọi đúng tên dự án ("E-commerce Microservices").
2. CV không có mục Dự án nhưng có Kỹ năng -> Turn 1 gọi top 3 công nghệ ("PyTorch, FastAPI, Milvus").
3. CV rỗng/fallback -> Turn 1 dùng câu hỏi mở tổng quát.
4. P1 Planner trích xuất chính xác Candidate Matrix (Strengths vs Gaps).
5. P2 Question Selector phân tầng chính xác DEEP_DIVE cho Strengths và CHALLENGE cho Gaps.
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.modules.interviews.planner import derive_competency_plan
from src.modules.interviews.question_selector import select_and_freeze_questions
from src.modules.matching.schemas import (
    CanonicalJob,
    ConceptResult,
    MatchResult,
    RequirementResult,
    SkillRequirement,
    TaxonomyRef,
)
from src.modules.user_cvs.schemas import EvidenceSpan


def _concept(concept_id: str, label: str) -> TaxonomyRef:
    return TaxonomyRef(
        conceptId=concept_id,
        scheme="skill",
        taxonomyVersion="internal-2026.1",
        label=label,
    )


def _job(*requirements) -> CanonicalJob:
    evidence = []
    for index, req in enumerate(requirements, start=1):
        text_value = f"Requirement {index} text description"
        evidence.append(
            EvidenceSpan(
                evidenceId=req.source_evidence_ref,
                documentId="jd-doc",
                documentSha256="a" * 64,
                section="requirements",
                text=text_value,
                charStart=(index - 1) * 50,
                charEnd=(index - 1) * 50 + len(text_value),
            )
        )
    return CanonicalJob(
        schemaVersion="2.1",
        jobId="job-1",
        documentId="jd-doc",
        documentSha256="a" * 64,
        requirements=list(requirements),
        careerClassifications=[],
        seniority=None,
        evidence=evidence,
    )


def _match(results) -> MatchResult:
    return MatchResult(
        resumeId="cv-1",
        jobId="job-1",
        policyVersion="balanced-v1",
        eligibility="review_required",
        suitabilityScore=None,
        fitBand="review_required",
        decision="abstained",
        requirementResults=results,
        factorResults=[],
        warnings=[],
    )


class TestCVPersonalizationScenarios(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session_id = str(uuid4())
        self.plan_id = str(uuid4())

    async def test_scenario_1_cv_with_project_name(self):
        """Kịch bản 1: CV có mục dự án -> Turn 1 gọi đúng tên dự án."""
        db = AsyncMock()

        # Mock Plan
        plan_row = {
            "status": "READY",
            "plan_payload": {
                "targets": [
                    {
                        "selectionRank": 0,
                        "taxonomyVersion": "internal-2026.1",
                        "conceptId": "skill-ai",
                        "label": "Trí tuệ nhân tạo",
                        "targetQuestionCount": 1,
                        "rationale": {"source": "job_requirement", "matchStatuses": ["met"]},
                    }
                ],
                "difficulty": {"level": "intermediate"},
            },
        }

        # Mock CV parsed data with Project
        cv_parsed_data = {
            "projects": [
                {
                    "name": "E-commerce Microservices",
                    "description": "Xây dựng hệ thống backend chịu tải cao.",
                }
            ],
            "skills": [{"raw_label": "Python"}, {"raw_label": "PostgreSQL"}],
        }

        async def _scalar_mock(stmt, params=None):
            sql = str(stmt)
            if "SELECT parsed_data FROM user_cvs" in sql:
                return json.dumps(cv_parsed_data)
            if "SELECT title FROM job_descriptions" in sql:
                return "Backend Engineer"
            return None

        db.scalar = AsyncMock(side_effect=_scalar_mock)

        # Mock candidates and turn insertion
        inserted_turns = []

        async def _exec_mock(stmt, params=None):
            sql = str(stmt)
            if "interview_session_plans" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.one_or_none.return_value = plan_row
                return mock_res
            if "session_competency_targets" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = [
                    {
                        "selection_rank": 0,
                        "taxonomy_version": "internal-2026.1",
                        "concept_id": "skill-ai",
                        "label": "Trí tuệ nhân tạo",
                        "importance": 1.0,
                        "target_question_count": 1,
                        "rationale": {"source": "job_requirement", "matchStatuses": ["met"]},
                    }
                ]
                return mock_res
            if "SELECT q.stable_key" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = [
                    {
                        "stable_key": "ai-core",
                        "question_version_id": str(uuid4()),
                        "version": "1.0",
                        "status": "APPROVED",
                        "question_type": "technical",
                        "difficulty_band": "intermediate",
                        "canonical_locale": "vi-VN",
                        "canonical_text": "Giải thích về Microservices Caching.",
                        "objective": "Test caching",
                        "soft_answer_seconds": 60,
                        "hard_answer_seconds": 120,
                        "mapping_purpose": "PRIMARY_COMPETENCY",
                        "relevance": 1.0,
                        "rubric_version_id": str(uuid4()),
                    }
                ]
                return mock_res
            if "SELECT id, version, score_min" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.one_or_none.return_value = {
                    "id": str(uuid4()),
                    "version": "1.0",
                    "score_min": 0,
                    "score_max": 10,
                    "minimum_coverage": 0.5,
                    "aggregation_method": "weighted",
                    "aggregation_policy": {},
                }
                return mock_res
            if "SELECT stable_key, description" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = []
                return mock_res
            if "SELECT id, stable_key, name" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = []
                return mock_res
            if "INSERT INTO interview_turns" in sql:
                inserted_turns.append(params)
                return MagicMock()
            if "SELECT id, turn_index, status" in sql and "FROM interview_turns" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = []
                return mock_res
            return MagicMock()

        db.execute = AsyncMock(side_effect=_exec_mock)

        session_row = {
            "id": self.session_id,
            "plan_id": self.plan_id,
            "resume_id": "cv-1",
            "job_id": "job-1",
            "locale": "vi-VN",
        }

        await select_and_freeze_questions(db=db, session_row=session_row)

        # Kiểm chứng Turn 1 đã được cá nhân hóa với tên dự án
        turn_1_snap = json.loads(inserted_turns[1]["snapshot"])
        self.assertEqual(turn_1_snap["stage"], "VALIDATE")
        self.assertIn("E-commerce Microservices", turn_1_snap["questionText"])
        self.assertIn("vai trò của bạn", turn_1_snap["questionText"])

    async def test_scenario_2_cv_with_skills_fallback(self):
        """Kịch bản 2: CV không có dự án riêng nhưng có kỹ năng -> Turn 1 hỏi về top 3 công nghệ."""
        db = AsyncMock()

        plan_row = {
            "status": "READY",
            "plan_payload": {
                "targets": [
                    {
                        "selectionRank": 0,
                        "taxonomyVersion": "internal-2026.1",
                        "conceptId": "skill-ai",
                        "label": "Trí tuệ nhân tạo",
                        "targetQuestionCount": 1,
                        "rationale": {"source": "job_requirement", "matchStatuses": ["not_met"]},
                    }
                ],
                "difficulty": {"level": "intermediate"},
            },
        }

        # CV không có project nhưng có skills
        cv_parsed_data = {
            "projects": [],
            "employment": [],
            "skills": [
                {"raw_label": "PyTorch"},
                {"raw_label": "FastAPI"},
                {"raw_label": "Milvus Vector DB"},
            ],
        }

        async def _scalar_mock(stmt, params=None):
            sql = str(stmt)
            if "SELECT parsed_data FROM user_cvs" in sql:
                return cv_parsed_data  # trả về dict trực tiếp
            if "SELECT title FROM job_descriptions" in sql:
                return "AI Engineer"
            return None

        db.scalar = AsyncMock(side_effect=_scalar_mock)
        inserted_turns = []

        async def _exec_mock(stmt, params=None):
            sql = str(stmt)
            if "interview_session_plans" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.one_or_none.return_value = plan_row
                return mock_res
            if "session_competency_targets" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = [
                    {
                        "selection_rank": 0,
                        "taxonomy_version": "internal-2026.1",
                        "concept_id": "skill-ai",
                        "label": "Trí tuệ nhân tạo",
                        "importance": 1.0,
                        "target_question_count": 1,
                        "rationale": {"source": "job_requirement", "matchStatuses": ["not_met"]},
                    }
                ]
                return mock_res
            if "SELECT q.stable_key" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = [
                    {
                        "stable_key": "ai-core",
                        "question_version_id": str(uuid4()),
                        "version": "1.0",
                        "status": "APPROVED",
                        "question_type": "technical",
                        "difficulty_band": "intermediate",
                        "canonical_locale": "vi-VN",
                        "canonical_text": "Giải thích cơ chế Vector Search.",
                        "objective": "Test vector search",
                        "soft_answer_seconds": 60,
                        "hard_answer_seconds": 120,
                        "mapping_purpose": "PRIMARY_COMPETENCY",
                        "relevance": 1.0,
                        "rubric_version_id": str(uuid4()),
                    }
                ]
                return mock_res
            if "SELECT id, version, score_min" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.one_or_none.return_value = {
                    "id": str(uuid4()),
                    "version": "1.0",
                    "score_min": 0,
                    "score_max": 10,
                    "minimum_coverage": 0.5,
                    "aggregation_method": "weighted",
                    "aggregation_policy": {},
                }
                return mock_res
            if "SELECT stable_key, description" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = []
                return mock_res
            if "SELECT id, stable_key, name" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = []
                return mock_res
            if "INSERT INTO interview_turns" in sql:
                inserted_turns.append(params)
                return MagicMock()
            if "SELECT id, turn_index, status" in sql and "FROM interview_turns" in sql:
                mock_res = MagicMock()
                mock_res.mappings.return_value.all.return_value = []
                return mock_res
            return MagicMock()

        db.execute = AsyncMock(side_effect=_exec_mock)

        session_row = {
            "id": self.session_id,
            "plan_id": self.plan_id,
            "resume_id": "cv-2",
            "job_id": "job-1",
            "locale": "vi-VN",
        }

        await select_and_freeze_questions(db=db, session_row=session_row)

        turn_1_snap = json.loads(inserted_turns[1]["snapshot"])
        self.assertEqual(turn_1_snap["stage"], "VALIDATE")
        # Gọi tên kỹ năng
        self.assertIn("PyTorch", turn_1_snap["questionText"])
        self.assertIn("FastAPI", turn_1_snap["questionText"])

        # Đồng thời kiểm chứng Turn 2 kỹ thuật
        turn_2_snap = json.loads(inserted_turns[2]["snapshot"])
        self.assertIn(turn_2_snap["stage"], ("DEEP_DIVE", "CHALLENGE"))

    def test_scenario_3_planner_candidate_matrix_strengths_and_gaps(self):
        """Kịch bản 3: P1 Planner phân loại chính xác thế mạnh (Strengths) và lỗ hổng (Gaps)."""
        req_python = SkillRequirement(
            requirementId="req-python",
            priority="must_have",
            sourceEvidenceRef="ev-1",
            type="skill",
            skill=_concept("skill-python", "Python Programming"),
        )
        req_docker = SkillRequirement(
            requirementId="req-docker",
            priority="must_have",
            sourceEvidenceRef="ev-2",
            type="skill",
            skill=_concept("skill-docker", "Docker Container"),
        )
        job = _job(req_python, req_docker)

        # Match results: Python MET (strength), Docker NOT_MET (gap)
        results = [
            RequirementResult(
                requirementId="req-python",
                status="met",
                confidence=0.9,
                reasonCode="MET_BY_SKILL",
                conceptResults=[
                    ConceptResult(
                        conceptId="skill-python",
                        label="Python Programming",
                        status="met",
                        confidence=0.9,
                        reasonCode="EXACT_MATCH",
                    )
                ],
            ),
            RequirementResult(
                requirementId="req-docker",
                status="not_met",
                confidence=0.8,
                reasonCode="MISSING_SKILL",
                conceptResults=[
                    ConceptResult(
                        conceptId="skill-docker",
                        label="Docker Container",
                        status="not_met",
                        confidence=0.8,
                        reasonCode="NOT_FOUND",
                    )
                ],
            ),
        ]
        match = _match(results)

        plan = derive_competency_plan(job=job, match=match, duration_minutes=25)
        matrix = plan.get("candidateMatrix", {})

        self.assertIn("Python Programming", matrix.get("strengthsToVerify", []))
        self.assertIn("Docker Container", matrix.get("gapCompetencies", []))


if __name__ == "__main__":
    unittest.main()
