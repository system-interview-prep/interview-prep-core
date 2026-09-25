import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.infrastructure.database import SessionFactory
from src.modules.interviews.router import (
    CreateInterviewSession,
    close_interview_session,
    create_interview_session,
    get_interview_session,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_INTEGRATION_TESTS") != "1",
    reason="PostgreSQL integration database is not enabled",
)


@pytest.mark.asyncio
async def test_grounded_session_persists_reads_and_closes() -> None:
    user_id = str(uuid4())
    resume_id = str(uuid4())
    job_id = str(uuid4())
    checksum = uuid4().hex + uuid4().hex
    user = {"sub": user_id, "email": f"{user_id}@example.test", "roles": ["CANDIDATE"]}

    async with SessionFactory() as db:
        try:
            await db.execute(
                text(
                    "INSERT INTO users (id, email, name, provider) "
                    "VALUES (:id, :email, 'P0 Candidate', 'local')"
                ),
                {"id": user_id, "email": user["email"]},
            )
            await db.execute(
                text(
                    "INSERT INTO user_cvs "
                    "(id, user_id, checksum, filename, content_type, size, storage_key, url, status) "
                    "VALUES (:id, :uid, :checksum, 'p0.pdf', 'application/pdf', 1, "
                    ":storage, '/p0.pdf', 'DONE')"
                ),
                {
                    "id": resume_id,
                    "uid": user_id,
                    "checksum": checksum,
                    "storage": f"p0/{resume_id}.pdf",
                },
            )
            await db.execute(
                text(
                    "INSERT INTO job_descriptions "
                    "(id, item_type, title, description, requirements, status, search_text, listing_status, "
                    "processing_status) "
                    "VALUES (:id, 'JOB_DESCRIPTION', 'P0 Engineer', '', '', 'ACTIVE', 'P0 Engineer', "
                    "'ACTIVE', 'DONE')"
                ),
                {"id": job_id},
            )
            await db.commit()

            created = await create_interview_session(
                CreateInterviewSession.model_validate(
                    {
                        "resumeId": resume_id,
                        "jobId": job_id,
                        "mode": "text",
                        "locale": "vi-VN",
                        "durationMinutes": 25,
                    }
                ),
                user=user,
                db=db,
            )

            assert created["resumeId"] == resume_id
            assert created["jobId"] == job_id
            assert created["mode"] == "text"
            assert created["locale"] == "vi-VN"
            assert created["plan"]["status"] == "DRAFT"

            loaded = await get_interview_session(created["sessionId"], user=user, db=db)
            assert loaded["sessionId"] == created["sessionId"]
            assert loaded["plan"]["planId"] == created["plan"]["planId"]

            closed = await close_interview_session(created["sessionId"], user=user, db=db)
            assert closed["status"] == "CLOSED"
            assert closed["endedAt"] is not None
        finally:
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await db.execute(text("DELETE FROM job_descriptions WHERE id = :id"), {"id": job_id})
            await db.commit()
