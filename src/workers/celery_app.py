from celery import Celery
from celery.signals import worker_process_init

from src.core.config import get_settings

settings = get_settings()
celery_app = Celery(
    "interview_prep",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "src.workers.tasks.matching",
        "src.workers.tasks.cv",
        "src.workers.tasks.job_description",
    ],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_routes={
        "matching.*": {"queue": settings.rabbitmq_matching_queue},
        "cv.*": {"queue": settings.rabbitmq_cv_queue},
        "job_description.*": {"queue": settings.rabbitmq_jd_queue},
    },
)

@worker_process_init.connect
def on_worker_process_init(**kwargs: object) -> None:
    from src.infrastructure.database import engine
    from src.workers.async_runner import worker_async_runner

    # The worker is forked from Celery's parent process.  Do not close inherited
    # asyncpg connections: they belong to the parent's event loop.  Replacing
    # the pool makes this child establish fresh connections on its own loop.
    engine.sync_engine.dispose(close=False)
    worker_async_runner.reset_after_fork()
