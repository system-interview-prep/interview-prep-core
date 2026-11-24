"""Application services exposed by the matching module."""

from typing import Any

__all__ = [
    "MatchingFacade",
    "canonical_job_from_description",
    "get_matching_facade",
    "resolve_job",
    "resolve_resume",
    "run_match",
]


def __getattr__(name: str) -> Any:
    """Load application components lazily to keep package imports acyclic."""
    if name in {"MatchingFacade", "canonical_job_from_description", "get_matching_facade"}:
        from src.modules.matching.application import facade

        return getattr(facade, name)
    if name in {"resolve_job", "resolve_resume"}:
        from src.modules.matching.application import input_resolver

        return getattr(input_resolver, name)
    if name == "run_match":
        from src.modules.matching.application.service import run_match

        return run_match
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
