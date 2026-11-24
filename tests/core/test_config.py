import pytest

from src.core.config import Settings


def test_production_requires_strong_jwt_secret() -> None:
    settings = Settings(app_env="production", jwt_secret="short", _env_file=None)
    with pytest.raises(RuntimeError):
        settings.validate_production()


def test_cors_origins_support_comma_separated_env_value() -> None:
    settings = Settings(cors_origins="https://a.test, https://b.test", _env_file=None)
    assert settings.cors_origins == ["https://a.test", "https://b.test"]
