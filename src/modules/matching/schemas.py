"""Public matching contracts for other feature modules.

Sibling modules must not reach into ``matching.domain`` directly; the module
boundary contract in ``tests/app/test_architecture.py`` allows only ``schemas``,
``facade`` and ``events`` at the module root. Everything re-exported here is a
deliberate cross-module contract, not an accident of what happens to be
importable.
"""

from src.modules.matching.domain.schemas import (
    CanonicalJob,
    LanguageRequirement,
    MatchRequest,
    MatchResult,
    SkillRequirement,
    UnresolvedRequirement,
)

__all__ = [
    "CanonicalJob",
    "LanguageRequirement",
    "MatchRequest",
    "MatchResult",
    "SkillRequirement",
    "UnresolvedRequirement",
]
