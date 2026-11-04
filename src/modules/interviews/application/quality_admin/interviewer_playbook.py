"""The interviewer learns from past interviews ("AI tích luỹ skill phỏng vấn").

Design: ADVISOR_FEEDBACK_RESEARCH_2026-10-26.md, section 3 (after ACE, ExpeL).

Loop::

    turns (interview_pipeline_events.turn_spoken: rule flags, decision, reply)
      + reviewer / AI-judge verdicts from Langfuse (human.*, judge.*), when available
        -> cases (no candidate text: the reply, the decision, what was missing)
        -> one LLM "reflector" call proposes at most 5 small edits to the playbook
           (ADD / EDIT / RETIRE one lesson each, citing its cases)
        -> PROPOSED entries; an admin approves (ACTIVE) or rejects them
        -> ACTIVE entries reach the engine prompts (core/playbook.py), cached 60 s
        -> every traced turn records the playbook version, so Langfuse compares
           before/after, and scripts/langfuse_experiment.py --playbook candidate
           runs the fixed test set with the proposals before anyone approves.

Counters: an entry's ``helpful`` / ``harmful`` are the reviewed turns of the last
``COUNT_DAYS`` days that ran with it and passed / failed (flags or a failed verdict).
Every reflection recomputes them from scratch, so running it twice never counts a
turn twice. They are coarse (a turn has several entries) and only steer which
lessons to review first.

Safety: the reflector never sees what a candidate said, only what the
interviewer did and why; nothing it writes is used before a person approves it;
lessons cannot override the fixed rules (the block sits below them).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import llm_tracing
from src.core.trace_logging import trace_event
from src.infrastructure.database import SessionFactory
from src.modules.ai.facade import generate_text
from src.modules.interviews.core import playbook
from src.modules.interviews.planning.question_generation import is_near_duplicate

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 60.0
MAX_CASES = 30
MAX_EVENTS = 1000
COUNT_DAYS = 30
MAX_SCORE_PAGES = 10
# What llm_tracing.mask puts in place of personal data; the interviewer said the real value.
MASK_MARKERS = ("[tên]", "[email]", "[sđt]")
MAX_OPS = 5
MAX_ACTIVE = 40
REVIEW_SCORES = ("human.decision_ok", "human.reply_in_scope", "human.reply_neutral", "human.probe_ok")
JUDGE_SCORES = ("judge.decision_ok", "judge.reply_in_scope", "judge.reply_neutral", "judge.probe_ok")
STEP_OF_ACTION = {"PROBE": "probe", "CLARIFY": "clarify"}

_loaded_at = 0.0
_refresh_lock = asyncio.Lock()
_run: dict[str, Any] = {"running": False}


class PlaybookError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


# --------------------------------------------------------------------------- runtime cache


async def refresh(db: AsyncSession | None = None, *, force: bool = False) -> None:
    """Load ACTIVE entries into core.playbook at most every REFRESH_SECONDS; never fails a turn.

    Reads on its own short session, never on the caller's: a failed read
    (table not migrated) must not roll back the turn being processed.
    ``db`` is accepted for call-site symmetry and ignored.
    """
    global _loaded_at
    if not force and time.monotonic() - _loaded_at < REFRESH_SECONDS:
        return
    async with _refresh_lock:
        if not force and time.monotonic() - _loaded_at < REFRESH_SECONDS:
            return
        try:
            async with SessionFactory() as own:
                rows = (
                    (
                        await own.execute(
                            text(
                                "SELECT id, step, skill, text FROM interviewer_playbook "
                                "WHERE status = 'ACTIVE' "
                                "ORDER BY (helpful - harmful) DESC, created_at LIMIT :n"
                            ),
                            {"n": MAX_ACTIVE},
                        )
                    )
                    .mappings()
                    .all()
                )
            playbook.load(dict(r) for r in rows)
        except Exception:  # noqa: BLE001 - table not migrated yet, DB down: run without a playbook
            playbook.load([])
        _loaded_at = time.monotonic()


# --------------------------------------------------------------------------- cases


def case_from_event(row: dict[str, Any]) -> dict[str, Any]:
    """What the interviewer did on one turn, without the candidate's words."""
    p = row.get("payload") or {}
    return {
        "case": f"{str(row['session_id'])[:8]}#{row.get('turn_index')}",
        "sessionId": str(row["session_id"]),
        "turnIndex": row.get("turn_index"),
        "traceId": p.get("langfuse_trace_id"),
        "stage": p.get("stage"),
        "action": p.get("action"),
        "decisionRule": p.get("decision_rule"),
        "intent": p.get("intent"),
        "sufficiency": p.get("sufficiency"),
        "missingAspect": (p.get("missing_aspect") or "")[:200] or None,
        "interviewer": llm_tracing.mask(data=str(p.get("interviewer") or "")[:300]),
        "answerChars": p.get("answer_chars"),
        "flags": list(p.get("flags") or []),
        "playbookIds": list(p.get("playbook_ids") or []),
        "at": row.get("created_at"),
        "next": None,
        "verdicts": {},
    }


