import json
from datetime import datetime, timezone

from src.modules.user_cvs.infrastructure.repositories.cv_repository import SqlAlchemyCvRepository


class FakeResult:
    def mappings(self):
        return self

    def all(self):
        now = datetime.now(timezone.utc)
        return [
            {
                "id": "cv-1", "user_id": "user-1", "filename": "resume.pdf",
                "content_type": "application/pdf", "size": 1, "storage_key": "cvs/cv-1.pdf",
                "url": None, "status": "DONE", "score": None, "error": None,
                "parse_source": "parser", "raw_text": None, "parsed_data": {},
                "created_at": now, "updated_at": now,
            }
        ]


class FakeSession:
    def __init__(self) -> None:
        self.statement = None
        self.params = None

    async def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return FakeResult()


async def test_repository_filters_cvs_by_exact_career_code_jsonb() -> None:
    session = FakeSession()
    repository = SqlAlchemyCvRepository(session)  # type: ignore[arg-type]

    records = await repository.list_owned("user-1", 50, "technology.software-engineering.backend")

    assert [record.id for record in records] == ["cv-1"]
    assert "parsed_data @> CAST(:career_filter AS jsonb)" in session.statement
    assert json.loads(session.params["career_filter"]) == {
        "careerClassifications": [{"code": "technology.software-engineering.backend"}]
    }
