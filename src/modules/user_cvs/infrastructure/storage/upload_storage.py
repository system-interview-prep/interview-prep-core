from src.infrastructure.r2 import delete_object, public_url, put_object


class R2CvFileStorage:
    def write(self, key: str, content: bytes, content_type: str) -> None:
        put_object(key, content, content_type)

    def delete(self, key: str) -> None:
        delete_object(key)

    def url(self, key: str, cv_id: str) -> str:
        return public_url(key) or f"/users/me/cvs/{cv_id}/download"
