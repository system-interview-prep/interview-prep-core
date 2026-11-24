"""Trí nhớ về ứng viên xuyên phiên (INTERVIEW_AGENT_DECISION_PLAN_V1 §7, LANGMEM plan).

Store: the LangGraph ``AsyncPostgresStore`` the agent opens next to its
checkpointer (``agent/shared/checkpoints.py``); namespace ``("candidate", user_id)``.
An item names a topic — "còn hở phần invalidation" — never the candidate's
words (§5 of the plan: the interviewer must not recite). The flag
``INTERVIEW_CANDIDATE_MEMORY`` is ``0`` (default), ``read`` (seed from the gap
notebook, search in a turn) or ``readwrite`` (also extract after a session).

Items are stored the way ``langmem`` stores them — ``{"kind": "CandidateMemory",
"content": {...}}`` — so its memory manager (LANGMEM_EXTRACTOR_SWAP_SPEC_V1) can
insert, patch and remove them after ``score``. Readers unwrap ``content``.
The model calls it makes go through LangChain, not ``ai.facade``; the
``_AiCallLog`` callback writes the same ``ai_call_logs`` row per call and the
whole extraction is one Langfuse generation.

Nothing here is on the critical path of a turn: a store failure returns no
items; extraction runs in the ``score`` command, after grading, and never
raises. ``langmem``/``langchain-openai`` (extra ``memory``) are imported only
when extraction runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.pipeline_events import ai_call_timer
from src.core.trace_logging import trace_event
from src.infrastructure import langfuse
from src.modules.interviews.agent.shared.checkpoints import get_interview_store
from src.modules.interviews.application.progress.progress_repository import load_progress
from src.modules.interviews.core.conversation.prompt.candidate_memory import (
    MAX_ITEMS,
    extract_instructions,
)
from src.modules.interviews.evaluation.evaluation_service import get_session_evaluation

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60.0
MAX_OPS = 5
KINDS = ("gap", "strength", "project_fact", "preference")
PURPOSE_EXTRACT = "interviews.candidate_memory.extract"
SCHEMA_KIND = "CandidateMemory"
MAX_TEXT_CHARS = 160

_cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}


class CandidateMemory(BaseModel):
    """One memory the extractor may insert or patch (the ``langmem`` schema).

    Only these four fields are for the model; the bookkeeping keys that
    ``_stamp`` adds (``source``, ``evidence``, ``last_seen``, ``times_seen``)
    ride along as extras so a patch does not drop them.
    """

    model_config = ConfigDict(extra="allow")

    kind: Literal["gap", "strength", "project_fact", "preference"] = Field(
        description="gap = ý còn hở; strength = ý đã nắm; project_fact = dự án đã kể; preference = lưu ý khi hỏi"
    )
    skill_key: str = Field(description="Mã kỹ năng của câu hỏi (competency), ví dụ skill-redis")
    text: str = Field(max_length=MAX_TEXT_CHARS, description="MỘT câu ngắn gọi tên chủ đề; không trích lời, không điểm số, không thông tin cá nhân")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Mức chắc chắn của người chấm, 0–1")


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


def wrap(content: Dict[str, Any]) -> Dict[str, Any]:
    """The stored value of an item: ``langmem``'s ``{"kind", "content"}`` envelope."""
    return {"kind": SCHEMA_KIND, "content": dict(content)}


def unwrap(value: Any) -> Dict[str, Any]:
    """The item inside a stored value; a value written flat (before the envelope) is returned as is."""
    if isinstance(value, dict) and isinstance(value.get("content"), dict) and value.get("kind") == SCHEMA_KIND:
        return dict(value["content"])
    return dict(value or {})


async def _all_values(user_id: str, store: Any) -> Dict[str, Any]:
    found = await store.asearch(namespace(user_id), limit=200)
    return {hit.key: hit.value for hit in found}


async def _all_items(user_id: str, store: Any) -> List[Dict[str, Any]]:
    items = []
    for key, value in (await _all_values(user_id, store)).items():
        item = unwrap(value)
        item["key"] = key
        items.append(item)
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
            text_value = f"{prefix}: {point} ({entry.get('skill') or skill_key})"[:MAX_TEXT_CHARS]
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
            await store.aput(namespace(user_id), item["key"], wrap({k: v for k, v in item.items() if k != "key"}))
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


