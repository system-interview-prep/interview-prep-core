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


def test_worker_event_loop_runner_reset_after_fork_discards_inherited_loop() -> None:
    runner = WorkerEventLoopRunner()

    async def loop_id() -> int:
        return id(asyncio.get_running_loop())

    try:
        inherited_loop_id = runner.run(loop_id())
        runner.reset_after_fork()
        assert runner.run(loop_id()) != inherited_loop_id
    finally:
        runner.close()
