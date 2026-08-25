from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class SecuroConfig(BaseModel):
    """Connection settings for a self-hosted Securo instance."""

    server_url: str = "http://localhost:3001"
    email: str = ""
    password: str = ""
    output_dir: Path = Field(default=Path("/tmp/finance_sync_securo_exports"))
    request_timeout: float = 60.0
    verify_ssl: bool = True
    auto_create_accounts: bool = True
    account_name_overrides: dict[str, str] = Field(default_factory=dict)
