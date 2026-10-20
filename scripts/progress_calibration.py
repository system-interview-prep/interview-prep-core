"""Đo độ nhiễu của điểm để trang Tiến độ biết khi nào một thay đổi là thật.

Nghiên cứu 20/10 (DOC_AND_PLAN/plan/interview/PROGRESS_EVIDENCE_RESEARCH_2026-10-20.md):
điểm một kỹ năng trong một buổi dao động vì câu hỏi khác nhau, vì AI chấm và vì
phong độ từng ngày. Ngưỡng 0,5 điểm cũ nằm trong vùng dao động đó, nên trang hiện
"đang tốt lên"/"đang kém đi" giả. Script này đo dao động thật từ dữ liệu đã lưu và
ghi một dòng vào bảng ``progress_calibration``; trang Tiến độ đọc dòng mới nhất.

Chạy trong thư mục interview-prep-core:

    # Đo tất cả: chấm lại 200 câu trả lời đã lưu, mỗi câu 3 lần (gọi AI), rồi ghi kết quả
    docker compose exec backend python scripts/progress_calibration.py measure

    # Chỉ đo từ dữ liệu đã có, không gọi AI
    docker compose exec backend python scripts/progress_calibration.py measure --regrade 0

    # Chưa ghi gì, chỉ in kết quả
    docker compose exec backend python scripts/progress_calibration.py measure --dry-run

    # Bộ câu neo: chụp 30 câu trả lời đã lưu làm mốc, rồi chấm lại mỗi khi đổi model hoặc prompt
    docker compose exec backend python scripts/progress_calibration.py anchors-snapshot
    docker compose exec backend python scripts/progress_calibration.py anchors-check

Đo gì:
- Độ lệch khi chấm lại cùng một câu trả lời (bỏ qua bộ nhớ đệm kết quả chấm).
- Sai số giữa hai buổi gần nhau (cách nhau tối đa 14 ngày) của cùng người, cùng kỹ
  năng, cùng mức luyện và cùng phiên bản cách chấm.
- Độ chênh giữa các người trên cùng kỹ năng, để tính "thay đổi nhỏ nhất có ý nghĩa".
- Chênh điểm giữa các mức luyện (Cơ bản, Thử thách so với Đúng mức).

Kết quả cũng ghi ra trace-logs/progress_calibration_<giờ>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # chạy được cả khi không đặt PYTHONPATH

from sqlalchemy import text  # noqa: E402

from src.infrastructure.database import StandaloneSessionFactory  # noqa: E402
from src.modules.interviews.application.progress_calibration import (  # noqa: E402
    Calibration,
    SkillSessionPoint,
    between_person_sd,
    grading_sd,
    save_calibration,
    tier_offsets,
    typical_error,
)
from src.modules.interviews.evaluation.evaluation_engine import (  # noqa: E402
    InterviewEvaluationEngine,
    grader_version,
)
from src.modules.interviews.evaluation.evaluation_types import TurnEvaluationInput  # noqa: E402

ANCHORS_FILE = Path("trace-logs/grading_anchors.json")
# Mean anchor shift (points) above which a model or prompt change needs attention.
ANCHOR_SHIFT_ALERT = 0.5


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def load_skill_points(db) -> list[SkillSessionPoint]:
    """Each person's mean score per skill per graded session."""
    rows = (
        await db.execute(
            text(
                """
                SELECT s.user_id, s.started_at, p.source_context->>'practiceTier' AS tier,
                       ev.grader_version, t.question_snapshot, te.score
                FROM interview_evaluations ev
                JOIN interview_sessions s ON s.id = ev.session_id
                JOIN interview_session_plans p ON p.session_id = s.id
                JOIN interview_turn_evaluations te ON te.evaluation_id = ev.id
                JOIN interview_turns t ON t.id = te.turn_id
                WHERE te.outcome = 'ANSWERED' AND te.score IS NOT NULL
                """
            )
        )
    ).mappings().all()
    grouped: dict[tuple, list[float]] = {}
    for row in rows:
        snap = _json(row["question_snapshot"]) or {}
        if str(snap.get("stage") or "") in {"WARM_UP", "CLOSING"}:
            continue
        concept = (snap.get("taxonomyTarget") or {}).get("conceptId")
        if not concept:
            continue
        tier = row["tier"] if row["tier"] in {"basic", "standard", "challenge"} else "standard"
        key = (str(row["user_id"]), str(concept), row["started_at"], tier, row["grader_version"])
        grouped.setdefault(key, []).append(float(row["score"]))
    return [
        SkillSessionPoint(
            user_id=key[0], concept=key[1], started_at=key[2], tier=key[3], grader_version=key[4],
            score=statistics.mean(scores),
        )
        for key, scores in grouped.items()
    ]


