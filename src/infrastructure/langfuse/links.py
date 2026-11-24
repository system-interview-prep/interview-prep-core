"""Deep links into the Langfuse UI, for the admin pages."""

from __future__ import annotations

import asyncio

from src.infrastructure.langfuse import connection as lf_client


async def session_links(session_id: str | None = None) -> dict[str, str] | None:
    """Links to this session (and its turn traces) in Langfuse; None when Langfuse is off."""
    if not lf_client.enabled():
        return None
    project = await asyncio.to_thread(lf_client.project_url)
    if not project:
        return None
    links = {"projectUrl": project}
    if session_id:
        links["sessionUrl"] = f"{project}/sessions/{session_id}"
    return links
