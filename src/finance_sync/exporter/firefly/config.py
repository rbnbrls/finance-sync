"""Configuration for the Firefly III exporter."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FireflyConfig(BaseModel):
    """Connection and mapping policy for a Firefly III instance."""

    server_url: str = Field(default="http://localhost:8082")
    access_token: str = Field(default="")
    verify_ssl: bool = True
    request_timeout: float = Field(default=60.0, gt=0)
    default_currency: str = "EUR"
    import_tag: str = "finance-sync"
    account_name_overrides: dict[str, str] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @classmethod
    def from_settings(cls, settings: Any) -> FireflyConfig:
        token = getattr(settings, "firefly_access_token", None)
        if token is not None and hasattr(token, "get_secret_value"):
            token = token.get_secret_value()
        return cls(
            server_url=getattr(settings, "firefly_server_url", "")
            or "http://localhost:8082",
            access_token=str(token or ""),
            verify_ssl=getattr(settings, "firefly_verify_ssl", True),
            request_timeout=getattr(settings, "firefly_request_timeout", 60.0),
            default_currency=getattr(
                settings, "firefly_default_currency", "EUR"
            ),
            import_tag=getattr(settings, "firefly_import_tag", "finance-sync"),
            account_name_overrides=getattr(
                settings, "firefly_account_name_overrides", {}
            ),
        )
