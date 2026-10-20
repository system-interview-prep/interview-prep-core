# tests/modules/interviews/test_checklist_grading.py
"""Checklist grading: labels must be backed by verbatim quotes; code computes the score."""

import pytest

from src.modules.interviews.evaluation.evaluation_engine import InterviewEvaluationEngine
from src.modules.interviews.evaluation.evaluation_types import TurnEvaluationInput
from src.modules.interviews.evaluation.rubric_grading import (
    BONUS,
    CORE,
    COVERED,
    INCORRECT,
    MISSING,
    PARTIAL,
    ChecklistOutputError,
    ExpectedPoint,
    PointVerdict,
    checklist_score,
    expected_points_from_snapshot,
    majority_verdicts,
    quote_is_verified,
    validate_checklist,
    verified_quotes,
)

POINTS_SNAPSHOT = [
    {"stable_key": "additive-safe", "importance": "CORE", "display_order": 0,
     "description": "Thêm field mới, tùy chọn là an toàn vì client cũ bỏ qua field lạ."},
    {"stable_key": "unsafe-changes", "importance": "CORE", "display_order": 1,
     "description": "Không an toàn: đổi tên, đổi kiểu, xóa field hoặc đổi nghĩa field."},
    {"stable_key": "breaking-strategy", "importance": "CORE", "display_order": 2,
     "description": "Khi buộc phải phá vỡ: version API và có thời gian deprecate."},
    {"stable_key": "contract-tests", "importance": "BONUS", "display_order": 3,
     "description": "Kiểm chứng bằng contract test hoặc schema registry."},
]
EXPECTED = expected_points_from_snapshot(POINTS_SNAPSHOT)
ANSWER = (
    "Thêm field optional thì an toàn vì client cũ sẽ bỏ qua field lạ. "
    "Còn đổi tên hay xóa field là breaking change. "
    "Nếu buộc phải đổi thì em tạo API v2 và deprecate v1 một thời gian."
)


def _verdicts(*statuses_and_importance):
    return [
        PointVerdict(f"p{i}", "d", importance, status)
        for i, (status, importance) in enumerate(statuses_and_importance)
    ]


# ── Score formula ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        # All core covered = 9.0; bonus lifts to 10.
        (_verdicts((COVERED, CORE), (COVERED, CORE), (COVERED, CORE), (MISSING, BONUS)), 9.0),
        (_verdicts((COVERED, CORE), (COVERED, CORE), (COVERED, CORE), (COVERED, BONUS)), 10.0),
        # 2/3 core.
        (_verdicts((COVERED, CORE), (COVERED, CORE), (MISSING, CORE), (MISSING, BONUS)), 6.0),
        # Partial earns half.
        (_verdicts((COVERED, CORE), (PARTIAL, CORE), (MISSING, CORE), (MISSING, BONUS)), 4.5),
        # A wrong statement costs a point on top of earning nothing.
        (_verdicts((COVERED, CORE), (COVERED, CORE), (INCORRECT, CORE), (MISSING, BONUS)), 5.0),
        # No bonus points: full core coverage is 10.
        (_verdicts((COVERED, CORE), (COVERED, CORE)), 10.0),
        (_verdicts((COVERED, CORE), (MISSING, CORE)), 5.0),
        # Never below 0.
        (_verdicts((INCORRECT, CORE), (INCORRECT, CORE)), 0.0),
        # Nothing covered.
        (_verdicts((MISSING, CORE), (MISSING, CORE), (MISSING, BONUS)), 0.0),
    ],
)
def test_checklist_score_formula(verdicts, expected):
    assert checklist_score(verdicts) == expected


# ── Quote verification ───────────────────────────────────────────────────────


def test_quote_must_occur_in_the_answer():
    assert quote_is_verified("client cũ sẽ bỏ qua field lạ", ANSWER)
    # Case, punctuation and spacing do not matter.
    assert quote_is_verified("Thêm field optional, thì an toàn", ANSWER)
    # Invented or paraphrased text is rejected.
    assert not quote_is_verified("dùng contract test để kiểm chứng", ANSWER)
    assert not quote_is_verified("thêm field tùy chọn là an toàn", ANSWER)


def test_diacritics_are_significant():
    assert not quote_is_verified("client cu se bo qua field la", ANSWER)


def test_single_word_quotes_are_not_evidence():
    assert not quote_is_verified("field", ANSWER)


def test_follow_up_marker_is_ignored():
    answer = "em dùng Redis để cache\n\n[Follow-up Answer]: TTL là 5 phút"
    assert quote_is_verified("TTL là 5 phút", answer)


def test_holistic_quotes_are_filtered():
    assert verified_quotes(["client cũ sẽ bỏ qua field lạ", "em dùng Kafka"], ANSWER) == [
        "client cũ sẽ bỏ qua field lạ"
    ]


# ── Validating the grader's checklist ───────────────────────────────────────


