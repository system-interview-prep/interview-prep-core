"""The question bank learns from real interviews: statistics, rules, AI proposal guard."""

import asyncio

from src.modules.interviews.evaluation import bank_insights as bi


def _row(
    session, user, score=None, outcome="GRADED", points=None, overall=None, seconds=60, qid="q1", text="Câu A"
):
    return {
        "session_id": session,
        "user_id": user,
        "status": "ANSWERED",
        "version_id": f"v-{qid}",
        "current_version_id": f"v-{qid}",
        "question_id": qid,
        "stable_key": f"key-{qid}",
        "canonical_text": text,
        "difficulty_band": "intermediate",
        "canonical_locale": "vi-VN",
        "version_status": "APPROVED",
        "answer_seconds": seconds,
        "hard_answer_seconds": 180,
        "score": score,
        "outcome": outcome,
        "rubric_points": points or [],
        "overall_score": overall,
    }


def test_collect_counts_skips_overtime_repeats_and_rubric_points() -> None:
    rows = [
        _row(
            "s1",
            "u1",
            6,
            points=[{"stable_key": "c1", "status": "PARTIAL", "quote": "chỉ nói index"}],
            overall=6,
        ),
        _row("s2", "u1", outcome="SKIPPED"),
        _row("s3", "u2", 2, seconds=400, points=[{"stable_key": "c1", "status": "MISSING"}], overall=3),
    ]
    stats = bi.collect(
        rows, [{"version_id": "v-q1", "stable_key": "c1", "name": "Đánh đổi khi ghi"}], {"v-q1": "skill-db"}
    )
    q = stats["q1"]
    assert (q.asked, q.skipped, q.overtime, q.repeat_users) == (3, 1, 1, 1)
    assert q.scores == [6.0, 2.0] and q.pairs == [(6.0, 6.0), (2.0, 3.0)]
    assert q.criteria["c1"] == {"PARTIAL": 1, "MISSING": 1}
    assert q.criterion_names["c1"] == "Đánh đổi khi ghi" and q.quotes["c1"] == ["chỉ nói index"]
    assert q.skill == "skill-db"


def _stats(
    scores, *, overall=None, skipped=0, points=None, sessions=None, qid="q1", text="Câu A", skill="skill-db"
):
    rows = [
        _row(
            f"s{i}",
            f"u{i}",
            score,
            points=points,
            overall=(overall[i] if overall else None),
            qid=qid,
            text=text,
        )
        for i, score in enumerate(scores)
    ]
    rows += [_row(f"k{i}", f"x{i}", outcome="SKIPPED", qid=qid, text=text) for i in range(skipped)]
    return bi.collect(rows, [], {f"v-{qid}": skill})


def _kinds(found):
    return sorted((s.kind, s.reason) for s in found)


def test_rules_flag_skips_easy_questions_and_rubric_gaps() -> None:
    assert ("REVISE", "skipped_often") in _kinds(bi.analyze(_stats([6, 7, 5], skipped=3)))
    easy = bi.analyze(_stats([10, 10, 9.8, 10, 10]))
    assert ("DIFFICULTY", "too_easy") in _kinds(easy)
    assert next(s for s in easy if s.kind == "DIFFICULTY").proposal == {"difficultyBand": "advanced"}
    gap = bi.analyze(_stats([4, 5, 6, 5, 4], points=[{"stable_key": "c1", "status": "MISSING"}]))
    found = next(s for s in gap if s.kind == "RUBRIC_GAP")
    assert found.criterion_key == "c1" and found.evidence["missedRate"] == 1.0
    assert found.evidence["lowData"] is True  # 5 answers: a hint, not a calibration


def test_too_few_answers_raise_nothing() -> None:
    assert bi.analyze(_stats([10, 10])) == []


def test_negative_discrimination_suggests_retiring() -> None:
    scores = [9, 8, 9, 7, 8, 2, 3, 2, 3, 1]
    overall = [2, 3, 2, 3, 3, 8, 9, 8, 9, 9]
    found = bi.analyze(_stats(scores, overall=overall))
    assert ("RETIRE", "negative_discrimination") in _kinds(found)
    assert abs(bi.pearson([(1, 1), (2, 2), (3, 3)]) - 1.0) < 1e-9


def test_near_duplicates_of_the_same_skill() -> None:
    stats = {
        **_stats([5], qid="a", text="Giải thích cách đánh chỉ mục trong cơ sở dữ liệu quan hệ"),
        **_stats([5], qid="b", text="Giải thích cách đánh chỉ mục trong cơ sở dữ liệu quan hệ lớn"),
        **_stats(
            [5], qid="c", text="Giải thích cách đánh chỉ mục trong cơ sở dữ liệu quan hệ", skill="other"
        ),
    }
    dups = [s for s in bi.analyze(stats) if s.kind == "DUPLICATE"]
    assert [(s.question_id, s.evidence["twinQuestionId"]) for s in dups] == [("b", "a")]


def test_proposal_must_not_copy_candidate_words() -> None:
    quotes = ["em nghĩ là index giúp truy vấn nhanh hơn rất nhiều lần"]
    assert bi.copies_candidate("Bạn nghĩ index giúp truy vấn nhanh hơn rất nhiều lần không?", quotes)
    assert not bi.copies_candidate("Khi bảng được ghi liên tục, chỉ mục ảnh hưởng thế nào?", quotes)


def test_gap_proposal_parses_masks_and_rejects_copies(monkeypatch) -> None:
    stats = _stats(
        [4, 5, 6, 5, 4], points=[{"stable_key": "c1", "status": "PARTIAL", "quote": "liên hệ a@b.com nhé"}]
    )["q1"]
    suggestion = next(s for s in bi.analyze({"q1": stats}) if s.kind == "RUBRIC_GAP")
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return (
            '{"misconception": "Chỉ nói tốc độ đọc", "probe": "Khi ghi nhiều thì sao?", '
            '"anchor_partial": "a", "anchor_full": "b"}'
        )

    monkeypatch.setattr(bi, "generate_text", fake)
    proposal = asyncio.run(bi.propose_for_gap(stats, suggestion))
    assert proposal["probe"] == "Khi ghi nhiều thì sao?" and proposal["basedOnAnswers"] == 5
    assert "a@b.com" not in seen["input_text"]

    async def bad(**kwargs):
        return "not json"

    monkeypatch.setattr(bi, "generate_text", bad)
    assert asyncio.run(bi.propose_for_gap(stats, suggestion)) is None