def _extract_input(evaluation: Dict[str, Any], is_vi: bool) -> str:
    """Grader conclusions only: no evidence_quotes (the candidate's words), no scores.

    The existing items are not listed here: the memory manager shows them to the
    model itself, with the ids a patch or removal must name.
    """
    lines = ["Kết luận của người chấm theo từng câu:" if is_vi else "Grader conclusions per question:"]
    for turn in evaluation.get("turn_results") or evaluation.get("turnResults") or []:
        lines.append(
            f"- skill: {turn.get('competency')} | stage: {turn.get('stage')} | outcome: {turn.get('outcome')}\n"
            f"  question: {str(turn.get('question_text') or '')[:160]}\n"
            f"  strengths: {'; '.join(str(s) for s in (turn.get('strengths') or [])[:4])}\n"
            f"  weaknesses: {'; '.join(str(w) for w in (turn.get('weaknesses') or [])[:4])}"
        )
    return "\n".join(lines)


def _chat_model() -> Any:
    """The LangChain chat model the memory manager calls (same model and key as ``ai.facade``)."""
    from langchain_openai import ChatOpenAI

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        timeout=settings.ai_request_timeout_seconds,
        max_retries=1,
    )


def _usage_of(response: Any) -> Any:
    """``input_tokens``/``output_tokens`` of one LangChain LLMResult (None when the provider sent none)."""
    usage = None
    try:
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None)
    except (AttributeError, IndexError):
        pass
    if not usage:
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        if usage:
            usage = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
    if not usage:
        return None
    return SimpleNamespace(input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))


def _output_of(response: Any) -> str:
    try:
        message = response.generations[0][0].message
    except (AttributeError, IndexError):
        return ""
    calls = getattr(message, "tool_calls", None)
    if calls:
        return json.dumps(calls, ensure_ascii=False, default=str)
    return str(getattr(message, "content", "") or "")


def _make_call_log(purpose: str, model: str) -> Any:
    """A LangChain callback that writes one ``ai_call_logs`` row per model call.

    ``langmem`` calls the model through LangChain, so the facade's timer is not
    on its path; this handler opens ``ai_call_timer`` when a chat model starts
    and closes it (with usage, or with the error) when it ends. Totals feed the
    Langfuse generation of the whole extraction.
    """
    from langchain_core.callbacks import BaseCallbackHandler

    class _AiCallLog(BaseCallbackHandler):
        run_inline = True

        def __init__(self) -> None:
            self.timers: Dict[Any, ai_call_timer] = {}
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0

        def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any) -> None:
            flat = "\n".join(str(getattr(m, "content", m)) for batch in messages for m in batch)
            timer = ai_call_timer(purpose, provider="openai", model=model, input_text=flat)
            timer.__enter__()
            self.timers[run_id] = timer
            self.calls += 1

        def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
            timer = self.timers.pop(run_id, None)
            if timer is None:
                return
            usage = _usage_of(response)
            timer.done(_output_of(response), usage)
            self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            timer.__exit__(None, None, None)

        def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
            timer = self.timers.pop(run_id, None)
            if timer is not None:
                timer.__exit__(type(error), error, error.__traceback__)

        def usage(self) -> Any:
            return SimpleNamespace(input_tokens=self.input_tokens, output_tokens=self.output_tokens)

    return _AiCallLog()


def _manager(model: Any, store: Any, is_vi: bool) -> Any:
    from langmem import create_memory_store_manager

    return create_memory_store_manager(
        model,
        schemas=[CandidateMemory],
        instructions=extract_instructions(is_vi),
        enable_inserts=True,
        enable_deletes=True,
        # No semantic index on the store: the query is ignored and the namespace
        # is listed whole, which is what a notebook of ≤ a few dozen items needs.
        query_limit=200,
        namespace=("candidate", "{user_id}"),
        store=store,
    )


def _changes(before: Dict[str, Any], after: Dict[str, Any]) -> List[tuple[str, str]]:
    """``(action, key)`` for every row that differs, inserts first, in key order."""
    inserted = sorted(k for k in after if k not in before)
    updated = sorted(k for k in after if k in before and after[k] != before[k])
    deleted = sorted(k for k in before if k not in after)
    return [("added", k) for k in inserted] + [("updated", k) for k in updated] + [("retired", k) for k in deleted]


