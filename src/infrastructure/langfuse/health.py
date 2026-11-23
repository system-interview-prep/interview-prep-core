"""Whether Langfuse answers, for the admin health page."""

from __future__ import annotations

import asyncio
from typing import Any

from src.infrastructure.langfuse import connection as lf_client


def _llm_connections() -> list[str] | None:
    """Providers of the project's LLM connections (what the AI judges call); None when unknown."""
    lf = lf_client.client()
    if lf is None:
        return None
    try:
        return [str(c.provider) for c in lf.api.llm_connections.list(limit=50).data]
    except Exception:  # noqa: BLE001 - older Langfuse or key without the scope
        return None


def langfuse_status(url: str | None, providers: list[str] | None) -> tuple[str, str | None]:
    """ok / down (unreachable).

    The project API lists only project-level LLM connections; an organization
    connection (Settings → LLM Connections → Organization connections) also
    serves the judges but is not visible here, so an empty list is not a
    problem to report. The page shows what the project list contains.
    """
    if not url:
        return "down", "unreachable"
    return "ok", None


async def langfuse_check(check: Any) -> dict[str, Any]:
    """``check`` is the page's row builder, passed in so this package does not
    import the admin module."""
    if not lf_client.enabled():
        return check("langfuse", "off", "not_configured")
    url = await asyncio.to_thread(lf_client.project_url)
    providers = await asyncio.to_thread(_llm_connections) if url else None
    status, detail = langfuse_status(url, providers)
    return check("langfuse", status, detail, projectUrl=url, llmConnections=providers)
