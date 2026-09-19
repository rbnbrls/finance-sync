"""Configuration for the YNAB native transaction adapter."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class YNABConfig:
    """Non-secret YNAB settings plus an already decrypted access token."""

    access_token: str
    budget_id: str
    api_base_url: str = "https://api.ynab.com/v1"
    account_map: dict[str, str] = field(
        default_factory=lambda: dict[str, str]()
    )
    category_map: dict[str, str] = field(
        default_factory=lambda: dict[str, str]()
    )
    transfer_account_map: dict[str, str] = field(
        default_factory=lambda: dict[str, str]()
    )
    retry_attempts: int = 3
    retry_base_delay: float = 0.5
