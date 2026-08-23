"""Configuration for the Ghostfolio destination."""

from __future__ import annotations

from pydantic import BaseModel, Field

from finance_sync.config.settings import secret_value


class GhostfolioConfig(BaseModel):
    """Connection settings for a self-hosted Ghostfolio instance."""

    server_url: str = Field(default="http://localhost:3333")
    access_token: str
    request_timeout: float = Field(default=60.0, gt=0)
    verify_ssl: bool = True
    data_source: str = "YAHOO"
    include_pending: bool = False

    model_config = {"extra": "forbid"}

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if not self.server_url:
            message = "server_url must be non-empty"
            raise ValueError(message)
        if not self.access_token:
            message = "access_token must be non-empty"
            raise ValueError(message)

    @classmethod
    def from_settings(cls, settings: object) -> GhostfolioConfig:
        return cls(
            server_url=getattr(
                settings, "ghostfolio_server_url", "http://localhost:3333"
            ),
            access_token=secret_value(
                getattr(settings, "ghostfolio_access_token", "")
            ),
            request_timeout=getattr(
                settings, "ghostfolio_request_timeout", 60.0
            ),
            verify_ssl=getattr(settings, "ghostfolio_verify_ssl", True),
            data_source=getattr(settings, "ghostfolio_data_source", "YAHOO"),
            include_pending=getattr(
                settings, "ghostfolio_include_pending", False
            ),
        )
