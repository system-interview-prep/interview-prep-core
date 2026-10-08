"""Operations supplied by the application layer to the single agent graph."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from src.modules.interviews.agent.state import InterviewCommand

InterviewOperation = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]
InterviewOperations = Mapping[InterviewCommand, InterviewOperation]
