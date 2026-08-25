"""Configuration for the InvestBrain destination."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from finance_sync.config.settings import secret_value


class InvestBrainConfig(BaseModel):
    server_url: str = Field(default="http://localhost:8000")
    access_token: str
    request_timeout: float = Field(default=60.0, gt=0)
    verify_ssl: bool = True
    include_pending: bool = False
    portfolio_name_prefix: str = "finance-sync"

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
    def from_settings(cls, settings: Any) -> InvestBrainConfig:
        return cls(
            server_url=getattr(
                settings, "investbrain_server_url", "http://localhost:8000"
            ),
            access_token=secret_value(
                getattr(settings, "investbrain_access_token", "")
            ),
            request_timeout=getattr(
                settings, "investbrain_request_timeout", 60.0
            ),
            verify_ssl=getattr(settings, "investbrain_verify_ssl", True),
            include_pending=getattr(
                settings, "investbrain_include_pending", False
            ),
            portfolio_name_prefix=getattr(
                settings, "investbrain_portfolio_name_prefix", "finance-sync"
            ),
        )
