"""Generate a balanced 300-utterance Vietnamese interview collection plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROMPTS = [
    "Bạn hãy giới thiệu ngắn gọn về bản thân và định hướng nghề nghiệp hiện tại.",
    "Hãy kể về dự án gần nhất mà bạn trực tiếp tham gia.",
    "Trong dự án đó, trách nhiệm chính của bạn là gì?",
    "Bạn đã gặp lỗi khó nhất nào và đã tìm nguyên nhân ra sao?",
    "Hãy mô tả cách bạn kiểm tra log khi hệ thống có sự cố.",
    "Khi yêu cầu chưa rõ, bạn thường trao đổi với người liên quan như thế nào?",
    "Bạn ưu tiên công việc ra sao khi có nhiều việc gấp cùng lúc?",
    "Hãy kể một lần bạn nhận phản hồi chưa tốt và cách bạn xử lý.",
    "Bạn đã từng bất đồng kỹ thuật với đồng đội chưa? Kết quả thế nào?",
    "Hãy giải thích một khái niệm kỹ thuật quen thuộc cho người không chuyên.",
    "Bạn đã dùng Git trong quy trình làm việc nhóm như thế nào?",
    "Bạn thường viết và chạy unit test ở giai đoạn nào?",
    "Hãy mô tả cách bạn review một pull request.",
    "Bạn xử lý một API phản hồi chậm theo các bước nào?",
    "Khi database có truy vấn chậm, bạn sẽ kiểm tra những gì trước?",
    "Bạn đã từng làm việc với Docker hoặc Kubernetes như thế nào?",
    "Hãy kể về một quyết định kỹ thuật bạn phải đánh đổi giữa tốc độ và chất lượng.",
    "Bạn bảo vệ dữ liệu nhạy cảm trong ứng dụng như thế nào?",
    "Nếu deployment mới gây lỗi production, bạn sẽ làm gì?",
    "Bạn theo dõi hiệu năng và độ ổn định của hệ thống bằng cách nào?",
    "Bạn đã từng tối ưu một đoạn code hoặc quy trình nào chưa?",
    "Hãy kể về một lần bạn phải học công nghệ mới trong thời gian ngắn.",
    "Bạn làm gì để bảo đảm tài liệu kỹ thuật luôn hữu ích cho đội nhóm?",
    "Bạn ước lượng thời gian hoàn thành một task chưa quen như thế nào?",
    "Hãy mô tả cách bạn chia một yêu cầu lớn thành các phần nhỏ.",
    "Bạn đã từng hỗ trợ đồng đội giải quyết blocker như thế nào?",
    "Khi test tự động bị flaky, bạn sẽ điều tra theo hướng nào?",
    "Bạn đánh giá chất lượng của một tính năng trước khi release như thế nào?",
    "Hãy kể một thành quả kỹ thuật mà bạn cảm thấy tự hào nhất.",
    "Nếu được làm lại một dự án cũ, bạn sẽ thay đổi điều gì và vì sao?",
]

DIALECTS = ("north", "central", "south")
ENVIRONMENTS = ("quiet", "office-noise")
DEVICES = ("laptop", "phone", "headset")


def generate_plan() -> list[dict]:
    rows: list[dict] = []
    for speaker_index in range(30):
        dialect = DIALECTS[speaker_index // 10]
        slot_id = f"{dialect}-speaker-{speaker_index % 10 + 1:02d}"
        for utterance_index in range(10):
            prompt_index = (speaker_index * 7 + utterance_index * 3) % len(PROMPTS)
            rows.append(
                {
                    "id": f"vi-test-{dialect}-{speaker_index % 10 + 1:02d}-{utterance_index + 1:02d}",
                    "slotId": slot_id,
                    "split": "test",
                    "dialect": dialect,
                    "environment": ENVIRONMENTS[(speaker_index + utterance_index) % 2],
                    "device": DEVICES[speaker_index % len(DEVICES)],
                    "prompt": PROMPTS[prompt_index],
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in generate_plan()),
        encoding="utf-8",
    )
    print(f"wrote {len(generate_plan())} samples to {args.output}")


if __name__ == "__main__":
    main()
