"""The Langfuse client, and whether there is one at all.

Everything downstream treats ``None`` as "tracing is off": without keys, or
without the ``langfuse`` package, every caller becomes a cheap no-op and an
exporter error never reaches the interview.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from src.infrastructure.langfuse.privacy import mask

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_client: Any = None
_client_ready = False


def enabled() -> bool:
    from src.core.config import get_settings

    settings = get_settings()
    if settings.langfuse_enabled is False:
        return False
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def client() -> Any:
    """The Langfuse client, or None when tracing is off or the package is missing."""
    global _client, _client_ready
    if _client_ready:
        return _client
    with _lock:
        if _client_ready:
            return _client
        _client_ready = True
        if not enabled():
            return None
        try:
            from langfuse import Langfuse

            from src.core.config import get_settings

            settings = get_settings()
            _client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                base_url=settings.langfuse_base_url,
                environment=(settings.langfuse_environment or settings.app_env or "development").casefold()[
                    :40
                ],
                mask=mask,
            )
        except Exception:  # noqa: BLE001 - tracing is optional
            logger.warning("Langfuse tracing disabled: client could not start", exc_info=True)
            _client = None
        return _client


def use_client(langfuse_client: Any) -> None:
    """Install a client (tests: a Langfuse instance with an in-memory span exporter)."""
    global _client, _client_ready
    with _lock:
        _client, _client_ready = langfuse_client, True


def flush() -> None:
    lf = _client
    if lf is not None:
        try:
            lf.flush()
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse flush failed", exc_info=True)


def project_url() -> str | None:
    """``<Langfuse UI>/project/<id>`` for links on the admin pages; None when off or unreachable."""
    lf = client()
    if lf is None:
        return None
    try:
        from src.core.config import get_settings

        project_id = lf._get_project_id()  # noqa: SLF001 - cached by the SDK after the first call
        if not project_id:
            return None
        settings = get_settings()
        base = (settings.langfuse_ui_url or settings.langfuse_base_url).rstrip("/")
        return f"{base}/project/{project_id}"
    except Exception:  # noqa: BLE001
        logger.debug("Langfuse project not reachable", exc_info=True)
        return None
