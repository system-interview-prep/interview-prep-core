"""Chuẩn bị project Langfuse cho việc theo dõi AI phỏng vấn (chạy một lần).

Thiết kế: DOC_AND_PLAN/plan/interview/LLM_MONITORING_DESIGN_2026-10-25.md.
Hướng dẫn: DOC_AND_PLAN/plan/interview/LANGFUSE_SETUP_2026-10-25.md.

Tạo:
1. Score config cho người soát (tầng 3): đúng/sai cho từng tiêu chí, cùng tên với
   các giám khảo AI để Langfuse so được giám khảo với người.
2. Hàng đợi soát "Soát AI phỏng vấn". In ra id để đặt LANGFUSE_REVIEW_QUEUE_ID:
   backend tự đưa lượt bị cờ và 5% lượt có văn bản vào hàng đợi.
3. (--judges) Bốn giám khảo AI (tầng 2) và một luật chạy chúng trên các lượt có
   tag "text". Cần đã thêm LLM connection (khoá OpenAI) trong Langfuse: Settings →
   LLM Connections. Nếu API từ chối, tạo tay theo `python scripts/langfuse_judges.py`.

Chạy trong container backend (đã có LANGFUSE_* trong .env):

    docker compose exec backend python scripts/langfuse_setup.py
    docker compose exec backend python scripts/langfuse_setup.py --judges --judge-model gpt-4.1

Chạy lại không tạo trùng: thứ đã có (theo tên) được giữ nguyên.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langfuse_judges import JUDGES, judge_prompt  # noqa: E402

from src.core import llm_tracing  # noqa: E402

QUEUE_NAME = "Soát AI phỏng vấn"
RULE_NAME = "Giám khảo AI: lượt có văn bản"
# Human checks: the four judge criteria, plus the guard verdict sent from the admin page.
HUMAN_CONFIGS = [(judge["name"].replace("judge.", "human."), judge["description"]) for judge in JUDGES] + [
    ("human.guard_correct", "Rào chắn chặn hoặc sửa câu AI viết là đúng (trang Lời nói AI).")
]


def _existing_configs(lf) -> dict[str, str]:
    found: dict[str, str] = {}
    page = 1
    while True:
        result = lf.api.score_configs.get(page=page, limit=100)
        for config in result.data:
            found[config.name] = config.id
        if page >= (result.meta.total_pages or 1):
            return found
        page += 1


def ensure_score_configs(lf) -> list[str]:
    existing = _existing_configs(lf)
    ids = []
    for name, description in HUMAN_CONFIGS:
        if name not in existing:
            config = lf.api.score_configs.create(name=name, data_type="BOOLEAN", description=description)
            existing[name] = config.id
            print(f"  + score config {name}")
        else:
            print(f"  = score config {name}")
        ids.append(existing[name])
    return ids


def ensure_queue(lf, config_ids: list[str]) -> str:
    queues = lf.api.annotation_queues.list_queues(limit=100)
    for queue in queues.data:
        if queue.name == QUEUE_NAME:
            print(f"  = hàng đợi {QUEUE_NAME}")
            return queue.id
    queue = lf.api.annotation_queues.create_queue(
        name=QUEUE_NAME,
        score_config_ids=config_ids,
        description="Lượt bị cờ và lượt ngẫu nhiên: người soát chấm đúng/sai theo bảng kiểm.",
    )
    print(f"  + hàng đợi {QUEUE_NAME}")
    return queue.id


def ensure_judges(lf, provider: str, model: str, sampling: float) -> None:
    from langfuse.api.evaluation_commons.types.evaluator_output_definition import (
        EvaluatorOutputDefinition_Boolean,
    )
    from langfuse.api.evaluation_commons.types.prompt_variable_mapping_input import PromptVariableMappingInput
    from langfuse.api.evaluation_rules.types.evaluation_rule_evaluator_assignment_input import (
        EvaluationRuleEvaluatorAssignmentInput,
    )
    from langfuse.api.evaluators.types.create_evaluator_request import CreateEvaluatorRequest_LlmAsJudge
    from langfuse.api.evaluators.types.update_llm_as_judge_evaluator_request import (
        UpdateLlmAsJudgeEvaluatorRequest,
    )

    # An evaluator created before its LLM connection is paused at once and stays
    # paused after the connection is added; Langfuse resumes it only when it is saved again.
    if not any(c.provider == provider for c in lf.api.llm_connections.list().data):
        print(
            f"  ! chưa có LLM connection {provider}: thêm ở Langfuse → Settings → LLM Connections "
            "rồi chạy lại lệnh này (giám khảo tạo khi thiếu connection sẽ bị tạm dừng)."
        )
        return
    evaluators = {evaluator.name: evaluator for evaluator in lf.api.evaluators.list().data}
    mapping = [
        PromptVariableMappingInput(variable="input", source="input"),
        PromptVariableMappingInput(variable="output", source="output"),
    ]
    ids = []
    for judge in JUDGES:
        current = evaluators.get(judge["name"])
        if current is not None:
            ids.append(current.id)
            if getattr(current.status, "value", current.status) != "paused":
                print(f"  = giám khảo {judge['name']}")
                continue
            lf.api.evaluators.update(
                current.id,
                request=UpdateLlmAsJudgeEvaluatorRequest(
                    type="llm_as_judge",
                    prompt=current.prompt,
                    model_config_=current.model_config_,
                    variable_mapping=[
                        PromptVariableMappingInput(variable=v.variable, source=v.source)
                        for v in current.variable_mapping
                    ],
                    output_definition=EvaluatorOutputDefinition_Boolean(
                        score_reasoning_instructions=current.output_definition.score_reasoning_instructions,
                        score_value_instructions=current.output_definition.score_value_instructions,
                    ),
                ),
            )
            print(f"  ~ giám khảo {judge['name']} đang tạm dừng ({current.paused_reason}): đã bật lại")
            continue
        request = CreateEvaluatorRequest_LlmAsJudge(
            name=judge["name"],
            description=judge["description"],
            prompt=judge_prompt(judge),
            model_config_={"provider": provider, "model": model},
            variable_mapping=mapping,
            output_definition=EvaluatorOutputDefinition_Boolean(
                score_reasoning_instructions="Một câu lý do bằng tiếng Việt.",
                score_value_instructions="true nếu đạt, false nếu không đạt.",
            ),
        )
        evaluator = lf.api.evaluators.create(request=request)
        ids.append(evaluator.id)
        print(f"  + giám khảo {judge['name']}")

    rules = {rule.name for rule in lf.api.evaluation_rules.list().data}
    if RULE_NAME in rules:
        print(f"  = luật {RULE_NAME}")
        return
    assignments = [
        EvaluationRuleEvaluatorAssignmentInput(evaluator_id=i, variable_mapping=mapping) for i in ids
    ]
    # Rules accept no observation "name" column; the turn is the root span of a trace with the same name.
    name_filter = [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": [llm_tracing.TURN_TRACE],
        },
        {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
    ]
    tag_filter = {"type": "arrayOptions", "column": "tags", "operator": "any of", "value": ["text"]}
    try:
        lf.api.evaluation_rules.create(
            name=RULE_NAME,
            enabled=True,
            evaluator_assignments=assignments,
            sampling=sampling,
            filter=[*name_filter, tag_filter],
        )
        print(f"  + luật {RULE_NAME} (observation interview_turn, tag text, lấy mẫu {sampling:.0%})")
    except Exception as exc:  # noqa: BLE001 - the filter columns differ between Langfuse versions
        print(f"  ! luật có lọc tag bị từ chối ({exc}); tạo luật chỉ lọc theo tên observation")
        lf.api.evaluation_rules.create(
            name=RULE_NAME,
            enabled=True,
            evaluator_assignments=assignments,
            sampling=sampling,
            filter=name_filter,
        )
        print(
            "    Vào Langfuse → Evaluators → luật này, thêm điều kiện tags có 'text' để giám khảo chỉ chạy "
            "trên lượt có câu trả lời."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--judges", action="store_true", help="tạo giám khảo AI và luật chạy chúng")
    parser.add_argument("--judge-provider", default="openai")
    parser.add_argument("--judge-model", default="gpt-4.1", help="nên khác model đang phỏng vấn")
    parser.add_argument(
        "--sampling",
        type=float,
        default=1.0,
        help="tỉ lệ lượt có tag text được chấm (lượt có text đã là mẫu)",
    )
    args = parser.parse_args()

    lf = llm_tracing.client()
    if lf is None:
        sys.exit(
            "Langfuse chưa bật: đặt LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL trong .env "
            "và cài gói langfuse (rebuild image backend)."
        )
    if not lf.auth_check():
        sys.exit("Khoá Langfuse không hợp lệ hoặc không kết nối được LANGFUSE_BASE_URL.")
    print("Score config cho người soát:")
    config_ids = ensure_score_configs(lf)
    print("Hàng đợi soát:")
    queue_id = ensure_queue(lf, config_ids)
    if args.judges:
        print("Giám khảo AI:")
        ensure_judges(lf, args.judge_provider, args.judge_model, args.sampling)
    print()
    print(f"Đặt trong .env: LANGFUSE_REVIEW_QUEUE_ID={queue_id}")
    print(f"Project: {llm_tracing.project_url()}")


if __name__ == "__main__":
    main()
