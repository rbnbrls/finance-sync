"""Configuration for the Actual Budget exporter.

Settings are read from environment variables (via ``Settings``) or can
be passed directly to ``ActualBudgetConfig``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ActualBudgetConfig(BaseModel):
    """Connection and sync configuration for an Actual Budget server.

    Usage::

        config = ActualBudgetConfig(
            server_url="http://localhost:5006",
            password="hunter2",
            budget_name="My Budget",
        )

    Or, when using with the application settings::

        config = ActualBudgetConfig.from_settings(settings)
    """

    # ── Server connection ────────────────────────────────────────────
    server_url: str = Field(
        default="http://localhost:5006",
        description="Actual Budget server URL (e.g. http://localhost:5006)",
    )
    password: str = Field(
        default="",
        description="Server password (from Settings → Show advanced)",
    )

    # ── Budget identification ────────────────────────────────────────
    sync_id: str | None = Field(
        default=None,
        description="Budget sync ID (UUID from Settings → Advanced). "
        "If provided, takes precedence over budget_name.",
    )
    budget_name: str | None = Field(
        default=None,
        description="Budget file display name, as shown in the AB UI. "
        "Ignored if sync_id is set.",
    )

    # ── Encryption ───────────────────────────────────────────────────
    encryption_password: str | None = Field(
        default=None,
        description="End-to-end encryption password, if the budget is "
        "encrypted.",
    )

    # ── Network ──────────────────────────────────────────────────────
    verify_ssl: bool | str = Field(
        default=True,
        description="SSL verification. Pass a certificate path or False "
        "to disable (not recommended).",
    )
    request_timeout: float = Field(
        default=60.0,
        description="HTTP request timeout in seconds.",
    )
    data_dir: Path = Field(
        default=Path("/tmp/finance_sync_ab_data"),
        description="Local directory for caching the budget database. "
        "A temporary directory is used per run.",
    )

    # ── Export behaviour ─────────────────────────────────────────────
    batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Max transactions per export batch.",
    )
    default_off_budget: bool = Field(
        default=False,
        description="Create new AB accounts as off-budget by default.",
    )

    # ── Account mapping overrides ────────────────────────────────────
    # Maps finance-sync account ID → AB account name.
    # An empty dict means "match by same name" (default).
    account_name_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="Override AB account name for specific "
        "finance-sync account IDs.",
    )

    model_config = {"extra": "forbid"}

    @classmethod
    def from_settings(cls, settings: Any) -> ActualBudgetConfig:
        """Build config from the application settings object.

        Looks for ``ACTUAL_BUDGET_*`` env variables via the settings.
        """
        return cls(
            server_url=getattr(settings, "actual_budget_server_url", "")
            or "http://localhost:5006",
            password=getattr(settings, "actual_budget_password", "") or "",
            sync_id=getattr(settings, "actual_budget_sync_id", None),
            budget_name=getattr(settings, "actual_budget_budget_name", None),
            encryption_password=getattr(
                settings, "actual_budget_encryption_password", None
            ),
            verify_ssl=getattr(settings, "actual_budget_verify_ssl", True),
            request_timeout=getattr(
                settings, "actual_budget_request_timeout", 60.0
            ),
            batch_size=getattr(settings, "actual_budget_batch_size", 100),
            default_off_budget=getattr(
                settings, "actual_budget_default_off_budget", False
            ),
            account_name_overrides=getattr(
                settings, "actual_budget_account_name_overrides", {}
            ),
        )
