"""Pre-generation for published jobs, the generation quality gate, and the coverage report."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.interviews.planning import question_coverage, question_generation


def _question(text: str, objective: str = "Assess the skill.") -> dict:
    return {
        "text": text,
        "objective": objective,
        "criteria": [
            {
                "stableKey": "core",
                "name": "Core idea",
                "description": "Explains the core idea.",
                "weight": 1,
                "anchors": [{"level": level, "description": f"Level {level}"} for level in range(4)],
            }
        ],
    }


VI_QUESTION = _question(
    "Bạn xử lý namespace trong XML như thế nào khi đối tác đổi prefix?",
    "Đánh giá cách xử lý namespace khi tích hợp dữ liệu.",
)


def test_language_check_matches_the_locale() -> None:
    assert question_generation.language_matches(VI_QUESTION, "vi-VN")
    assert not question_generation.language_matches(VI_QUESTION, "en-US")
    english = _question("How do you validate XML against an XSD?")
    assert question_generation.language_matches(english, "en-US")
    assert not question_generation.language_matches(english, "vi-VN")


def test_near_duplicate_detection_is_lexical() -> None:
    existing = ["How do you validate an XML payload against an XSD before processing it?"]
    assert question_generation.is_near_duplicate(
        "How would you validate the XML payload against an XSD?", existing
    )
    assert not question_generation.is_near_duplicate(
        "Explain how an XXE attack works and how to stop it.", existing
    )


@pytest.mark.asyncio
async def test_gate_counts_every_rejection_reason(monkeypatch) -> None:
    async def judge(*, questions, **_):
        # Reject the second surviving question.
        return [
            {"index": 0, "on_skill": True, "difficulty_ok": True, "verbal": True, "sound": True, "concise": True},
            {"index": 1, "on_skill": False, "difficulty_ok": True, "verbal": True, "sound": True, "concise": True},
        ]

    monkeypatch.setattr(question_generation, "judge_question_payloads", judge)
    payloads = [
        VI_QUESTION,
        {"text": "missing criteria", "objective": "x"},
        _question("How do you validate XML against an XSD?"),  # English in a Vietnamese batch
        _question("Bạn xử lý namespace trong XML như thế nào khi đối tác đổi prefix?", "Đánh giá lại."),
        _question(
            "Tấn công XXE hoạt động ra sao và bạn cấu hình parser thế nào để chặn?", "Đánh giá bảo mật."
        ),
    ]

    accepted, stats = await question_generation.gate_questions(
        payloads=payloads,
        existing_texts=[],
        skill_label="XML",
        competency_label="Backend Engineering",
        difficulty="intermediate",
        locale="vi-VN",
    )

    assert [item["text"] for item in accepted] == [VI_QUESTION["text"]]
    assert stats == {"structure": 1, "language": 1, "duplicate": 1, "judge": 1}


def _patch_generation_db(monkeypatch, filed: list) -> AsyncMock:
    monkeypatch.setattr(
        question_generation,
        "load_active_skill_taxonomy",
        AsyncMock(return_value=SimpleNamespace(version="v1", skills={"skill-xml": ("XML",)})),
    )
    monkeypatch.setattr(question_generation, "_competency_for", AsyncMock(return_value="backend"))
    monkeypatch.setattr(question_generation, "_existing_question_texts", AsyncMock(return_value=[]))

    async def file_question(db, **kwargs):
        filed.append(kwargs["text"])

    monkeypatch.setattr(question_generation, "file_generated_question", file_question)
    db = AsyncMock()
    db.scalar.return_value = "Backend Engineering"
    return db


@pytest.mark.asyncio
async def test_generation_fails_closed_when_the_judge_is_unavailable(monkeypatch) -> None:
    filed: list = []
    db = _patch_generation_db(monkeypatch, filed)

    async def payloads(**_):
        return [VI_QUESTION]

    async def judge_down(**_):
        raise TimeoutError("provider down")

    monkeypatch.setattr(question_generation, "generate_question_payloads", payloads)
    monkeypatch.setattr(question_generation, "judge_question_payloads", judge_down)

    count = await question_generation.generate_and_file(
        db, concept_id="skill-xml", job_role=None, difficulty="intermediate", locale="vi-VN", count=1
    )

    assert count == 0 and filed == []
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_pregeneration_may_file_three_variants(monkeypatch) -> None:
    filed: list = []
    db = _patch_generation_db(monkeypatch, filed)
    variants = [
        VI_QUESTION,
        _question(
            "Tấn công XXE hoạt động ra sao và bạn cấu hình parser thế nào để chặn?", "Đánh giá bảo mật."
        ),
        _question(
            "Khi nào bạn vẫn chọn XML thay vì JSON để trao đổi dữ liệu giữa hệ thống?", "Đánh giá đánh đổi."
        ),
    ]

    async def payloads(*, count, **_):
        return variants[:count]

    monkeypatch.setattr(question_generation, "generate_question_payloads", payloads)

    count = await question_generation.generate_and_file(
        db,
        concept_id="skill-xml",
        job_role=None,
        difficulty="intermediate",
        locale="vi-VN",
        count=3,
        max_count=question_generation.MAX_PREGENERATED_PER_TARGET,
    )

    assert count == 3 and len(filed) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("reachable", "expected_count"), [(3, None), (1, 2)])
async def test_ensure_generates_only_what_selection_cannot_reach(
    monkeypatch, reachable, expected_count
) -> None:
    from src.modules.interviews.planning import question_selector

    monkeypatch.setattr(
        question_selector, "reachable_candidates", AsyncMock(return_value=[object()] * reachable)
    )
    generate = AsyncMock(return_value=0)
    monkeypatch.setattr(question_generation, "generate_and_file", generate)
    db = AsyncMock()

    await question_generation.ensure_generated_questions(
        db,
        target={"conceptId": "skill-xml"},
        job_concepts=set(),
        job_role=None,
        difficulty="intermediate",
        locale="vi-VN",
        desired=3,
        max_count=3,
    )

    # Counted under a per-skill advisory lock, released by the final commit.
    assert "pg_advisory_xact_lock" in str(db.execute.call_args_list[0].args[0])
    db.commit.assert_awaited()
    if expected_count is None:
        generate.assert_not_called()
    else:
        assert generate.call_args.kwargs["count"] == expected_count


def test_only_must_have_skills_are_prepared(monkeypatch) -> None:
    monkeypatch.setattr(question_coverage, "_requirement_concepts", lambda requirement: requirement.concepts)

    def concept(concept_id: str):
        return SimpleNamespace(concept_id=concept_id, taxonomy_version="v1", label=concept_id)

    job = SimpleNamespace(
        requirements=[
            SimpleNamespace(priority="must_have", concepts=[concept("skill-xml"), concept("skill-json")]),
            SimpleNamespace(priority="preferred", concepts=[concept("skill-kubernetes")]),
            SimpleNamespace(priority="must_have", concepts=[concept("skill-xml")]),
        ]
    )

    targets = question_coverage._must_have_targets(job)

    assert [t["conceptId"] for t in targets] == ["skill-xml", "skill-json"]
    assert targets[0]["rationale"] == {"source": "job_requirement"}


def test_enqueue_never_fails_the_caller(monkeypatch) -> None:
    from src.workers import celery_app as celery_module

    settings = SimpleNamespace(question_generation_enabled=True, question_pregeneration_enabled=True)
    monkeypatch.setattr(question_coverage, "get_settings", lambda: settings)

    send = MagicMock()
    monkeypatch.setattr(celery_module.celery_app, "send_task", send)
    assert question_coverage.enqueue_question_coverage(["job-1"]) is True
    send.assert_called_once_with(question_coverage.PREPARE_TASK_NAME, args=[{"job_ids": ["job-1"]}])

    send.side_effect = ConnectionError("broker down")
    assert question_coverage.enqueue_question_coverage(["job-1"]) is False

    settings.question_pregeneration_enabled = False
    send.reset_mock()
    assert question_coverage.enqueue_question_coverage(["job-1"]) is False
    send.assert_not_called()


@pytest.mark.asyncio
async def test_coverage_report_splits_demo_and_regular_sessions(monkeypatch) -> None:
    monkeypatch.setattr(question_coverage, "_active_jobs", AsyncMock(return_value=[]))
    rows = [
        {"duration_minutes": 25, "selection": {"questionSources": {"question_bank": 4}}},
        {
            "duration_minutes": 25,
            "selection": {"generatedQuestionCount": 1, "questionSources": {"generated_unreviewed": 1}},
        },
        {"duration_minutes": 25, "selection": {"uncoveredTargets": [{"conceptId": "skill-x"}]}},
        {"duration_minutes": 3, "selection": {"questionSources": {"question_bank": 2}}},
    ]
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    db = AsyncMock()
    db.execute.return_value = result

    report = await question_coverage.question_coverage_report(db)

    regular = report["sessions"]["regular"]
    assert regular["sessions"] == 3
    assert (regular["fullyReviewedPct"], regular["withGeneratedPct"], regular["withUncoveredPct"]) == (
        33.3,
        33.3,
        33.3,
    )
    assert regular["questionSources"] == {"question_bank": 4, "generated_unreviewed": 1}
    assert report["sessions"]["demo"]["fullyReviewedPct"] == 100.0


# ── DB-backed (opt-in with RUN_DB_INTEGRATION_TESTS=1, rolled back) ─────────

from sqlalchemy import text  # noqa: E402

from tests.modules.interviews.test_question_fallback import _db, rollback_db  # noqa: E402, F401


@_db
@pytest.mark.asyncio
async def test_published_job_gets_three_gated_drafts_once(rollback_db, monkeypatch) -> None:  # noqa: F811
    job_id = await rollback_db.scalar(
        text(
            "SELECT id FROM job_descriptions "
            "WHERE title = 'Software Engineer, Backend' AND listing_status = 'ACTIVE'"
        )
    )
    if job_id is None:
        pytest.skip("demo JD not ingested")
    # Make XML uncovered inside the rolled-back transaction.
    await rollback_db.execute(
        text(
            "UPDATE interview_questions SET retired_at = now() WHERE id IN ("
            "SELECT qv.question_id FROM interview_question_versions qv "
            "JOIN question_version_taxonomy_concepts m ON m.question_version_id = qv.id "
            "WHERE m.concept_id = 'skill-xml')"
        )
    )
    variants = iter(
        [
            "Bạn xử lý namespace trong XML như thế nào khi đối tác đổi prefix?",
            "Tấn công XXE hoạt động ra sao và bạn cấu hình parser thế nào để chặn?",
            "Khi nào bạn vẫn chọn XML thay vì JSON để trao đổi dữ liệu giữa hệ thống?",
        ]
    )
    calls = []

    async def payloads(*, count, **kwargs):
        calls.append({"count": count, **kwargs})
        objective = "Đánh giá kỹ năng XML trong tích hợp backend."
        return [_question(next(variants), objective) for _ in range(count)]

    monkeypatch.setattr(question_generation, "generate_question_payloads", payloads)

    first = await question_coverage.prepare_job_question_coverage(rollback_db, job_ids=[str(job_id)])
    second = await question_coverage.prepare_job_question_coverage(rollback_db, job_ids=[str(job_id)])

    assert first["generated"] == 3 and first["stillShort"] == []
    assert second["generated"] == 0
    # Only the taxonomy label reaches the model, never JD text.
    assert calls[0]["skill_label"] == "XML" and calls[0]["locale"] == "vi-VN"
    drafts = await rollback_db.scalar(
        text(
            "SELECT count(*) FROM interview_question_versions qv "
            "JOIN interview_questions q ON q.id = qv.question_id AND q.retired_at IS NULL "
            "JOIN question_version_taxonomy_concepts m ON m.question_version_id = qv.id "
            "WHERE m.concept_id = 'skill-xml' AND qv.status = 'IN_REVIEW' "
            "AND qv.created_by = 'system-question-generator' AND qv.canonical_locale = 'vi-VN'"
        )
    )
    assert drafts == 3
