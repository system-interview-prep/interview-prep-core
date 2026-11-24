class CeleryCvParsePublisher:
    def publish(self, cv_id: str) -> None:
        from src.workers.celery_app import celery_app

        celery_app.send_task("cv.parse", args=[{"cv_id": cv_id}])
