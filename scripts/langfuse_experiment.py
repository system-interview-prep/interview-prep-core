"""Bộ đề cố định và hồi quy cho AI phỏng vấn, chạy bằng Langfuse Experiments.

Tầng 4 của DOC_AND_PLAN/plan/interview/LLM_MONITORING_DESIGN_2026-10-25.md.

1. `seed`: tạo (hoặc bổ sung) dataset `interview-turns-golden` từ
   scripts/data/golden_turns.json. Thêm dần các lượt người soát đã chấm vào file đó.
2. `run`: cho engine thật (model thật, prompt hiện tại) trả lời từng mục, chấm
   bằng luật, và ghi thành một lần chạy trong Langfuse (Datasets → interview-turns-golden
   → Runs). Đặt tên lần chạy theo thay đổi (vd "probe-prompt-v3") để so trong Langfuse.

Chạy trong container backend (cần LANGFUSE_* và OPENAI_API_KEY):

    docker compose exec backend python scripts/langfuse_experiment.py seed
    docker compose exec backend python scripts/langfuse_experiment.py run --name "truoc-khi-sua-prompt"

Sổ tay người phỏng vấn (interviews/core/playbook.py): `--playbook active` chạy với các
bài học đang dùng, `--playbook candidate` thêm cả các bài học đang chờ duyệt. Chạy hai lần
rồi so trong Langfuse trước khi duyệt bài học mới:

    docker compose exec backend python scripts/langfuse_experiment.py run --name pb-active --playbook active
    docker compose exec backend python scripts/langfuse_experiment.py run \
        --name pb-candidate --playbook candidate

Mỗi lần chạy gọi model khoảng 1–2 lần cho mỗi mục (16 mục ban đầu).
Chạy `run --local` để thử không cần Langfuse (in kết quả ra màn hình).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import llm_tracing  # noqa: E402
from src.modules.interviews.application import llm_checks  # noqa: E402
from src.modules.interviews.core import speech_checks  # noqa: E402

DATASET = "interview-turns-golden"
DATA_FILE = Path(__file__).resolve().parent / "data" / "golden_turns.json"


def _items() -> list[dict[str, Any]]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))["items"]


def _state(item: dict[str, Any]) -> dict[str, Any]:
    stage = item.get("stage") or "DEEP_DIVE"
    return {
        "session_id": f"golden-{item['id']}",
        "locale": "vi-VN",
        "current_stage": stage,
        "started_at": datetime.now(UTC),
        "target_duration_minutes": 25,
        "asked_question_ids": ["q-golden"],
        "current_turn_in_question": 0,
        "current_question_context": {
            "question_id": "q-golden",
            "main_prompt": item["question"],
            "competency": "Golden",
            "rubric_criteria": item.get("rubric", ""),
        },
        "questions_pool": {
            "DEEP_DIVE": [
                {
                    "question_id": "q-next",
                    "competency": "Docker",
                    "stage": "DEEP_DIVE",
                    "main_prompt": "Trình bày sự khác nhau giữa image và container trong Docker.",
                    "spoken_prompt": "Theo bạn, image với container trong Docker khác nhau ở chỗ nào?",
                }
            ],
            "BEHAVIORAL": [
                {
                    "question_id": "q-beh",
                    "competency": "Teamwork",
                    "stage": "BEHAVIORAL",
                    "main_prompt": "Kể một lần bạn bất đồng trong team.",
                }
            ],
        },
    }


def observed_intent(meta: dict[str, Any]) -> str | None:
    """The intent the engine acted on, read the way the runtime records it (chat_runtime turn_spoken).

    Only the LLM path sets ``intent``. A request to change the question is
    handled before the LLM (``skipped_turn``), and asides such as off-topic
    chat or "write it for me" are answered by a fixed reply
    (``candidate_question_intent``); reading ``intent`` alone counted those
    correct decisions as misses (baseline 26/10: skip, off-topic, injection-role).
    """
    if meta.get("intent"):
        return str(meta["intent"])
    if meta.get("skipped_turn"):
        return "SKIP_QUESTION"
    if meta.get("candidate_question_intent"):
        return str(meta["candidate_question_intent"])
    if meta.get("candidate_abort_intent"):
        return "CANDIDATE_ABORT"
    if meta.get("clarify_used"):
        return "CLARIFY_REQUEST"
    return None


async def run_item(payload: dict[str, Any]) -> dict[str, Any]:
    """One golden turn through the real engine: what it decided and said, and the rule checks."""
    from src.modules.ai.facade import generate_text
    from src.modules.interviews.core.interview_engine import InterviewCoreEngine
    from src.modules.interviews.core.interview_types import CandidateTurnInput

    engine = InterviewCoreEngine(ai_generator=generate_text)
    speech_checks.begin()
    output = await engine.handle_turn(
        CandidateTurnInput(
            session_id=f"golden-{payload.get('id', 'x')}", turn_index=1, text_content=payload["answer"]
        ),
        _state(payload),
    )
    meta = output.metadata or {}
    action = output.action.value if hasattr(output.action, "value") else str(output.action)
    checks = speech_checks.collected()
    return {
        "action": action,
        "decision_rule": llm_checks.decision_rule(action, meta),
        "intent": observed_intent(meta),
        "sufficiency": meta.get("sufficiency_status"),
        "missing_aspect": meta.get("missing_aspect"),
        "reply": output.message_text,
        "speech_checks": checks,
        "flags": llm_checks.turn_flags(
            action=action,
            meta=meta,
            answer=payload["answer"],
            question=payload["question"],
            rubric=payload.get("rubric", ""),
            reply=output.message_text or "",
            checks=checks,
        ),
    }


def score(output: dict[str, Any], expected: dict[str, Any]) -> dict[str, bool]:
    """Rule evaluators: each expectation of the item that can be checked, true when met."""
    results: dict[str, bool] = {}
    if "intent" in expected:
        results["intent_match"] = output.get("intent") == expected["intent"]
    if "intent_in" in expected:
        results["intent_match"] = output.get("intent") in expected["intent_in"]
    if "sufficiency" in expected:
        results["sufficiency_match"] = output.get("sufficiency") == expected["sufficiency"]
    if "sufficiency_not" in expected:
        results["sufficiency_match"] = output.get("sufficiency") != expected["sufficiency_not"]
    if expected.get("should_probe") is not None:
        results["probe_decision"] = (output.get("action") == "PROBE") == bool(expected["should_probe"])
    if "action_in" in expected:
        results["action_match"] = output.get("action") in expected["action_in"]
    flags = set(output.get("flags") or [])
    results["in_scope"] = not ({"system_leak"} & flags)
    results["no_guard_reject"] = not any(
        c.get("outcome") == "reject" for c in output.get("speech_checks") or []
    )
    return results


def seed() -> None:
    lf = llm_tracing.client()
    if lf is None:
        sys.exit("Langfuse chưa bật (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL).")
    try:
        lf.get_dataset(DATASET)
    except Exception:  # noqa: BLE001 - not created yet
        lf.create_dataset(name=DATASET, description="Lượt phỏng vấn mẫu với kết quả mong đợi (tầng 4).")
    for item in _items():
        lf.create_dataset_item(
            dataset_name=DATASET,
            id=f"golden-{item['id']}",  # same id: running seed again updates instead of duplicating
            input={k: item[k] for k in ("id", "stage", "question", "rubric", "answer") if k in item},
            expected_output=item["expected"],
            metadata={"source": "golden_turns.json"},
        )
    lf.flush()
    print(f"Đã ghi {len(_items())} mục vào dataset {DATASET}.")


def load_playbook(mode: str) -> str:
    """Load the ``mode`` playbook entries (none / active / candidate) into the engine; return the version."""
    from src.modules.interviews.core import playbook

    if mode == "none":
        playbook.load([])
        return "none"
    from sqlalchemy import text

    from src.infrastructure.database import SessionFactory

    statuses = ["ACTIVE"] if mode == "active" else ["ACTIVE", "PROPOSED"]

    async def fetch() -> list[dict[str, Any]]:
        async with SessionFactory() as db:
            rows = await db.execute(
                text(
                    "SELECT id, step, skill, text, status, replaces_id FROM interviewer_playbook "
                    "WHERE status = ANY(:s)"
                ),
                {"s": statuses},
            )
            return [dict(r) for r in rows.mappings().all()]

    entries = asyncio.run(fetch())
    # A proposal that replaces or retires an active lesson takes its place.
    replaced = {e["replaces_id"] for e in entries if e["status"] == "PROPOSED" and e.get("replaces_id")}
    playbook.load(e for e in entries if e["id"] not in replaced)
    return playbook.version() or "empty"


def run(name: str, local: bool, playbook_mode: str = "active") -> None:
    version = load_playbook(playbook_mode)
    print(f"Playbook: {playbook_mode} ({version})")
    if local:

        async def main() -> None:
            totals: dict[str, list[bool]] = {}
            for item in _items():
                output = await run_item({**item})
                result = score(output, item["expected"])
                for key, ok in result.items():
                    totals.setdefault(key, []).append(ok)
                bad = [k for k, ok in result.items() if not ok]
                print(
                    f"{'✗' if bad else '✓'} {item['id']:<26} {output['action']:<14} {output['intent']} "
                    f"{output['sufficiency']} {', '.join(bad)}"
                )
            print()
            for key, values in totals.items():
                print(f"{key:<18} {sum(values)}/{len(values)}")

        asyncio.run(main())
        return

    from langfuse import Evaluation

    lf = llm_tracing.client()
    if lf is None:
        sys.exit("Langfuse chưa bật; dùng --local để chạy không cần Langfuse.")
    dataset = lf.get_dataset(DATASET)

    async def task(*, item: Any, **_: Any) -> dict[str, Any]:
        payload = item.input if hasattr(item, "input") else item["input"]
        return await run_item(dict(payload))

    def evaluator(*, output: Any, expected_output: Any, **_: Any) -> list[Evaluation]:
        return [
            Evaluation(name=key, value=ok, data_type="BOOLEAN")
            for key, ok in score(output or {}, expected_output or {}).items()
        ]

    result = lf.run_experiment(
        name="interview-turns",
        run_name=name,
        description="Bộ đề cố định qua engine thật",
        data=dataset.items,
        task=task,
        evaluators=[evaluator],
        max_concurrency=4,
        metadata={
            "model": __import__("src.core.config", fromlist=["x"]).get_settings().llm_model,
            "playbook": f"{playbook_mode}:{version}",
        },
    )
    lf.flush()
    print(result.format() if hasattr(result, "format") else result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--name", default=datetime.now().strftime("run-%Y%m%d-%H%M"))
    run_parser.add_argument("--local", action="store_true", help="không ghi lên Langfuse, in kết quả")
    run_parser.add_argument(
        "--playbook",
        choices=("none", "active", "candidate"),
        default="active",
        help="bài học của sổ tay người phỏng vấn đưa vào prompt (candidate = cả bài đang chờ duyệt)",
    )
    args = parser.parse_args()
    if args.command == "seed":
        seed()
    else:
        run(args.name, args.local, args.playbook)


if __name__ == "__main__":
    main()
