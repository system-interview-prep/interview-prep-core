"""What is removed from a payload before it leaves this process, and how much is sent.

Masking runs on every input, output and metadata value: the Langfuse client is
built with ``mask=mask``. Sampling decides whether a session's prompts and
answers are sent as text at all.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from typing import Any

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Vietnamese mobile and landline numbers, with or without +84 and separators.
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)(?:[\s.-]?\d){8,10}(?!\d)")
# Names of candidates in recent turns (full names and call names), masked in every payload.
_NAMES: deque[str] = deque(maxlen=200)


def register_names(*names: str | None) -> None:
    for name in names:
        value = " ".join(str(name or "").split())
        if len(value) >= 2 and value not in _NAMES:
            _NAMES.append(value)


def _mask_text(value: str) -> str:
    value = _EMAIL_RE.sub("[email]", value)
    value = _PHONE_RE.sub("[sđt]", value)
    for name in sorted(_NAMES, key=len, reverse=True):
        value = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[tên]", value)
    return value


def mask(*, data: Any, **_: Any) -> Any:
    """Langfuse mask hook: applied to every input, output and metadata value."""
    if isinstance(data, str):
        return _mask_text(data)
    if isinstance(data, dict):
        return {key: mask(data=value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=value) for value in data]
    return data


def share(seed: str) -> float:
    """A stable number in [0, 1) for a seed: the same seed always samples the same way."""
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def text_sampled(session_id: str) -> bool:
    """Whether this session's prompts and answers are sent as text (stable per session)."""
    from src.core.config import get_settings

    return share(f"text:{session_id}") < get_settings().langfuse_text_sample_rate


def hashed_user(user_id: str | None) -> str | None:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16] if user_id else None