def _order_key(case: dict[str, Any]) -> tuple:
    at = case.get("at")
    return (
        case.get("sessionId") or "",
        at.timestamp() if isinstance(at, datetime) else 0.0,
        case.get("turnIndex") or 0,
    )


def link_followups(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each turn what the interviewer did on the next turn of the same interview.

    A turn judged "not enough" is followed up on the next turn (a probe), so a case
    seen alone looks like a missed follow-up; ``next`` lets the reflector check.
    """
    ordered = sorted(cases, key=_order_key)
    for case, after in zip(ordered, [*ordered[1:], None], strict=True):
        same = after is not None and after.get("sessionId") == case.get("sessionId")
        case["next"] = {"action": after.get("action"), "intent": after.get("intent")} if same else None
    return cases


def verdict_of(case: dict[str, Any]) -> str | None:
    """good / bad / None: flags or a failed review make a turn bad; a passed review makes it good."""
    values = list(case["verdicts"].values())
    if case["flags"] or any(v is False for v in values):
        return "bad"
    if values and all(values):
        return "good"
    return None


def _langfuse_verdicts(trace_ids: list[str], since: datetime) -> dict[str, dict[str, bool]]:
    lf = llm_tracing.client() if llm_tracing.enabled() else None
    if lf is None or not trace_ids:
        return {}
    wanted = set(trace_ids)
    found: dict[str, dict[str, bool]] = {}
    for name in (*REVIEW_SCORES, *JUDGE_SCORES):
        cursor = None
        for _ in range(MAX_SCORE_PAGES):
            try:
                page = lf.api.scores.get_many(
                    name=name, from_timestamp=since, fields="subject", limit=100, cursor=cursor
                )
            except Exception:  # noqa: BLE001
                break
            for score in page.data:
                subject = getattr(score, "subject", None)
                trace_id = getattr(subject, "trace_id", None) or getattr(subject, "id", None)
                if trace_id in wanted:
                    value = getattr(score, "value", None)
                    found.setdefault(trace_id, {})[name] = value in (1, 1.0, True, "1", "true")
            cursor = getattr(getattr(page, "meta", None), "cursor", None)
            if not cursor:
                break
    return found


async def load_cases(db: AsyncSession, *, days: int) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        (
            await db.execute(
                text(
                    "SELECT session_id, turn_index, payload, created_at FROM interview_pipeline_events "
                    "WHERE event = 'turn_spoken' AND created_at >= :since ORDER BY created_at DESC LIMIT :n"
                ),
                {"since": since, "n": MAX_EVENTS},
            )
        )
        .mappings()
        .all()
    )
    cases = link_followups([case_from_event(dict(r)) for r in rows])
    verdicts = await asyncio.to_thread(
        _langfuse_verdicts, [c["traceId"] for c in cases if c["traceId"]], since
    )
    for case in cases:
        case["verdicts"] = verdicts.get(case["traceId"] or "", {})
    return cases


def in_window(cases: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    return [c for c in cases if not isinstance(c.get("at"), datetime) or c["at"] >= since]


def pick_cases(cases: list[dict[str, Any]], limit: int = MAX_CASES) -> list[dict[str, Any]]:
    """Bad turns first (most to learn from), then a few good ones as contrast."""
    bad = [c for c in cases if verdict_of(c) == "bad"]
    good = [c for c in cases if verdict_of(c) == "good"]
    keep_good = min(len(good), max(limit // 4, limit - len(bad)))
    return (bad[: limit - keep_good] + good[:keep_good])[:limit]


def count_outcomes(cases: list[dict[str, Any]]) -> dict[str, Counter]:
    """helpful / harmful per playbook entry, from the reviewed turns that ran with it."""
    helpful: Counter = Counter()
    harmful: Counter = Counter()
    for case in cases:
        outcome = verdict_of(case)
        for entry_id in case["playbookIds"]:
            if outcome == "good":
                helpful[entry_id] += 1
            elif outcome == "bad":
                harmful[entry_id] += 1
    return {"helpful": helpful, "harmful": harmful}


# --------------------------------------------------------------------------- reflection


_REFLECTOR = """You improve the playbook of an AI technical interviewer (Vietnamese and English interviews).
The playbook is a short list of lessons the interviewer reads before it judges an answer
(step evaluate_answer), writes a follow-up question (probe) or re-explains a question (clarify).

You get the current lessons (with ids) and recent turns: what the interviewer said and decided,
rule flags (problems found automatically) and reviewer verdicts. Candidates' words are not included.
Turns are grouped by interview, in order; "next" is what the interviewer did on the following
turn of the same interview (null: the interview ended or that turn is not shown). A turn judged
not sufficient is followed up on the next turn, so check "next" before saying a follow-up was missed.
Personal data was replaced before you see it: [tên] is a name, [email] an email, [sđt] a phone
number. The interviewer said the real value; these markers are never a problem, never cite them.

Propose at most 5 small edits that would have prevented the problems, as JSON only.
Every edit cites at least one case id from the input whose own fields show the problem:
{"ops": [{"op": "ADD" | "EDIT" | "RETIRE", "id": "<lesson id, for EDIT/RETIRE>",
          "step": "evaluate_answer" | "probe" | "clarify" | "all",
          "text": "<the lesson, one or two sentences, for ADD/EDIT>",
          "why": "<one sentence>", "cases": ["<case ids>"]}]}
Rules for lessons:
- Concrete and checkable ("Ask for one specific metric before asking about the team"),
  not vague ("be better").
- Never about a specific candidate, company or person; no names; no answers to interview questions.
- Must not contradict: neutral acknowledgements, never reveal the answer or the rubric,
  one idea per follow-up,
  stay within the interview, keep the candidate's language.
- Write the lesson in Vietnamese.
- Prefer EDIT of an existing lesson over ADD of a near-duplicate.
  RETIRE a lesson only when cases show it causes problems.
- If nothing useful can be learned, return {"ops": []}."""


def parse_ops(raw: str, active_ids: set[str]) -> list[dict[str, Any]]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(match.group(0) if match else raw)
    except (json.JSONDecodeError, TypeError):
        return []
    ops = []
    for item in (data.get("ops") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "").upper()
        step = str(item.get("step") or "all")
        step = step if step in (*playbook.STEPS, playbook.ALL_STEPS) else "all"
        body = str(item.get("text") or "").strip()[:400]
        target = str(item.get("id") or "") or None
        why = str(item.get("why") or "")
        if any(marker in body or marker in why for marker in MASK_MARKERS):
            continue  # a lesson about the masking markers misreads the system, not the interviewer
        if op == "ADD" and body:
            ops.append(
                {
                    "op": "ADD",
                    "step": step,
                    "text": body,
                    "why": str(item.get("why") or "")[:300],
                    "cases": item.get("cases") or [],
                }
            )
        elif op == "EDIT" and body and target in active_ids:
            ops.append(
                {
                    "op": "EDIT",
                    "id": target,
                    "step": step,
                    "text": body,
                    "why": str(item.get("why") or "")[:300],
                    "cases": item.get("cases") or [],
                }
            )
        elif op == "RETIRE" and target in active_ids:
            ops.append(
                {
                    "op": "RETIRE",
                    "id": target,
                    "why": str(item.get("why") or "")[:300],
                    "cases": item.get("cases") or [],
                }
            )
    return ops[:MAX_OPS]


async def reflect(cases: list[dict[str, Any]], active: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = await generate_text(
        instructions=_REFLECTOR,
        input_text=json.dumps(
            {
                "lessons": [{"id": e["id"], "step": e["step"], "text": e["text"]} for e in active],
                "turns": [
                    {
                        k: v
                        for k, v in c.items()
                        if k not in {"traceId", "playbookIds", "sessionId", "turnIndex", "at"}
                    }
                    for c in sorted(cases, key=_order_key)
                ],
            },
            ensure_ascii=False,
        ),
        max_output_tokens=1500,
        temperature=0.2,
        purpose="interviews.playbook.reflect",
    )
    return parse_ops(raw, {e["id"] for e in active})


async def _all_entries(db: AsyncSession, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = await db.execute(
        text("SELECT * FROM interviewer_playbook WHERE status = ANY(:s) ORDER BY created_at"),
        {"s": list(statuses)},
    )
    return [dict(r) for r in rows.mappings().all()]


async def store_ops(
    db: AsyncSession, ops: list[dict[str, Any]], *, run_id: str, cases: list[dict[str, Any]]
) -> int:
    known = await _all_entries(db, ("ACTIVE", "PROPOSED"))
    by_case = {c["case"]: c for c in cases}
    stored = 0
    for op in ops:
        if op["op"] in {"ADD", "EDIT"} and is_near_duplicate(op["text"], [e["text"] for e in known]):
            continue
        evidence = [
            {
                "case": cid,
                "sessionId": by_case[cid].get("sessionId"),
                "turnIndex": by_case[cid].get("turnIndex"),
                "traceId": by_case[cid].get("traceId"),
                "flags": by_case[cid].get("flags"),
            }
            for cid in dict.fromkeys(str(c) for c in op.get("cases") or [])
            if cid in by_case
        ]
        if not evidence:
            continue  # nothing an admin could check the lesson against
        if op["op"] == "RETIRE":
            target = next((e for e in known if e["id"] == op["id"]), None)
            body, step = (target["text"], target["step"]) if target else ("", "all")
        else:
            body, step = op["text"], op["step"]
        entry = {
            "id": str(uuid4()),
            "step": step,
            "text": body,
            "replaces_id": op.get("id"),
            "source": json.dumps(
                {"op": op["op"], "why": op.get("why"), "cases": evidence, "run": run_id}, ensure_ascii=False
            ),
        }
        await db.execute(
            text(
                "INSERT INTO interviewer_playbook (id, step, text, status, origin, replaces_id, source) "
                "VALUES (:id, :step, :text, 'PROPOSED', 'reflection', :replaces_id, CAST(:source AS jsonb))"
            ),
            entry,
        )
        known.append({"id": entry["id"], "text": body})
        stored += 1
    return stored


async def _apply_counts(db: AsyncSession, counts: dict[str, Counter]) -> None:
    """Set (not add) the counters, so overlapping reflection windows never count a turn twice."""
    await db.execute(
        text("UPDATE interviewer_playbook SET helpful = 0, harmful = 0 WHERE helpful <> 0 OR harmful <> 0")
    )
    for column in ("helpful", "harmful"):
        for entry_id, n in counts[column].items():
            await db.execute(
                text(f"UPDATE interviewer_playbook SET {column} = :n, updated_at = now() WHERE id = :id"),
                {"n": n, "id": entry_id},
            )


async def _reflect_job(run_id: str, days: int) -> None:
    try:
        async with SessionFactory() as db:
            counted = await load_cases(db, days=max(days, COUNT_DAYS))
            cases = in_window(counted, days)
            picked = pick_cases(cases)
            active = await _all_entries(db, ("ACTIVE",))
            ops = await reflect(picked, active) if picked else []
            stored = await store_ops(db, ops, run_id=run_id, cases=picked)
            await _apply_counts(db, count_outcomes(counted))
            await db.commit()
        verdicts = Counter(verdict_of(c) or "unreviewed" for c in cases)
        _run.update(
            {
                "cases": len(cases),
                "used": len(picked),
                "proposed": stored,
                "verdicts": dict(verdicts),
                "error": None,
            }
        )
        trace_event("interviewer", "playbook_reflected", run_id=run_id, cases=len(cases), proposed=stored)
    except Exception as exc:  # noqa: BLE001 - shown on the page
        logger.exception("Playbook reflection failed")
        _run["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        _run["running"] = False
        _run["finishedAt"] = datetime.now(UTC).isoformat()


def start_reflection(*, days: int = 14) -> dict[str, Any]:
    if _run.get("running"):
        raise PlaybookError(409, "reflection_running")
    run_id = str(uuid4())
    _run.clear()
    _run.update({"running": True, "runId": run_id, "days": days, "startedAt": datetime.now(UTC).isoformat()})
    asyncio.create_task(_reflect_job(run_id, days), name=f"playbook-reflect-{run_id}")
    return dict(_run)


# --------------------------------------------------------------------------- admin view and decisions


def entry_view(
    row: dict[str, Any], *, actor_id: str | None = None, names: dict[str, str] | None = None
) -> dict[str, Any]:
    """One lesson for the admin page: its status, who wrote and who decided it.

    ``mine`` is true for a lesson the viewing admin wrote by hand: they cannot
    approve it themselves (four-eyes rule, same as the question bank).
    """
    names = names or {}
    created_by = str(row["created_by"]) if row.get("created_by") else None
    decided_by = str(row["decided_by"]) if row.get("decided_by") else None
    return {
        "id": row["id"],
        "step": row["step"],
        "skill": row.get("skill"),
        "text": row["text"],
        "status": row["status"],
        "origin": row["origin"],
        "replacesId": row.get("replaces_id"),
        "helpful": row.get("helpful") or 0,
        "harmful": row.get("harmful") or 0,
        "source": row.get("source") or {},
        "note": row.get("note"),
        "decidedAt": row["decided_at"].isoformat() if row.get("decided_at") else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "createdBy": names.get(created_by, created_by) if created_by else None,
        "decidedBy": names.get(decided_by, decided_by) if decided_by else None,
        "mine": bool(actor_id) and created_by == str(actor_id),
    }


async def _names(db: AsyncSession, ids: set[str]) -> dict[str, str]:
    """Display names of the admins who wrote or decided lessons; ids stay as-is when unknown."""
    if not ids:
        return {}
    try:
        rows = await db.execute(
            text(
                "SELECT CAST(id AS TEXT) AS id, COALESCE(name, email) AS name FROM users "
                "WHERE CAST(id AS TEXT) = ANY(:ids)"
            ),
            {"ids": sorted(ids)},
        )
        return {str(r["id"]): str(r["name"]) for r in rows.mappings().all() if r["name"]}
    except Exception:  # noqa: BLE001 - names are a nicety; the page still loads
        await db.rollback()
        return {}


async def view(db: AsyncSession, *, actor_id: str | None = None) -> dict[str, Any]:
    try:
        entries = await _all_entries(db, ("ACTIVE", "PROPOSED", "RETIRED", "REJECTED"))
    except Exception:  # noqa: BLE001 - migration 0037 not applied yet
        await db.rollback()
        return {"entries": [], "version": "", "run": dict(_run), "needsMigration": True}
    await refresh(db, force=True)
    people = {str(e[k]) for e in entries for k in ("created_by", "decided_by") if e.get(k)}
    names = await _names(db, people)
    return {
        "entries": [entry_view(e, actor_id=actor_id, names=names) for e in entries],
        "version": playbook.version(),
        "run": dict(_run),
        "needsMigration": False,
    }


async def add_manual(
    db: AsyncSession, *, step: str, text_value: str, skill: str | None, actor_id: str
) -> str:
    if step not in (*playbook.STEPS, playbook.ALL_STEPS) or not text_value.strip():
        raise PlaybookError(422, "invalid_entry")
    entry_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO interviewer_playbook (id, step, skill, text, status, origin, created_by, source) "
            "VALUES (:id, :step, :skill, :text, 'PROPOSED', 'manual', :by, '{}'::jsonb)"
        ),
        {
            "id": entry_id,
            "step": step,
            "skill": (skill or None),
            "text": text_value.strip()[:400],
            "by": actor_id,
        },
    )
    await db.commit()
    return entry_id


async def decide(
    db: AsyncSession, entry_id: str, *, decision: str, actor_id: str, note: str | None, text_value: str | None
) -> dict[str, Any]:
    row = (
        (await db.execute(text("SELECT * FROM interviewer_playbook WHERE id = :id"), {"id": entry_id}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise PlaybookError(404, "entry_not_found")
    row = dict(row)
    source = row.get("source") or {}
    if decision == "APPROVE":
        if row["status"] != "PROPOSED":
            raise PlaybookError(409, "not_proposed")
        if row.get("created_by") and str(row["created_by"]) == str(actor_id):
            # Four-eyes rule: a hand-written lesson needs another admin.
            raise PlaybookError(403, "own_entry")
        if source.get("op") == "RETIRE" and row.get("replaces_id"):
            # Approving a RETIRE proposal retires its target; the proposal itself is closed.
            await _set(db, row["replaces_id"], "RETIRED", actor_id, note)
            await _set(db, entry_id, "REJECTED", actor_id, "retire_applied")
        else:
            if text_value and text_value.strip():
                await db.execute(
                    text("UPDATE interviewer_playbook SET text = :t WHERE id = :id"),
                    {"t": text_value.strip()[:400], "id": entry_id},
                )
            await _set(db, entry_id, "ACTIVE", actor_id, note)
            if row.get("replaces_id"):
                await _set(db, row["replaces_id"], "RETIRED", actor_id, "replaced")
    elif decision == "REJECT":
        if row["status"] != "PROPOSED":
            raise PlaybookError(409, "not_proposed")
        await _set(db, entry_id, "REJECTED", actor_id, note)
    elif decision == "RETIRE":
        if row["status"] != "ACTIVE":
            raise PlaybookError(409, "not_active")
        await _set(db, entry_id, "RETIRED", actor_id, note)
    else:
        raise PlaybookError(422, "invalid_decision")
    await db.commit()
    await refresh(db, force=True)
    return {"version": playbook.version()}


async def _set(db: AsyncSession, entry_id: str, status: str, actor_id: str, note: str | None) -> None:
    await db.execute(
        text(
            "UPDATE interviewer_playbook SET status = :s, decided_by = :by, decided_at = now(), "
            "note = COALESCE(:note, note), updated_at = now() WHERE id = :id"
        ),
        {"s": status, "by": actor_id[:36], "note": note, "id": entry_id},
    )