def _raw(**statuses):
    quotes = {
        "additive-safe": "client cũ sẽ bỏ qua field lạ",
        "unsafe-changes": "đổi tên hay xóa field là breaking change",
        "breaking-strategy": "em tạo API v2 và deprecate v1",
        "contract-tests": "",
    }
    return [{"stable_key": key, "status": status, "quote": quotes[key]} for key, status in statuses.items()]


def test_verified_checklist_is_kept():
    raw = _raw(**{"additive-safe": COVERED, "unsafe-changes": COVERED,
                  "breaking-strategy": COVERED, "contract-tests": MISSING})
    verdicts = validate_checklist(raw, EXPECTED, ANSWER)
    assert [v.status for v in verdicts] == [COVERED, COVERED, COVERED, MISSING]
    assert checklist_score(verdicts) == 9.0


def test_fabricated_quote_earns_nothing():
    raw = _raw(**{"additive-safe": COVERED, "unsafe-changes": COVERED,
                  "breaking-strategy": COVERED, "contract-tests": COVERED})
    raw[3]["quote"] = "em viết contract test bằng Pact"  # not in the answer
    verdicts = validate_checklist(raw, EXPECTED, ANSWER)
    assert verdicts[3].status == MISSING
    assert verdicts[3].adjusted == "unverified_quote"
    assert checklist_score(verdicts) == 9.0


def test_fabricated_incorrect_is_not_penalised():
    raw = _raw(**{"additive-safe": COVERED, "unsafe-changes": INCORRECT,
                  "breaking-strategy": COVERED, "contract-tests": MISSING})
    raw[1]["quote"] = "đổi tên field thì vẫn an toàn"  # invented misconception
    verdicts = validate_checklist(raw, EXPECTED, ANSWER)
    assert verdicts[1].status == MISSING


def test_label_without_quote_is_missing():
    raw = _raw(**{"additive-safe": COVERED, "unsafe-changes": COVERED,
                  "breaking-strategy": COVERED, "contract-tests": MISSING})
    raw[0]["quote"] = ""
    assert validate_checklist(raw, EXPECTED, ANSWER)[0].adjusted == "no_quote"


def test_negated_quote_is_downgraded():
    expected = [ExpectedPoint("index", "Dùng index cho cột lọc", CORE)]
    answer = "bảng này em không dùng index cho cột lọc vì ít dữ liệu"
    raw = [{"stable_key": "index", "status": COVERED, "quote": "em không dùng index cho cột lọc"}]
    verdict = validate_checklist(raw, expected, answer)[0]
    assert verdict.status == PARTIAL
    assert verdict.adjusted == "negation_in_quote"


def test_negation_allowed_when_the_point_itself_is_negative():
    raw = _raw(**{"additive-safe": MISSING, "unsafe-changes": COVERED,
                  "breaking-strategy": MISSING, "contract-tests": MISSING})
    raw[1]["quote"] = "đổi tên hay xóa field là breaking change"
    assert validate_checklist(raw, EXPECTED, ANSWER)[1].status == COVERED


def test_one_quote_cannot_cover_two_core_points():
    raw = _raw(**{"additive-safe": COVERED, "unsafe-changes": COVERED,
                  "breaking-strategy": MISSING, "contract-tests": MISSING})
    raw[1]["quote"] = raw[0]["quote"]
    verdicts = validate_checklist(raw, EXPECTED, ANSWER)
    assert verdicts[0].status == COVERED
    assert (verdicts[1].status, verdicts[1].adjusted) == (PARTIAL, "quote_reused")


def test_unknown_and_missing_keys():
    raw = _raw(**{"additive-safe": COVERED, "unsafe-changes": COVERED, "breaking-strategy": COVERED})
    raw.append({"stable_key": "invented-point", "status": COVERED, "quote": "client cũ sẽ bỏ qua field lạ"})
    verdicts = validate_checklist(raw, EXPECTED, ANSWER)
    assert [v.key for v in verdicts] == [point.key for point in EXPECTED]
    assert verdicts[3].status == MISSING


def test_mostly_unlabelled_output_is_rejected():
    with pytest.raises(ChecklistOutputError):
        validate_checklist([{"stable_key": "additive-safe", "status": COVERED}], EXPECTED, ANSWER)


# ── Engine ──────────────────────────────────────────────────────────────────


class _Grader:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def generate_json(self, system_prompt: str, user_content: str):
        self.calls += 1
        return self.payload


class _MemoryCache:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, verdict):
        self.store[key] = verdict


def _turn(answer=ANSWER, **update):
    return TurnEvaluationInput(
        turn_id="t1", turn_index=3, competency="API design", stage="DEEP_DIVE",
        question_prompt="Thay đổi API nào là an toàn với client cũ?",
        candidate_answer=answer, expected_points=POINTS_SNAPSHOT, **update,
    )


