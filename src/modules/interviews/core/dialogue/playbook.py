"""The interviewer's playbook: lessons from past interviews that a person approved.

"AI tích luỹ skill phỏng vấn" (DOC_AND_PLAN/plan/interview/ADVISOR_FEEDBACK_RESEARCH_2026-10-26.md,
section 3). Modelled on ACE (Agentic Context Engineering): short, itemised
lessons with ids, edited one at a time, never a rewrite of the whole context.

This module only holds the ACTIVE entries in memory and renders them into the
prompts of the engine steps that read a candidate's answer, write a follow-up
or re-explain a question. The application layer
(``application/interviewer_playbook.py``) loads them from the database and
proposes new ones; only entries a person approved ever reach a prompt, and the
block always sits below the fixed rules, which win on any conflict.

Empty playbook = no block = the prompts are exactly as before.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

STEPS = ("evaluate_answer", "probe", "clarify")
ALL_STEPS = "all"
MAX_ENTRIES_PER_PROMPT = 8
MAX_BLOCK_CHARS = 1200

_entries: list[dict[str, Any]] = []
_version = ""


def load(entries: Iterable[dict[str, Any]]) -> None:
    """Replace the in-memory playbook (ACTIVE entries: id, step, text, skill)."""
    global _entries, _version
    _entries = [
        {
            "id": str(e["id"]),
            "step": str(e.get("step") or ALL_STEPS),
            "text": str(e["text"]).strip(),
            "skill": e.get("skill"),
        }
        for e in entries
        if str(e.get("text") or "").strip()
    ]
    joined = "|".join(f"{e['id']}:{e['text']}" for e in sorted(_entries, key=lambda e: e["id"]))
    digest = hashlib.sha1(joined.encode())
    _version = digest.hexdigest()[:8] if _entries else ""


def version() -> str:
    """Short hash of the active playbook ("" when empty); stored on every traced turn."""
    return _version


def ids() -> list[str]:
    return [e["id"] for e in _entries]


def entries_for(step: str, skill: str | None = None) -> list[dict[str, Any]]:
    chosen = [
        e
        for e in _entries
        if e["step"] in (step, ALL_STEPS)
        and (not e.get("skill") or (skill and str(e["skill"]).strip().lower() == str(skill).strip().lower()))
    ]
    return chosen[:MAX_ENTRIES_PER_PROMPT]


def block(step: str, is_vi: bool = True, skill: str | None = None) -> str:
    """Prompt text for ``step``; "" when nothing applies."""
    chosen = entries_for(step, skill)
    if not chosen:
        return ""
    head = (
        "Kinh nghiệm phỏng vấn đã được người duyệt "
        "(áp dụng khi phù hợp; mọi quy tắc ở trên luôn được ưu tiên):"
        if is_vi
        else "Interviewing lessons approved by a reviewer "
        "(apply when relevant; every rule above takes precedence):"
    )
    lines, size = [], len(head)
    for entry in chosen:
        line = f"- {entry['text']}"
        if size + len(line) > MAX_BLOCK_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n\n" + head + "\n" + "\n".join(lines)
