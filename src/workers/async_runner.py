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
            asyncio.set_event_loop(self._loop)
        else:
            try:
                if asyncio.get_event_loop() != self._loop:
                    asyncio.set_event_loop(self._loop)
            except RuntimeError:
                asyncio.set_event_loop(self._loop)
        return self._loop.run_until_complete(coroutine)

    def close(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
            self._loop = None

    def reset_after_fork(self) -> None:
        """Discard a loop inherited from Celery's prefork parent.

        A child must never run coroutines on a loop that existed in the parent
        process.  There is deliberately no ``close()`` here: the inherited
        loop may own resources whose counterpart lives only in the parent.
        """

        self._loop = None


# SQLAlchemy/asyncpg pools are bound to the loop that creates their
# connections.  All async Celery tasks in a child process must therefore use
# this one runner, rather than one runner per task module.
worker_async_runner = WorkerEventLoopRunner()
