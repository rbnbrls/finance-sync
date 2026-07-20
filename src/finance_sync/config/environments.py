"""Environment enumeration for environment-aware configuration."""

from __future__ import annotations

import enum


class Environment(enum.StrEnum):
    """Deployment environment."""

    DEVELOPMENT = "dev"
    STAGING = "staging"
    PRODUCTION = "prod"

    @property
    def is_debug(self) -> bool:
        """Whether debug features should be enabled."""
        return self in (Environment.DEVELOPMENT, Environment.STAGING)

    @property
    def is_production(self) -> bool:
        """Whether this is a production environment."""
        return self == Environment.PRODUCTION

    @classmethod
    def from_str(cls, value: str) -> Environment:
        """Parse an environment string, case-insensitively.

        Accepts 'dev', 'staging', 'prod' plus friendly names like
        'development', 'production'.
        """
        normalized = value.strip().lower().replace(" ", "-")
        mapping: dict[str, Environment] = {
            "dev": cls.DEVELOPMENT,
            "development": cls.DEVELOPMENT,
            "staging": cls.STAGING,
            "prod": cls.PRODUCTION,
            "production": cls.PRODUCTION,
        }
        if normalized not in mapping:
            choices = [e.value for e in cls]
            msg = f"Unknown environment: {value!r}. Choose from {choices}"
            raise ValueError(msg)
        return mapping[normalized]