async def sample_turn_inputs(db, limit: int, seed: int) -> list[TurnEvaluationInput]:
    """Stored answers graded by checklist, rebuilt as grader inputs.

    Close to evaluate_closed_session but simpler: the stored answer text is used
    as is. Enough to measure how much the grader varies on the same input.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT t.id, t.turn_index, t.question_snapshot, t.answer_text
                FROM interview_turn_evaluations te
                JOIN interview_turns t ON t.id = te.turn_id
                WHERE te.outcome = 'ANSWERED' AND t.answer_text IS NOT NULL AND length(t.answer_text) > 20
                  AND jsonb_array_length(COALESCE(te.rubric_points, '[]'::jsonb)) > 0
                ORDER BY te.created_at DESC
                LIMIT 5000
                """
            )
        )
    ).mappings().all()
    rows = list(rows)
    random.Random(seed).shuffle(rows)
    inputs = []
    for row in rows[:limit]:
        snap = _json(row["question_snapshot"]) or {}
        inputs.append(
            TurnEvaluationInput(
                turn_id=str(row["id"]),
                turn_index=int(row["turn_index"] or 0),
                competency=(snap.get("taxonomyTarget") or {}).get("label") or "Chuyên môn",
                question_prompt=snap.get("questionText") or snap.get("question_text") or "",
                candidate_answer=str(row["answer_text"]),
                stage=str(snap.get("stage") or ""),
                expected_points=snap.get("expectedPoints") or [],
            )
        )
    return inputs


async def regrade(inputs: list[TurnEvaluationInput], repeats: int, concurrency: int = 4) -> list[list[float]]:
    """Grade each input ``repeats`` times without the verdict cache."""
    engine = InterviewEvaluationEngine(verdict_cache=None)
    gate = asyncio.Semaphore(concurrency)

    async def one(turn_input: TurnEvaluationInput) -> list[float]:
        scores = []
        for _ in range(repeats):
            async with gate:
                try:
                    result = await engine.evaluate_turn(turn_input)
                except Exception as exc:  # noqa: BLE001 - one failed call is reported, not fatal
                    print(f"  bỏ qua {turn_input.turn_id}: {exc}", file=sys.stderr)
                    continue
            if result.graded:
                scores.append(float(result.score))
        return scores

    return list(await asyncio.gather(*(one(item) for item in inputs)))


def _write_log(name: str, payload: dict[str, Any]) -> Path:
    out = Path("trace-logs")
    out.mkdir(exist_ok=True)
    path = out / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


