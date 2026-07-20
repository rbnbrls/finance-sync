"""Configuration for the Wealthfolio exporter.

Settings are read from environment variables (via ``Settings``) or can
be passed directly to ``WealthfolioConfig``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class WealthfolioConfig(BaseModel):
    """Connection and sync configuration for Wealthfolio.

    Wealthfolio is a local-first desktop app using SQLite. The exporter
    can write CSV files for manual import, or write directly to the
    Wealthfolio SQLite database when ``db_path`` is provided and
    Wealthfolio is not running.

    Usage::

        config = WealthfolioConfig(
            output_dir="/tmp/wealthfolio_exports",
            default_currency="EUR",
        )

    Or, when using with the application settings::

        config = WealthfolioConfig.from_settings(settings)
    """

    # ── Output ─────────────────────────────────────────────────────────
    output_dir: Path = Field(
        default=Path("/tmp/finance_sync_wealthfolio_exports"),
        description="Directory for generated CSV export files.",
    )

    # ── Currency ───────────────────────────────────────────────────────
    default_currency: str = Field(
        default="EUR",
        description="Default currency for accounts without explicit currency.",
    )

    # ── Holdings export ────────────────────────────────────────────────
    export_holdings: bool = Field(
        default=True,
        description="Generate holdings-mode CSV snapshot of current positions.",
    )

    # ── Export behaviour ──────────────────────────────────────────────
    max_transactions_per_file: int = Field(
        default=10_000,
        ge=1,
        le=100_000,
        description="Max rows per CSV file. Larger exports are split.",
    )
    include_pending: bool = Field(
        default=False,
        description="Include pending (unsettled) transactions in the export.",
    )

    # ── Account mapping overrides ─────────────────────────────────────
    # Maps finance-sync account ID → Wealthfolio account name.
    # An empty dict means "match by same name" (default).
    account_name_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="Override Wealthfolio account name for specific "
        "finance-sync account IDs.",
    )

    # ── Instrument type mapping ────────────────────────────────────────
    # Maps finance-sync SecurityType → Wealthfolio instrument type string.
    # Default mapping is applied if not overridden.
    instrument_type_overrides: dict[str, str] = Field(
        default_factory=dict,
        description="Override instrument type mapping for specific "
        "security types. Default: stock→EQUITY, etf→ETF, "
        "mutual_fund→MUTUAL_FUND, bond→BOND, crypto→CRYPTO, "
        "other→OTHER.",
    )

    model_config = {"extra": "forbid"}

    @classmethod
    def from_settings(cls, settings: Any) -> WealthfolioConfig:
        """Build config from the application settings object.

        Looks for ``WEALTHFOLIO_*`` env variables via the settings.
        """
        return cls(
            output_dir=Path(
                getattr(settings, "wealthfolio_output_dir", "")
                or "/tmp/finance_sync_wealthfolio_exports"
            ),
            default_currency=getattr(
                settings, "wealthfolio_default_currency", "EUR"
            ),
            export_holdings=getattr(
                settings, "wealthfolio_export_holdings", True
            ),
            max_transactions_per_file=getattr(
                settings, "wealthfolio_max_transactions_per_file", 10_000
            ),
            include_pending=getattr(
                settings, "wealthfolio_include_pending", False
            ),
            account_name_overrides=getattr(
                settings, "wealthfolio_account_name_overrides", {}
            ),
            instrument_type_overrides=getattr(
                settings, "wealthfolio_instrument_type_overrides", {}
            ),
        )
