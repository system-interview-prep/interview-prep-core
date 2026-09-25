import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from src.infrastructure.database import SessionFactory, engine
from src.modules.interviews import planner as planner_module
from src.modules.interviews.router import (
    CreateInterviewSession,
    build_interview_plan,
    create_interview_session,
    get_interview_plan,
)
from src.modules.job_descriptions.schemas import CanonicalJobDescription, JobRequirement
from src.modules.matching.schemas import MatchResult, RequirementResult
from src.modules.user_cvs.schemas import (
    CanonicalResume,
    EvidenceSpan,
    ParsingMetadata,
    TaxonomyRef,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
    reason="PostgreSQL integration database is not enabled",
)


@pytest.mark.asyncio
async def test_planner_persists_grounded_competency_agenda(monkeypatch) -> None:
    user_id = str(uuid4())
    resume_id = str(uuid4())
    job_id = str(uuid4())
    checksum = uuid4().hex + uuid4().hex
    user = {"sub": user_id, "email": f"{user_id}@example.test", "roles": ["CANDIDATE"]}

    resume = CanonicalResume(
        schemaVersion="2.1",
        resumeId=resume_id,
        documentId="cv-doc",
        documentSha256="b" * 64,
    )
    jd_evidence = EvidenceSpan(
        evidenceId="jd-ev-python",
        documentId="jd-doc",
        documentSha256="a" * 64,
        section="requirements",
        text="Python",
        charStart=0,
        charEnd=6,
    )
    jd = CanonicalJobDescription(
        schemaVersion="1.0",
        jobTitle="Backend Engineer",
        requirements=[
            JobRequirement(
                requirementId="req-python",
                kind="skill",
                priority="must_have",
                concept=TaxonomyRef(
                    conceptId="skill.python",
                    scheme="skill",
                    taxonomyVersion="career-v1",
                    label="Python",
                ),
                rawLabel="Python",
                evidenceRefs=["jd-ev-python"],
            )
        ],
        evidence=[jd_evidence],
        parsing=ParsingMetadata(
            parserVersion="test-jd-v1",
            extractionVersion="test-extract-v1",
            parsedAt=datetime.now(UTC),
            status="ready",
        ),
    )

    async def fake_run_match(payload):
        assert payload.resume.resume_id == resume_id
        assert payload.job.job_id == job_id
        return MatchResult(
            resumeId=resume_id,
            jobId=job_id,
            policyVersion="balanced-v1",
            eligibility="review_required",
            suitabilityScore=None,
            fitBand="review_required",
            decision="abstained",
            requirementResults=[
                RequirementResult(
                    requirementId="req-python",
                    status="unknown",
                    score=None,
                    confidence=0.0,
                    evidenceRefs=[],
                    reasonCode="evidence_missing",
                )
            ],
            factorResults=[],
            warnings=[],
        )

    monkeypatch.setattr(planner_module, "evaluate_match", fake_run_match)

    async with SessionFactory() as db:
        try:
            await db.execute(
                text(
                    "INSERT INTO users (id, email, name, provider) "
                    "VALUES (:id, :email, 'P1 Candidate', 'local')"
                ),
                {"id": user_id, "email": user["email"]},
            )
            await db.execute(
                text(
                    "INSERT INTO user_cvs "
                    "(id, user_id, checksum, filename, content_type, size, storage_key, url, "
                    "status, parsed_data, raw_text) "
                    "VALUES (:id, :uid, :checksum, 'p1.pdf', 'application/pdf', 1, "
                    ":storage, '/p1.pdf', 'DONE', CAST(:parsed AS jsonb), '')"
                ),
                {
                    "id": resume_id,
                    "uid": user_id,
                    "checksum": checksum,
                    "storage": f"p1/{resume_id}.pdf",
                    "parsed": json.dumps(resume.model_dump(by_alias=True, mode="json")),
                },
            )
            await db.execute(
                text(
                    "INSERT INTO job_descriptions "
                    "(id, item_type, title, description, requirements, status, search_text, "
                    "listing_status, processing_status, structured_data) "
                    "VALUES (:id, 'JOB_DESCRIPTION', 'Backend Engineer', '', 'Python', "
                    "'ACTIVE', 'Backend Engineer Python', 'ACTIVE', 'DONE', CAST(:structured AS jsonb))"
                ),
                {
                    "id": job_id,
                    "structured": json.dumps(jd.model_dump(by_alias=True, mode="json")),
                },
            )
            await db.commit()

            created = await create_interview_session(
                CreateInterviewSession.model_validate(
                    {
                        "resumeId": resume_id,
                        "jobId": job_id,
                        "mode": "text",
                        "locale": "vi-VN",
                        "durationMinutes": 20,
                    }
                ),
                user=user,
                db=db,
            )

            plan = await build_interview_plan(created["sessionId"], user=user, db=db)
            assert plan["status"] == "READY"
            assert plan["policyVersion"] == "interview-planner-v1"
            assert plan["questionBudget"] == 5
            assert plan["targetQuestionCount"] == 3
            assert plan["targets"][0]["conceptId"] == "skill.python"
            assert plan["targets"][0]["rationale"]["requirementIds"] == ["req-python"]
            assert plan["targets"][0]["rationale"]["matchStatuses"] == ["unknown"]
            assert plan["sourceContext"]["resumeChecksum"] == checksum
            assert plan["sourceContext"]["matchingPolicyVersion"] == "balanced-v1"

            loaded = await get_interview_plan(created["sessionId"], user=user, db=db)
            assert loaded["targets"] == plan["targets"]

            rebuilt = await build_interview_plan(created["sessionId"], user=user, db=db)
            assert rebuilt["targets"] == plan["targets"]
            target_count = await db.scalar(
                text(
                    "SELECT count(*) FROM session_competency_targets "
                    "WHERE plan_id = :plan_id"
                ),
                {"plan_id": plan["planId"]},
            )
            assert target_count == 1

            await db.execute(
                text(
                    "UPDATE interview_session_plans SET status = 'LOCKED' "
                    "WHERE id = :plan_id"
                ),
                {"plan_id": plan["planId"]},
            )
            await db.commit()
            with pytest.raises(HTTPException) as locked_error:
                await build_interview_plan(created["sessionId"], user=user, db=db)
            assert locked_error.value.status_code == 409
        finally:
            await db.rollback()
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await db.execute(text("DELETE FROM job_descriptions WHERE id = :id"), {"id": job_id})
            await db.commit()
            await engine.dispose()
