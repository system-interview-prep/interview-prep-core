from src.core.config import get_settings
from src.modules.matching.facade import get_matching_facade
from src.modules.matching.schemas import MatchRequest
from src.workers.async_runner import worker_async_runner
from src.workers.celery_app import celery_app


@celery_app.task(
    name="matching.match_cv_to_jd",
    bind=True,
    autoretry_for=(TimeoutError, ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=get_settings().rabbitmq_max_attempts,
)
def match_cv_to_jd(self, raw_payload: dict) -> dict:
    payload = MatchRequest.model_validate(raw_payload)
    return worker_async_runner.run(get_matching_facade().match_async(payload)).model_dump(by_alias=True)