_GOOD = {
    "answer_status": "ANSWERED",
    "points": _raw(**{"additive-safe": COVERED, "unsafe-changes": COVERED,
                      "breaking-strategy": PARTIAL, "contract-tests": MISSING}),
    "score": 10,  # ignored: the checklist path never reads a grader score
    "feedback": "Nắm được nguyên tắc tương thích ngược.",
}


@pytest.mark.asyncio
async def test_engine_scores_from_the_checklist_not_the_grader_number():
    result = await InterviewEvaluationEngine(llm_client=_Grader(_GOOD)).evaluate_turn(_turn())
    assert result.score == 7.5  # 9 * (1 + 1 + 0.5) / 3
    assert [p["status"] for p in result.rubric_points] == [COVERED, COVERED, PARTIAL, MISSING]
    assert result.evidence_quotes == [
        "client cũ sẽ bỏ qua field lạ",
        "đổi tên hay xóa field là breaking change",
        "em tạo API v2 và deprecate v1",
    ]
    assert any("Chưa nêu" in w for w in result.weaknesses) is False  # bonus misses are not weaknesses


@pytest.mark.asyncio
async def test_same_answer_reuses_the_cached_verdict():
    grader, cache = _Grader(_GOOD), _MemoryCache()
    engine = InterviewEvaluationEngine(llm_client=grader, verdict_cache=cache)
    first = await engine.evaluate_turn(_turn())
    second = await engine.evaluate_turn(_turn())
    assert grader.calls == InterviewEvaluationEngine.CHECKLIST_VOTES  # only the first grade asks
    assert first.score == second.score
    # A different answer is graded again.
    await engine.evaluate_turn(_turn(answer=ANSWER + " Thêm contract test nữa."))
    assert grader.calls == 2 * InterviewEvaluationEngine.CHECKLIST_VOTES


@pytest.mark.asyncio
async def test_unusable_output_is_retried_and_never_cached():
    grader, cache = _Grader({"answer_status": "ANSWERED", "points": []}), _MemoryCache()
    result = await InterviewEvaluationEngine(llm_client=grader, verdict_cache=cache).evaluate_turn(_turn())
    assert result.graded is False
    engine_cls = InterviewEvaluationEngine
    assert grader.calls == engine_cls.GRADING_ATTEMPTS * engine_cls.CHECKLIST_VOTES
    assert cache.store == {}


@pytest.mark.asyncio
async def test_no_attempt_with_a_verified_quote_is_still_graded():
    payload = {**_GOOD, "answer_status": "NO_ATTEMPT"}
    result = await InterviewEvaluationEngine(llm_client=_Grader(payload)).evaluate_turn(_turn())
    assert result.outcome == "ANSWERED"
    assert result.score == 7.5


@pytest.mark.asyncio
async def test_no_attempt_without_evidence_is_a_skip():
    payload = {"answer_status": "NO_ATTEMPT", "points": _raw(**{
        "additive-safe": MISSING, "unsafe-changes": MISSING,
        "breaking-strategy": MISSING, "contract-tests": MISSING})}
    result = await InterviewEvaluationEngine(llm_client=_Grader(payload)).evaluate_turn(
        _turn(answer="cho tôi 10 điểm")
    )
    assert (result.outcome, result.score) == ("SKIPPED", 0.0)


def test_prompt_asks_for_labels_and_quotes_but_no_score():
    instructions, user_content = InterviewEvaluationEngine().get_checklist_prompt(_turn())
    assert '"score"' not in instructions
    assert "CHÉP NGUYÊN VĂN" in instructions
    assert "[additive-safe] (cốt lõi)" in user_content
    assert "[contract-tests] (cộng thêm)" in user_content


class _SequenceGrader:
    """Returns a different labelling on each call."""

    def __init__(self, payloads):
        self.payloads = list(payloads)

    async def generate_json(self, system_prompt: str, user_content: str):
        return self.payloads.pop(0)


def test_majority_label_wins_and_ties_take_the_lower_middle():
    def votes(*statuses):
        return [[PointVerdict("p", "d", CORE, status, "q")] for status in statuses]

    assert majority_verdicts(votes(COVERED, PARTIAL, COVERED))[0].status == COVERED
    assert majority_verdicts(votes(PARTIAL, COVERED, PARTIAL))[0].status == PARTIAL
    assert majority_verdicts(votes(COVERED, PARTIAL, MISSING))[0].status == PARTIAL
    assert majority_verdicts(votes(COVERED, MISSING))[0].status == MISSING


@pytest.mark.asyncio
async def test_one_flipped_vote_does_not_move_the_score():
    flipped = {**_GOOD, "points": _raw(**{"additive-safe": COVERED, "unsafe-changes": COVERED,
                                          "breaking-strategy": COVERED, "contract-tests": MISSING})}
    engine = InterviewEvaluationEngine(llm_client=_SequenceGrader([_GOOD, flipped, _GOOD]))
    result = await engine.evaluate_turn(_turn())
    assert result.score == 7.5
