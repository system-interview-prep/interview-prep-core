from types import SimpleNamespace

import pytest

from src.infrastructure import r2


def _settings(**overrides):
    values = {
        "r2_endpoint_url": "https://account.r2.cloudflarestorage.com",
        "r2_bucket_name": "interview-prep",
        "r2_access_key_id": "access-key",
        "r2_secret_access_key": "secret-key",
        "r2_public_domain": "https://files.example.com",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_r2_client_uses_cloudflare_s3_compatibility_settings(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(r2, "get_settings", _settings)
    monkeypatch.setattr(r2.boto3, "client", lambda **kwargs: captured.update(kwargs) or object())

    r2.r2_client()

    assert captured == {
        "service_name": "s3",
        "endpoint_url": "https://account.r2.cloudflarestorage.com",
        "aws_access_key_id": "access-key",
        "aws_secret_access_key": "secret-key",
        "region_name": "auto",
    }


def test_r2_public_url_uses_configured_domain(monkeypatch) -> None:
    monkeypatch.setattr(r2, "get_settings", _settings)

    assert r2.public_url("cvs/user id/cv.pdf") == "https://files.example.com/cvs/user%20id/cv.pdf"


def test_r2_requires_all_connection_settings(monkeypatch) -> None:
    monkeypatch.setattr(r2, "get_settings", lambda: _settings(r2_bucket_name=None))

    with pytest.raises(RuntimeError, match="R2_BUCKET_NAME"):
        r2.r2_client()