def _stamp(before: Dict[str, Any], after: Dict[str, Any], action: str, key: str, session_id: str, now: str) -> Dict[str, Any]:
    """Bookkeeping on a row the manager wrote: who, when, how often; text normalised."""
    item = unwrap(after.get(key))
    item["text"] = " ".join(str(item.get("text") or "").split())[:MAX_TEXT_CHARS]
    item["source"] = "extracted" if action == "added" else str(item.get("source") or "extracted")
    item["evidence"] = {**(item.get("evidence") or {}), "session_id": session_id, "date": now}
    item["last_seen"] = now
    item["times_seen"] = int(unwrap(before.get(key)).get("times_seen") or 0) + 1 if action == "updated" else 1
    return wrap(item)


async def _settle(
    user_id: str, before: Dict[str, Any], after: Dict[str, Any], *, store: Any, session_id: str
) -> Dict[str, int]:
    """Keep at most MAX_OPS of the manager's changes, roll the rest back, stamp what stays."""
    ns = namespace(user_id)
    now = datetime.now(UTC).isoformat()
    changes = _changes(before, after)
    counts = {"added": 0, "updated": 0, "retired": 0, "rejected": 0, "truncated": 0}
    kept, dropped = changes[:MAX_OPS], changes[MAX_OPS:]
    counts["truncated"] = len(dropped)
    for action, key in dropped:
        if action == "added":
            await store.adelete(ns, key)
        else:
            await store.aput(ns, key, before[key])
    for action, key in kept:
        if action == "retired":
            counts["retired"] += 1
            continue
        item = unwrap(after.get(key))
        if item.get("kind") not in KINDS or not str(item.get("skill_key") or "").strip() or not str(item.get("text") or "").strip():
            counts["rejected"] += 1
            if action == "added":
                await store.adelete(ns, key)
            else:
                await store.aput(ns, key, before[key])
            continue
        await store.aput(ns, key, _stamp(before, after, action, key, session_id, now))
        counts[action] += 1
    clear_cache(user_id)
    return counts


async def extract_after_session(
    db: AsyncSession,
    session_id: str,
    *,
    store: Any = None,
    model: Any = None,
    is_vi: bool = True,
) -> Dict[str, int]:
    """After grading: the ``langmem`` memory manager runs once on the grader's conclusions.

    It inserts, patches and removes items in the user's namespace; at most
    MAX_OPS changes survive. Every model call is an ``ai_call_logs`` row; the
    whole run is one Langfuse generation. Never raises.
    """
    store = _store_or_none(store)
    if store is None:
        return {}
    try:
        evaluation = await get_session_evaluation(db, str(session_id))
        if not evaluation:
            return {}
        user_id = await _user_id_of_session(db, str(session_id))
        if not user_id:
            return {}
        model = model or _chat_model()
        model_name = str(getattr(model, "model_name", None) or getattr(model, "model", None) or get_settings().llm_model)
        instructions = extract_instructions(is_vi)
        input_text = _extract_input(evaluation, is_vi)
        before = await _all_values(user_id, store)
        call_log = _make_call_log(PURPOSE_EXTRACT, model_name)
        generation = langfuse.Generation(
            PURPOSE_EXTRACT, model=model_name, instructions=instructions, input_text=input_text,
            options={"max_changes": MAX_OPS, "existing": len(before)},
        )
        try:
            await _manager(model, store, is_vi).ainvoke(
                {"messages": [{"role": "user", "content": input_text}]},
                config={"configurable": {"user_id": user_id}, "callbacks": [call_log]},
            )
        except Exception as exc:
            generation.end(error=exc, usage=call_log.usage())
            raise
        after = await _all_values(user_id, store)
        counts = await _settle(user_id, before, after, store=store, session_id=str(session_id))
        generation.end(output=json.dumps(counts), usage=call_log.usage())
        if counts["truncated"]:
            trace_event("interviewer", "candidate_memory_ops_capped", session_id=str(session_id), truncated=counts["truncated"])
        trace_event("interviewer", "candidate_memory_extracted", session_id=str(session_id), model_calls=call_log.calls, **counts)
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
