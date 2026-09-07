from src.workers.tasks.cv import parse_cv
from src.workers.tasks.job_description import parse_job_description
from src.workers.tasks.matching import match_cv_to_jd


def test_worker_task_names_are_stable() -> None:
    assert parse_cv.name == "cv.parse"
    assert parse_job_description.name == "job_description.parse"
    assert match_cv_to_jd.name == "matching.match_cv_to_jd"
