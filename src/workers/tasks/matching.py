from src.core.config import get_settings
from src.modules.matching.application import run_tracking
from src.modules.matching.application.facade import get_matching_facade
from src.modules.matching.domain.schemas import MatchRequest
from src.workers.async_runner import worker_async_runner
from src.workers.celery_app import celery_app


async def _match_in_run(payload: MatchRequest, run_info: dict) -> dict:
    """Continue the enqueuing request's run (same run id) as flow ``celery``."""
    with run_tracking.match_run(
        "celery",
        run_id=run_info.get("runId"),
        user_id=run_info.get("userId"),
        resume_id=run_info.get("resumeId") or payload.resume.resume_id,
        job_id=run_info.get("jobId") or payload.job.job_id,
    ):
        try:
            result = await get_matching_facade().match_async(payload)
        except Exception as exc:
            run_tracking.emit_match_failed(exc)
            raise
        run_tracking.emit_match_completed(result)
        return result.model_dump(by_alias=True)


@celery_app.task(
    name="matching.match_cv_to_jd",
    bind=True,
    autoretry_for=(TimeoutError, ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=get_settings().rabbitmq_max_attempts,
)
def match_cv_to_jd(self, raw_payload: dict) -> dict:
    raw_payload = dict(raw_payload)
    run_info = raw_payload.pop(run_tracking.CELERY_RUN_KEY, None) or {}
    payload = MatchRequest.model_validate(raw_payload)
    return worker_async_runner.run(_match_in_run(payload, run_info if isinstance(run_info, dict) else {}))
