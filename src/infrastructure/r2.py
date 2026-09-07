from urllib.parse import quote

import boto3

from src.core.config import get_settings


def _settings():
    settings = get_settings()
    if not all(
        (
            settings.r2_endpoint_url,
            settings.r2_bucket_name,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
        )
    ):
        raise RuntimeError(
            "R2_ENDPOINT_URL, R2_BUCKET_NAME, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY are required"
        )
    return settings


def r2_client():
    settings = _settings()
    return boto3.client(
        service_name="s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def put_object(key: str, content: bytes, content_type: str) -> None:
    settings = _settings()
    r2_client().put_object(Bucket=settings.r2_bucket_name, Key=key, Body=content, ContentType=content_type)


def get_object(key: str) -> bytes:
    settings = _settings()
    return r2_client().get_object(Bucket=settings.r2_bucket_name, Key=key)["Body"].read()


def delete_object(key: str) -> None:
    settings = _settings()
    r2_client().delete_object(Bucket=settings.r2_bucket_name, Key=key)


def public_url(key: str) -> str | None:
    domain = get_settings().r2_public_domain.rstrip("/")
    return f"{domain}/{quote(key)}" if domain else None
