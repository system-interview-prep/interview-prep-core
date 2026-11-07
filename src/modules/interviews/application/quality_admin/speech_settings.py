"""Read-only voice settings shown in interviewer quality tools.

The standalone LiveKit process reads its own adapter configuration; this helper
keeps the API/application layer independent from transport implementation code.
"""

from __future__ import annotations

import os


def voice_dialect() -> str:
    value = os.getenv("INTERVIEWER_VOICE_DIALECT", "bac").strip().casefold()
    return "nam" if value in {"nam", "south", "southern", "mien nam", "mi?n nam"} else "bac"
