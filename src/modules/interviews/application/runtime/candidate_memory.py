"""Trí nhớ về ứng viên xuyên phiên (INTERVIEW_AGENT_DECISION_PLAN_V1 §7, LANGMEM plan).

Store: the LangGraph ``AsyncPostgresStore`` the agent opens next to its
checkpointer (``agent/checkpoints.py``); namespace ``("candidate", user_id)``.
An item names a topic — "còn hở phần invalidation" — never the candidate's
words (§5 of the plan: the interviewer must not recite). The flag
``INTERVIEW_CANDIDATE_MEMORY`` is ``0`` (default), ``read`` (seed from the gap
notebook, search in a turn) or ``readwrite`` (also extract after a session).

Nothing here is on the critical path of a turn: a store failure returns no
items; extraction runs in the ``score`` command, after grading.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_text
from src.modules.interviews.agent.checkpoints import get_interview_store
from src.modules.interviews.application.progress.progress_repository import load_progress
from src.modules.interviews.core.conversation.prompt.candidate_memory import (
    MAX_ITEMS,
    MEMORY_OPS_SCHEMA,
    extract_prompt,
)
from src.modules.interviews.evaluation.evaluation_service import get_session_evaluation

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60.0
MAX_OPS = 5
MAX_EXISTING_IN_PROMPT = 20
KINDS = ("gap", "strength", "project_fact", "preference")
PURPOSE_EXTRACT = "interviews.candidate_memory.extract"

_cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}


def enabled() -> str:
    value = os.getenv("INTERVIEW_CANDIDATE_MEMORY", "0").strip().lower()
    if value in ("read", "readwrite"):
        return value
    return "0"


def namespace(user_id: str) -> tuple[str, str]:
    return ("candidate", str(user_id))


def item_key(skill_key: str, text_value: str) -> str:
    return hashlib.sha1(f"{skill_key.strip().lower()}|{' '.join(text_value.split()).lower()}".encode()).hexdigest()[:16]


def version(items: Sequence[Dict[str, Any]]) -> str:
    """Short hash of the items in play ("" when none) — stored on the trace like playbook.version()."""
    if not items:
        return ""
    joined = "|".join(sorted(f"{i.get('key')}:{i.get('text')}" for i in items))
    return hashlib.sha1(joined.encode()).hexdigest()[:8]


def clear_cache(user_id: Optional[str] = None) -> None:
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(str(user_id), None)


def _store_or_none(store: Any) -> Any:
    return store if store is not None else get_interview_store()


async def _all_items(user_id: str, store: Any) -> List[Dict[str, Any]]:
    found = await store.asearch(namespace(user_id), limit=200)
    items = []
    for hit in found:
        value = dict(hit.value)
        value["key"] = hit.key
        items.append(value)
    items.sort(key=lambda i: str(i.get("last_seen") or ""), reverse=True)
    return items


async def load(
    user_id: str,
    *,
    skill_key: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = MAX_ITEMS,
    store: Any = None,
) -> List[Dict[str, Any]]:
    """The items of a user (cached 60 s), newest first, filtered by skill/kind, capped."""
    user_id = str(user_id or "")
    store = _store_or_none(store)
    if not user_id or store is None:
        return []
    cached = _cache.get(user_id)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        items = cached[1]
    else:
        try:
            items = await _all_items(user_id, store)
        except Exception as exc:  # noqa: BLE001 - memory never costs the candidate a turn
            logger.warning("candidate memory unavailable: %s", exc)
            return []
        _cache[user_id] = (time.monotonic(), items)
    if skill_key:
        wanted = str(skill_key).strip().lower()
        items = [i for i in items if str(i.get("skill_key") or "").lower() == wanted] or items
    if kind:
        items = [i for i in items if i.get("kind") == kind]
    return [dict(i) for i in items[:limit]]


def search_for(user_id: str, *, store: Any = None) -> Callable[[str], Awaitable[Sequence[Dict[str, Any]]]]:
    """The ``search_candidate_memory`` tool of the decider, bound to one user."""

    async def search(skill_key: str) -> Sequence[Dict[str, Any]]:
        hits = await load(user_id, skill_key=skill_key, store=store)
        # Topic only: the decider never sees where a memory came from.
        return [{"kind": h.get("kind"), "skill_key": h.get("skill_key"), "text": h.get("text")} for h in hits]

    return search


# --------------------------------------------------------------------------- seed

def _seed_items(gaps: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    items = []
    for kind, bucket, prefix in (("gap", "open", "Còn hở"), ("strength", "fixed", "Đã nắm")):
        for entry in gaps.get(bucket) or []:
            skill_key = str(entry.get("skillKey") or entry.get("skill") or "").strip()
            point = str(entry.get("point") or "").strip()
            if not skill_key or not point:
                continue
            last = entry.get("lastSeen") or {}
            text_value = f"{prefix}: {point} ({entry.get('skill') or skill_key})"[:160]
            items.append({
                "key": item_key(skill_key, text_value),
                "kind": kind,
                "skill_key": skill_key,
                "text": text_value,
                "source": "seed_gap_notebook",
                "evidence": {
                    "session_id": str(last.get("sessionId") or ""),
                    "date": str(last.get("date") or ""),
                    "question": str(last.get("question") or "")[:240],
                },
                "confidence": 0.7 if kind == "gap" else 0.6,
                "last_seen": str(last.get("date") or now),
                "times_seen": int(entry.get("timesAsked") or 1),
            })
    return items


async def seed_from_gap_notebook(db: AsyncSession, user_id: str, *, store: Any = None) -> int:
    """Seed an empty namespace from the progress gap notebook; idempotent, never overwrites.

    Returns the number of items written (0 when the namespace already had any).
    """
    user_id = str(user_id or "")
    store = _store_or_none(store)
    if not user_id or store is None:
        return 0
    try:
        if await store.asearch(namespace(user_id), limit=1):
            return 0
        progress = await load_progress(db, user_id)
        items = _seed_items(progress.get("gaps") or {})
        for item in items:
            value = {k: v for k, v in item.items() if k != "key"}
            await store.aput(namespace(user_id), item["key"], value)
        clear_cache(user_id)
        trace_event("interviewer", "candidate_memory_seeded", user_id=user_id, items=len(items))
        return len(items)
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate memory seed failed: %s", exc)
        return 0


# --------------------------------------------------------------------------- extract

async def _user_id_of_session(db: AsyncSession, session_id: str) -> str:
    row = (
        await db.execute(text("SELECT user_id FROM interview_sessions WHERE id = :sid"), {"sid": session_id})
    ).first()
    return str(row[0]) if row and row[0] else ""


def _extract_input(evaluation: Dict[str, Any], existing: Sequence[Dict[str, Any]], is_vi: bool) -> str:
    """Grader conclusions only: no evidence_quotes (the candidate's words), no scores."""
    lines = ["Các mục đang có (key | kind | skill | text):" if is_vi else "Existing items (key | kind | skill | text):"]
    for item in list(existing)[:MAX_EXISTING_IN_PROMPT]:
        lines.append(f"- {item.get('key')} | {item.get('kind')} | {item.get('skill_key')} | {item.get('text')}")
    if len(lines) == 1:
        lines.append("- (trống)" if is_vi else "- (none)")
    lines.append("")
    lines.append("Kết luận của người chấm theo từng câu:" if is_vi else "Grader conclusions per question:")
    for turn in evaluation.get("turn_results") or evaluation.get("turnResults") or []:
        lines.append(
            f"- skill: {turn.get('competency')} | stage: {turn.get('stage')} | outcome: {turn.get('outcome')}\n"
            f"  question: {str(turn.get('question_text') or '')[:160]}\n"
            f"  strengths: {'; '.join(str(s) for s in (turn.get('strengths') or [])[:4])}\n"
            f"  weaknesses: {'; '.join(str(w) for w in (turn.get('weaknesses') or [])[:4])}"
        )
    return "\n".join(lines)


def _parse_ops(raw: str) -> List[Dict[str, Any]]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    ops = data.get("ops") if isinstance(data, dict) else None
    return [op for op in (ops or []) if isinstance(op, dict)]


async def apply_ops(
    user_id: str, ops: Sequence[Dict[str, Any]], *, store: Any, session_id: str = ""
) -> Dict[str, int]:
    """Write ≤ MAX_OPS operations; ADD keys are deterministic so a re-run writes the same rows."""
    ns = namespace(user_id)
    now = datetime.now(UTC).isoformat()
    counts = {"added": 0, "updated": 0, "retired": 0, "rejected": 0, "truncated": 0}
    if len(ops) > MAX_OPS:
        counts["truncated"] = len(ops) - MAX_OPS
        ops = list(ops)[:MAX_OPS]
    for op in ops:
        kind = str(op.get("kind") or "")
        skill_key = str(op.get("skill_key") or "").strip()
        text_value = " ".join(str(op.get("text") or "").split())[:160]
        action = str(op.get("op") or "").upper()
        if action == "RETIRE":
            key = str(op.get("key") or "")
            if key and await store.aget(ns, key) is not None:
                await store.adelete(ns, key)
                counts["retired"] += 1
            else:
                counts["rejected"] += 1
            continue
        if kind not in KINDS or not skill_key or not text_value:
            counts["rejected"] += 1
            continue
        confidence = max(0.0, min(1.0, float(op.get("confidence") or 0.5)))
        if action == "UPDATE":
            key = str(op.get("key") or "")
            current = await store.aget(ns, key) if key else None
            if current is None:
                counts["rejected"] += 1
                continue
            value = dict(current.value)
            value.update({"kind": kind, "skill_key": skill_key, "text": text_value, "confidence": confidence,
                          "last_seen": now, "times_seen": int(value.get("times_seen") or 0) + 1})
            value.setdefault("evidence", {})
            value["evidence"] = {**value["evidence"], "session_id": session_id, "date": now}
            await store.aput(ns, key, value)
            counts["updated"] += 1
        elif action == "ADD":
            key = item_key(skill_key, text_value)
            await store.aput(ns, key, {
                "kind": kind, "skill_key": skill_key, "text": text_value, "source": "extracted",
                "evidence": {"session_id": session_id, "date": now, "question": ""},
                "confidence": confidence, "last_seen": now, "times_seen": 1,
            })
            counts["added"] += 1
        else:
            counts["rejected"] += 1
    clear_cache(user_id)
    return counts


async def extract_after_session(
    db: AsyncSession,
    session_id: str,
    *,
    store: Any = None,
    generate: Any = None,
    is_vi: bool = True,
) -> Dict[str, int]:
    """After grading: one model call on the grader's conclusions → ≤ 5 memory operations."""
    store = _store_or_none(store)
    if store is None:
        return {}
    generate = generate or generate_text
    try:
        evaluation = await get_session_evaluation(db, str(session_id))
        if not evaluation:
            return {}
        user_id = await _user_id_of_session(db, str(session_id))
        if not user_id:
            return {}
        existing = await _all_items(user_id, store)
        raw = await generate(
            instructions=extract_prompt(is_vi),
            input_text=_extract_input(evaluation, existing, is_vi),
            json_schema=MEMORY_OPS_SCHEMA,
            purpose=PURPOSE_EXTRACT,
            max_output_tokens=900,
        )
        counts = await apply_ops(user_id, _parse_ops(raw), store=store, session_id=str(session_id))
        trace_event("interviewer", "candidate_memory_extracted", session_id=str(session_id), **counts)
        return counts
    except Exception as exc:  # noqa: BLE001 - grading already succeeded; memory must not undo that
        logger.warning("candidate memory extraction failed: %s", exc)
        trace_event("interviewer", "candidate_memory_extract_error", session_id=str(session_id), error_type=type(exc).__name__)
        return {}


async def delete_user(user_id: str, *, store: Any = None) -> int:
    """Remove every memory of a user (account deletion)."""
    store = _store_or_none(store)
    if store is None:
        return 0
    items = await _all_items(str(user_id), store)
    for item in items:
        await store.adelete(namespace(str(user_id)), item["key"])
    clear_cache(str(user_id))
    return len(items)
