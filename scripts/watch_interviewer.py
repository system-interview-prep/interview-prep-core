"""Theo dõi trực tiếp lời nói của người phỏng vấn AI trong lúc phỏng vấn thật.

Mỗi lượt, backend ghi một dòng vào trace-logs/naturalness/trace.jsonl (chỉ khi
bật trace log, mặc định bật ở môi trường development). Script này đọc file đó
liên tục và in từng lượt ra terminal: ứng viên nói gì, AI đáp gì, câu ghi nhận
do AI viết hay bị rào chắn thay (và vì sao), độ trễ, có lặp câu vừa nói không.

Chạy trong một cửa sổ terminal riêng, trong thư mục interview-prep-core:

    docker compose exec backend python scripts/watch_interviewer.py

Tuỳ chọn:
    --session <mã buổi>   chỉ hiện một buổi (gõ vài ký tự đầu là đủ)
    --all                 in lại cả các lượt đã có trước khi mở script
    --file <đường dẫn>    file trace khác (mặc định trace-logs/naturalness/trace.jsonl)

Bấm Ctrl+C để dừng; script in tổng kết các lượt đã thấy.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REASONS_VI = {
    "empty": "AI không viết câu ghi nhận",
    "too_long": "quá dài",
    "is_question": "là câu hỏi",
    "rubric_leak": "nhắc điểm hoặc tiêu chí",
    "evaluative": "khen chê, nói đúng sai",
    "too_many_words": "quá 10 từ",
    "too_curt": "quá cụt (dưới 4 từ)",
    "grader_style": "giọng tóm tắt bài (Bạn mô tả/phân biệt…)",
    "parroting": "chép lại câu trả lời",
    "keyword_fragment": "chỉ đọc lại từ khoá",
    "incomplete": "câu chưa trọn",
    "judgmental": "tự kết luận chất lượng (an toàn, rõ rệt…)",
    "repeats_recent": "trùng câu vừa nói",
    "wrong_language": "sai ngôn ngữ",
    "llm_error": "lỗi gọi AI",
    "injection": "lời chèn lệnh",
    "not_an_answer": "không phải câu trả lời",
    "gave_up": "ứng viên không biết",
    "skip_request": "ứng viên xin qua câu",
}
SOURCES_VI = {
    "llm": "AI viết ✔",
    "fallback": "dự phòng",
    "non_answer": "câu cho lượt không phải câu trả lời",
    "give_up": "câu cho 'không biết'",
    "skip": "câu cho 'xin qua câu'",
}

USE_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def _short(text: str | None, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


class Stats:
    def __init__(self) -> None:
        self.turns = 0
        self.sources: dict[str, int] = {}
        self.repeats = 0
        self.latencies: list[int] = []

    def add(self, record: dict) -> None:
        self.turns += 1
        source = record.get("ack_source") or "-"
        self.sources[source] = self.sources.get(source, 0) + 1
        self.repeats += bool(record.get("repeats_recent"))
        if isinstance(record.get("core_ms"), (int, float)):
            self.latencies.append(int(record["core_ms"]))

    def line(self) -> str:
        if not self.turns:
            return "Chưa có lượt nào."
        with_ack = sum(v for k, v in self.sources.items() if k != "-") or 1
        llm = self.sources.get("llm", 0)
        lat = ""
        if self.latencies:
            ordered = sorted(self.latencies)
            p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
            lat = f" | độ trễ trung vị {statistics.median(ordered) / 1000:.1f}s, p95 {p95 / 1000:.1f}s"
        return (
            f"{self.turns} lượt | câu ghi nhận AI viết {llm}/{with_ack} ({100 * llm / with_ack:.0f}%)"
            f" | lặp câu vừa nói {self.repeats}{lat}"
        )


def show(record: dict, stats: Stats) -> None:
    stats.add(record)
    head = (
        f"[lượt {record.get('turn_index')} · {record.get('stage')} · {record.get('competency') or '-'}"
        f" · {record.get('modality')} · {record.get('action')}]"
    )
    print(_c(head, "1;36"))
    print(f"  ứng viên    : {_short(record.get('candidate'), 160)}")
    ack = record.get("acknowledgement")
    source = record.get("ack_source")
    if ack:
        label = SOURCES_VI.get(source or "", source or "")
        reason = record.get("ack_reason")
        extra = ""
        if source == "fallback" and reason:
            raw = _short(record.get("ack_raw"), 90)
            extra = _c(f"  – AI viết \"{raw}\" bị loại: {REASONS_VI.get(reason, reason)}", "33")
        mark = _c(label, "32" if source == "llm" else "33")
        print(f"  AI ghi nhận : \"{ack}\"   ({mark}){extra}")
    message = str(record.get("interviewer") or "")
    question = message.split("\n\n")[-1] if "\n\n" in message else message
    print(f"  AI nói tiếp : {_short(question, 200)}")
    latency = record.get("core_ms")
    repeat = _c("CÓ", "31") if record.get("repeats_recent") else "không"
    lat_text = f"{latency / 1000:.1f} s" if isinstance(latency, (int, float)) else "-"
    print(f"  độ trễ Core : {lat_text}  |  lặp câu vừa nói: {repeat}")
    print(_c("  " + stats.line(), "2"))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", default="", help="chỉ hiện buổi có mã bắt đầu bằng chuỗi này")
    parser.add_argument("--all", action="store_true", help="in cả các lượt đã có")
    parser.add_argument("--file", default="trace-logs/naturalness/trace.jsonl")
    args = parser.parse_args()

    path = Path(args.file)
    print(_c(f"Đang theo dõi {path} … (Ctrl+C để dừng)", "1"))
    if not path.exists():
        print("Chưa có file; nó sẽ xuất hiện ở lượt trả lời đầu tiên của một buổi phỏng vấn.")
    stats = Stats()
    position = 0 if args.all else (path.stat().st_size if path.exists() else 0)
    try:
        while True:
            if path.exists():
                size = path.stat().st_size
                if size < position:  # file was rotated or cleared
                    position = 0
                if size > position:
                    with path.open("r", encoding="utf-8") as handle:
                        handle.seek(position)
                        for line in handle:
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            if record.get("event") != "turn_spoken":
                                continue
                            if args.session and not str(record.get("session_id", "")).startswith(args.session):
                                continue
                            show(record, stats)
                        position = handle.tell()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        print(_c("Tổng kết: " + stats.line(), "1"))


if __name__ == "__main__":
    main()
