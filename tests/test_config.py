"""Tests for the config module."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from finance_sync.config import Environment, Settings


class TestEnvironment:
    """Environment enum behaviour."""

    def test_values(self) -> None:
        assert Environment.DEVELOPMENT.value == "dev"
        assert Environment.STAGING.value == "staging"
        assert Environment.PRODUCTION.value == "prod"

    def test_is_debug(self) -> None:
        assert Environment.DEVELOPMENT.is_debug is True
        assert Environment.STAGING.is_debug is True
        assert Environment.PRODUCTION.is_debug is False

    def test_is_production(self) -> None:
        assert Environment.DEVELOPMENT.is_production is False
        assert Environment.PRODUCTION.is_production is True

    @pytest.mark.parametrize(
        ("input_str", "expected"),
        [
            ("dev", Environment.DEVELOPMENT),
            ("development", Environment.DEVELOPMENT),
            ("staging", Environment.STAGING),
            ("prod", Environment.PRODUCTION),
            ("production", Environment.PRODUCTION),
        ],
    )
    def test_from_str_valid(
        self, input_str: str, expected: Environment
    ) -> None:
        assert Environment.from_str(input_str) is expected

    def test_from_str_invalid(self) -> None:
        with pytest.raises(ValueError, match="Unknown environment"):
            Environment.from_str("invalid")


class TestSettings:
    """Settings loading from env and defaults."""

    def test_defaults(self) -> None:
        """Without env vars, defaults are used."""
        settings = Settings(_env_file=None)
        assert settings.app_name == "finance-sync"
        assert settings.app_version == "0.1.0"
        assert settings.debug is False
        assert settings.environment == Environment.DEVELOPMENT
        assert settings.cors_origins == ["*"]
        assert settings.database_url is None
        assert settings.redis_url is None
        assert isinstance(settings.secret_key, SecretStr)
        assert settings.access_token_expire_minutes == 30

    def test_is_debug_property(self) -> None:
        """is_debug is True when environment permits it (dev/staging)."""
        settings = Settings(_env_file=None)
        assert settings.is_debug is True  # dev is debug

        settings_prod = Settings(environment="prod", _env_file=None)  # type: ignore[call-arg]
        assert settings_prod.is_debug is False

    def test_debug_flag_overrides(self) -> None:
        """Explicit debug flag overrides environment-based debug."""
        settings = Settings(debug=True, environment="prod")  # type: ignore[call-arg]
        assert settings.is_debug is True

    def test_is_production_property(self) -> None:
        settings_dev = Settings()
        assert settings_dev.is_production is False

        settings_prod = Settings(environment="prod")  # type: ignore[call-arg]
        assert settings_prod.is_production is True

    def test_database_url(self) -> None:
        url = "postgresql+asyncpg://user:pass@localhost:5432/db"
        settings = Settings(database_url=url)  # type: ignore[call-arg]
        assert settings.database_url is not None
        assert "localhost" in settings.database_url.unicode_string()

    def test_redis_url(self) -> None:
        url = "redis://localhost:6379/0"
        settings = Settings(redis_url=url)  # type: ignore[call-arg]
        assert settings.redis_url is not None
        assert "localhost" in settings.redis_url.unicode_string()

    def test_secret_key_validation(self) -> None:
        """Short secret keys raise a validation error."""
        with pytest.raises(
            ValueError, match="Secret key must be at least 16 characters"
        ):
            Settings(secret_key="short")  # type: ignore[call-arg]

    def test_long_secret_key_accepted(self) -> None:
        """Keys >= 16 chars are accepted."""
        settings = Settings(secret_key="this-is-32-chars-key-ok!!")  # type: ignore[call-arg]
        assert settings.secret_key is not None