async def measure(args: argparse.Namespace) -> None:
    async with StandaloneSessionFactory() as db:
        points = await load_skill_points(db)
        te, n_pairs = typical_error(points)
        bsd, n_people = between_person_sd(points)
        offsets = tier_offsets(points)
        repeats_scores: list[list[float]] = []
        if args.regrade > 0:
            inputs = await sample_turn_inputs(db, args.regrade, args.seed)
            print(f"Chấm lại {len(inputs)} câu trả lời, mỗi câu {args.repeats} lần (gọi AI)...")
            repeats_scores = await regrade(inputs, args.repeats)
        gsd, n_regraded = grading_sd(repeats_scores)

        calibration = Calibration(grader_version=grader_version())
        if te is not None:
            calibration.typical_error = te
        if bsd is not None:
            calibration.between_sd = bsd
        calibration.grading_sd = gsd
        calibration.tier_offsets = offsets
        # The grader's own spread is a floor for the session-to-session noise.
        if te is None and gsd is not None:
            calibration.typical_error = max(gsd, calibration.typical_error)
        calibration.sample = {
            "skillSessionPoints": len(points),
            "pairs": n_pairs,
            "peopleSkillMeans": n_people,
            "regradedAnswers": n_regraded,
            "repeats": args.repeats if args.regrade else 0,
            "typicalErrorMeasured": te is not None,
            "betweenSdMeasured": bsd is not None,
        }
        report = {
            "graderVersion": calibration.grader_version,
            "typicalError": calibration.typical_error,
            "betweenSd": calibration.between_sd,
            "swc": calibration.swc,
            "gradingSd": calibration.grading_sd,
            "tierOffsets": calibration.tier_offsets,
            "sample": calibration.sample,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if te is None:
            print(
                f"Chưa đủ cặp buổi gần nhau ({n_pairs} < 10): giữ sai số mặc định "
                "hoặc lấy độ lệch khi chấm lại làm cận dưới."
            )
        print(f"Đã ghi {_write_log('progress_calibration', report)}")
        if args.dry_run:
            print("--dry-run: không ghi vào progress_calibration.")
            return
        await save_calibration(db, calibration, notes=args.notes or "")
        print("Đã ghi một dòng vào progress_calibration; trang Tiến độ dùng dòng mới nhất.")


async def anchors_snapshot(args: argparse.Namespace) -> None:
    async with StandaloneSessionFactory() as db:
        inputs = await sample_turn_inputs(db, args.count, args.seed)
    scores = await regrade(inputs, 1)
    anchors = [
        {"input": item.model_dump(), "score": s[0]}
        for item, s in zip(inputs, scores, strict=True)
        if s
    ]
    ANCHORS_FILE.parent.mkdir(exist_ok=True)
    ANCHORS_FILE.write_text(
        json.dumps(
            {
                "graderVersion": grader_version(),
                "createdAt": time.strftime("%Y-%m-%d %H:%M"),
                "anchors": anchors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Đã lưu {len(anchors)} câu neo vào {ANCHORS_FILE} (cách chấm {grader_version()}).")


async def anchors_check(args: argparse.Namespace) -> int:
    if not ANCHORS_FILE.exists():
        print(f"Chưa có {ANCHORS_FILE}: chạy anchors-snapshot trước.")
        return 2
    data = json.loads(ANCHORS_FILE.read_text(encoding="utf-8"))
    anchors = data.get("anchors") or []
    inputs = [TurnEvaluationInput(**item["input"]) for item in anchors]
    scores = await regrade(inputs, 1)
    shifts = [s[0] - item["score"] for item, s in zip(anchors, scores, strict=True) if s]
    if not shifts:
        print("Không chấm lại được câu neo nào.")
        return 2
    mean_shift = statistics.mean(shifts)
    report = {
        "baselineVersion": data.get("graderVersion"),
        "currentVersion": grader_version(),
        "anchors": len(shifts),
        "meanShift": round(mean_shift, 2),
        "meanAbsShift": round(statistics.mean(abs(x) for x in shifts), 2),
        "largest": sorted((round(x, 1) for x in shifts), key=abs, reverse=True)[:5],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Đã ghi {_write_log('grading_anchor_check', report)}")
    if abs(mean_shift) > ANCHOR_SHIFT_ALERT:
        print(
            f"CẢNH BÁO: điểm câu neo lệch trung bình {mean_shift:+.2f} điểm. "
            "Cách chấm mới sẽ làm mọi ứng viên "
            "trông như tiến bộ hoặc thụt lùi. Trang Tiến độ chỉ so trong cùng phiên bản cách chấm, "
            "nên hãy đo lại bằng lệnh measure sau khi đổi."
        )
        return 1
    print("Điểm câu neo ổn định.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure", help="Đo độ nhiễu và ghi vào progress_calibration")
    m.add_argument("--regrade", type=int, default=200, help="Số câu trả lời chấm lại (0 = không gọi AI)")
    m.add_argument("--repeats", type=int, default=3, help="Số lần chấm lại mỗi câu")
    m.add_argument("--seed", type=int, default=20261020)
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--notes", default="")
    s = sub.add_parser("anchors-snapshot", help="Chụp bộ câu neo")
    s.add_argument("--count", type=int, default=30)
    s.add_argument("--seed", type=int, default=7)
    sub.add_parser("anchors-check", help="Chấm lại bộ câu neo và báo độ lệch")
    args = parser.parse_args()
    if args.command == "measure":
        asyncio.run(measure(args))
    elif args.command == "anchors-snapshot":
        asyncio.run(anchors_snapshot(args))
    else:
        raise SystemExit(asyncio.run(anchors_check(args)))


if __name__ == "__main__":
    main()
