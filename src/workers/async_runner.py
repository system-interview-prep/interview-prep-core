import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

ResultT = TypeVar("ResultT")


class WorkerEventLoopRunner:
    """Own one asyncio loop for the lifetime of a Celery child process.

    Async database drivers keep connections bound to their creating loop.
    Calling ``asyncio.run`` once per task closes that loop, which breaks a
    later Celery retry when its pooled connection is reused.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self, coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coroutine)

    def close(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
