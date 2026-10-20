"""Chấm độ tự nhiên của người phỏng vấn AI trên một buổi phỏng vấn THẬT.

Lấy toàn bộ hội thoại của buổi từ database, tính các chỉ số tự động và nhờ AI
chấm (giống scripts/eval_interviewer_naturalness.py, nhưng trên dữ liệu thật
thay vì 40 câu mẫu). Kết quả ghi ra trace-logs/session_eval_<mã buổi>_<giờ>.md
và .json.

Chạy trong thư mục interview-prep-core, sau khi buổi phỏng vấn kết thúc:

    docker compose exec backend python scripts/eval_session.py latest
    docker compose exec backend python scripts/eval_session.py <mã buổi>
    docker compose exec backend python scripts/eval_session.py latest --no-judge   # chỉ chỉ số, không gọi AI

Chỉ số:
- Câu ghi nhận: tỉ lệ AI viết / dự phòng (từ trace nếu có), câu khác nhau, câu lặp
  trong 3 lượt gần nhất, từ đánh giá hoặc kết luận chất lượng, giọng tóm tắt bài.
- Độ trễ: thời gian Interview Core xử lý mỗi lượt (trung vị, p95), lấy từ file theo dõi.
  Không tính theo giờ lưu tin nhắn: tin của ứng viên và của AI được lưu cùng lúc.
- AI chấm: cả chuỗi lời người phỏng vấn (từng đoạn 10 lượt) và từng lượt (1-5, kèm
  câu nghe cứng nhất).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from pathlib import Path

from sqlalchemy import text

from src.infrastructure.database import StandaloneSessionFactory
from src.modules.ai.facade import generate_text
from src.modules.interviews.core.interview_engine import _loads_llm_json
from src.modules.interviews.core.interviewer_voice import (
    ACK_FALLBACK_VI,
    GIVE_UP_ACK_VI,
    NON_ANSWER_ACK_VI,
    first_paragraph,
    is_evaluative,
    is_grader_style,
    is_judgmental,
    is_parroting,
    last_paragraph,
)

SEQ_JUDGE = """Bạn là chuyên gia đánh giá hội thoại phỏng vấn tiếng Việt. Đây là chuỗi câu người phỏng vấn AI đã nói
liên tiếp trong MỘT buổi phỏng vấn thật (mỗi dòng là một lượt). Chấm độ tự nhiên khi nghe liền cả buổi, 1-5
(lặp lại, máy móc, như đang chấm bài thì điểm thấp). Trả về đúng 1 JSON:
{"score": <1-5>, "reason": "<1-2 câu>", "stiffest": "<câu nghe cứng nhất, chép nguyên văn>"}"""

TURN_JUDGE = """Bạn là chuyên gia đánh giá hội thoại phỏng vấn tiếng Việt. Cho câu ứng viên vừa nói và câu người phỏng
vấn AI đáp lại ngay sau đó. Chấm độ TỰ NHIÊN của câu đáp như người phỏng vấn thật đang lắng nghe, 1-5
(5 = rất tự nhiên, đúng mực; 1 = máy móc, cụt, chung chung, hoặc như đang chấm bài). Khen chê hay nói đúng/sai
trước mặt ứng viên bị trừ điểm. Trả về đúng 1 JSON: {"score": <1-5>, "issue": "<vấn đề chính, để trống nếu ổn>"}"""


def _words(value: str) -> list[str]:
    return re.findall(r"\w+", value.casefold())


async def _resolve_session(db, wanted: str) -> str | None:
    if wanted and wanted != "latest":
        row = await db.execute(
            text("SELECT DISTINCT session_id FROM interview_chat_messages WHERE CAST(session_id AS TEXT) LIKE :p LIMIT 2"),
            {"p": f"{wanted}%"},
        )
        found = [str(r[0]) for r in row.all()]
        return found[0] if len(found) == 1 else None
    row = await db.execute(
        text("SELECT session_id FROM interview_chat_messages ORDER BY created_at DESC LIMIT 1")
    )
    first = row.first()
    return str(first[0]) if first else None


async def _load_messages(db, session_id: str) -> list[dict]:
    result = await db.execute(
        text(
            """
            SELECT role, content, message_type, created_at, sequence
            FROM interview_chat_messages
            WHERE session_id = :sid
            ORDER BY sequence
            """
        ),
        {"sid": session_id},
    )
    return [dict(r) for r in result.mappings().all()]


def _trace_records(session_id: str) -> list[dict]:
    """Mọi lượt của buổi trong file theo dõi, theo thứ tự ghi.

    Không gom theo turn_index: một câu hỏi có thể có nhiều lượt (giải thích lại,
    xin dừng) cùng turn_index.
    """
    path = Path("trace-logs/naturalness/trace.jsonl")
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == "turn_spoken" and str(record.get("session_id")) == session_id:
            out.append(record)
    return out


def _attach_traces(turns: list[dict], traces: list[dict]) -> None:
    """Gắn bản ghi theo dõi vào từng lượt, khớp theo lời ứng viên (đã cắt 300 ký tự)."""
    remaining = list(traces)
    for turn in turns:
        key = " ".join(turn["candidate"].split())[:300]
        match = next((r for r in remaining if " ".join(str(r.get("candidate") or "").split()) == key), None)
        if match is None:
            continue
        remaining.remove(match)
        turn["trace"] = match
        if isinstance(match.get("core_ms"), (int, float)):
            turn["latency_s"] = round(match["core_ms"] / 1000, 2)


def _turns(messages: list[dict]) -> list[dict]:
    """Pair each candidate message with the interviewer reply that follows it."""
    turns = []
    for i, message in enumerate(messages):
        if message["role"] != "user":
            continue
        reply = next((m for m in messages[i + 1:] if m["role"] == "assistant"), None)
        if reply is None:
            continue
        content = str(reply["content"] or "")
        ack = first_paragraph(content) if "\n\n" in content else ""
        latency = None  # filled from the trace (core_ms)
        turns.append(
            {
                "candidate": str(message["content"] or ""),
                "reply": content,
                "ack": ack,
                "question": last_paragraph(content),
                "type": reply.get("message_type"),
                "latency_s": latency,
            }
        )
    return turns


def _metrics(turns: list[dict], traces: list[dict]) -> dict:
    acks = [t["ack"] for t in turns if t["ack"]]
    n = len(acks) or 1
    repeats = sum(
        1 for i, a in enumerate(acks) if a and a in acks[max(0, i - 3):i]
    )
    pools = set(ACK_FALLBACK_VI) | set(GIVE_UP_ACK_VI) | set(NON_ANSWER_ACK_VI)
    sources: dict[str, int] = {}
    for record in traces:
        key = str(record.get("ack_source") or "-")
        sources[key] = sources.get(key, 0) + 1
    latencies = sorted(t["latency_s"] for t in turns if isinstance(t["latency_s"], (int, float)))
    p95 = latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))] if latencies else None
    return {
        "turns": len(turns),
        "acks": len(acks),
        "distinct_ratio": round(len(set(acks)) / n, 3),
        "repeats_within_3": repeats,
        "neutral_pool_rate": round(sum(a in pools for a in acks) / n, 3),
        "evaluative_or_judgmental": sum(is_evaluative(a) or is_judgmental(a) for a in acks),
        "grader_or_parroting": sum(
            is_grader_style(t["ack"]) or is_parroting(t["ack"], t["candidate"]) for t in turns if t["ack"]
        ),
        "avg_words": round(statistics.mean(len(_words(a)) for a in acks), 1) if acks else 0,
        "ack_sources_from_trace": sources,
        "latency_median_s": round(statistics.median(latencies), 1) if latencies else None,
        "latency_p95_s": round(p95, 1) if p95 is not None else None,
    }


async def _judge(turns: list[dict]) -> dict:
    lines = [t["ack"] or t["question"] for t in turns]
    sequences = []
    for start in range(0, len(lines), 10):
        chunk = "\n".join(f"{k + 1}. {line}" for k, line in enumerate(lines[start:start + 10]))
        try:
            raw = await generate_text(instructions=SEQ_JUDGE, input_text=chunk, max_output_tokens=600, temperature=0)
            sequences.append(_loads_llm_json(raw))
        except Exception as exc:  # noqa: BLE001
            sequences.append({"error": str(exc)})
    for t in turns:
        try:
            raw = await generate_text(
                instructions=TURN_JUDGE,
                input_text=f"Ứng viên: {t['candidate']}\n\nNgười phỏng vấn AI: {t['reply']}",
                max_output_tokens=400,
                temperature=0,
            )
            t["judge"] = _loads_llm_json(raw)
        except Exception as exc:  # noqa: BLE001
            t["judge"] = {"error": str(exc)}
    scores = [float(t["judge"]["score"]) for t in turns if "score" in t.get("judge", {})]
    seq_scores = [float(s["score"]) for s in sequences if "score" in s]
    return {
        "sequence": sequences,
        "sequence_mean": round(statistics.mean(seq_scores), 2) if seq_scores else None,
        "turn_mean": round(statistics.mean(scores), 2) if scores else None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", nargs="?", default="latest")
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()

    async with StandaloneSessionFactory() as db:
        session_id = await _resolve_session(db, args.session)
        if not session_id:
            print("Không tìm thấy buổi phỏng vấn (hoặc mã khớp nhiều buổi).")
            return
        messages = await _load_messages(db, session_id)
    turns = _turns(messages)
    if not turns:
        print(f"Buổi {session_id} chưa có lượt trả lời nào.")
        return
    traces = _trace_records(session_id)
    _attach_traces(turns, traces)
    metrics = _metrics(turns, traces)
    judged = None if args.no_judge else await _judge(turns)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = Path("trace-logs")
    out.mkdir(exist_ok=True)
    base = out / f"session_eval_{session_id[:8]}_{stamp}"
    base.with_suffix(".json").write_text(
        json.dumps({"session_id": session_id, "metrics": metrics, "judge": judged, "turns": turns},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    m = metrics
    lines = [
        f"# Chấm buổi phỏng vấn thật {session_id[:8]} ({stamp})", "",
        f"- Số lượt: {m['turns']} (có câu ghi nhận: {m['acks']})",
        f"- Câu ghi nhận khác nhau: {m['distinct_ratio']:.0%}; lặp trong 3 lượt gần nhất: {m['repeats_within_3']}",
        f"- Câu đáp trung tính có sẵn: {m['neutral_pool_rate']:.0%}; nguồn theo trace: {m['ack_sources_from_trace'] or 'không có trace'}",
        f"- Từ đánh giá / kết luận chất lượng: {m['evaluative_or_judgmental']}; giọng tóm tắt bài / chép lại: {m['grader_or_parroting']}",
        f"- Số từ trung bình mỗi câu ghi nhận: {m['avg_words']}",
        f"- Độ trễ Core: trung vị {m['latency_median_s']} s, p95 {m['latency_p95_s']} s"
        + ("" if m["latency_median_s"] is not None else " (không có file theo dõi cho buổi này)"),
    ]
    if judged:
        lines += [
            f"- **AI chấm cả chuỗi (1-5): {judged['sequence_mean']}**; AI chấm từng lượt (1-5): {judged['turn_mean']}",
            "",
        ]
        for s in judged["sequence"]:
            if "score" in s:
                lines.append(f"  - Đoạn: {s['score']}/5 — {s.get('reason', '')} Câu cứng nhất: \"{s.get('stiffest', '')}\"")
    lines += ["", "## Từng lượt", "", "| # | Ứng viên | AI đáp | Nguồn câu ghi nhận | Điểm | Vấn đề | Trễ |",
              "|---|---|---|---|---|---|---|"]
    for i, t in enumerate(turns, 1):
        j = t.get("judge") or {}
        cand = " ".join(t["candidate"].split())[:70]
        reply = " ".join(t["reply"].split())[:110]
        lat = f"{t['latency_s']:.1f}s" if isinstance(t["latency_s"], (int, float)) else ""
        tr = t.get("trace") or {}
        src = str(tr.get("ack_source") or tr.get("action") or "")
        if tr.get("ack_reason") and tr.get("ack_source") == "fallback":
            src += f" ({tr['ack_reason']})"
        lines.append(f"| {i} | {cand} | {reply} | {src} | {j.get('score', '')} | {j.get('issue', '')} | {lat} |")
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:12]))
    print(f"\nĐã ghi {base}.md và .json")


if __name__ == "__main__":
    asyncio.run(main())
