from src.modules.interviews.evaluation.evaluation_service import _format_expected_points
from src.seeds.question_bank_expected_points_vi import EXPECTED_POINTS_VI


def test_expected_points_render_core_before_bonus():
    block = _format_expected_points(
        [
            {"description": "Ví dụ setTimeout", "importance": "BONUS"},
            {"description": "Closure giữ tham chiếu", "importance": "CORE"},
        ]
    )

    assert block.splitlines() == [
        "Ý cần có (EXPECTED POINTS):",
        "Ý cốt lõi:",
        "- Closure giữ tham chiếu",
        "Ý cộng thêm (không bắt buộc):",
        "- Ví dụ setTimeout",
    ]


def test_questions_without_expected_points_keep_the_generic_rubric():
    assert _format_expected_points(None) == ""
    assert _format_expected_points([]) == ""
    assert _format_expected_points([{"description": "  ", "importance": "CORE"}]) == ""


def test_seed_expected_points_are_publishable():
    # CRITICAL points need a verified knowledge source before a version can be published.
    for stable_key, points in EXPECTED_POINTS_VI.items():
        importances = {importance for _, _, importance in points}
        assert importances <= {"CORE", "BONUS"}, stable_key
        assert "CORE" in importances, stable_key
        keys = [key for key, _, _ in points]
        assert len(keys) == len(set(keys)), stable_key
