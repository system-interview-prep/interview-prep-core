import asyncio

from src.workers.async_runner import WorkerEventLoopRunner


def test_worker_event_loop_runner_reuses_one_loop_for_sequential_tasks() -> None:
    runner = WorkerEventLoopRunner()

    async def loop_id() -> int:
        return id(asyncio.get_running_loop())

    try:
        assert runner.run(loop_id()) == runner.run(loop_id())
    finally:
        runner.close()
