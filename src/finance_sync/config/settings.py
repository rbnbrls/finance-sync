"""Application settings loaded from environment variables /.env file."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from finance_sync.config.environments import Environment

ROOT_DIR: Path = Path(__file__).resolve().parent.parent.parent.parent


class Settings(BaseSettings):
    """Application configuration.

    Values are read from environment variables or a ``.env`` file at the
    project root.  Secret values (passwords, API keys) are held as
    ``SecretStr`` and never displayed in repr/dumps.
    """

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Environment ──────────────────────────────────────────────────
    environment: Environment = Field(
        default=Environment.DEVELOPMENT,
        description="Runtime environment (dev/staging/prod).",
    )

    # ── Application ──────────────────────────────────────────────────
    app_name: str = Field(default="finance-sync", validation_alias="APP_NAME")
    app_version: str = Field(default="0.1.0", validation_alias="APP_VERSION")
    debug: bool = Field(default=False, validation_alias="DEBUG")

    # ── Logging ──────────────────────────────────────────────────────
    log_level: str = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
        description="Minimum log level (DEBUG, INFO, WARNING, ERROR).",
    )

    # ── CORS ─────────────────────────────────────────────────────────
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        validation_alias="CORS_ORIGINS",
    )

    # ── Database ─────────────────────────────────────────────────────
    database_url: PostgresDsn | None = Field(
        default=None,
        validation_alias="DATABASE_URL",
        description="PostgreSQL DSN.  If omitted, DB features are disabled.",
    )
    database_pool_min_size: int = Field(
        default=2,
        ge=1,
        validation_alias="DATABASE_POOL_MIN_SIZE",
    )
    database_pool_max_size: int = Field(
        default=10,
        ge=1,
        validation_alias="DATABASE_POOL_MAX_SIZE",
    )

    # ── Redis ────────────────────────────────────────────────────────
    redis_url: RedisDsn | None = Field(
        default=None,
        validation_alias="REDIS_URL",
        description="Redis DSN.  If omitted, caching features are disabled.",
    )

    # ── Security / JWT ───────────────────────────────────────────────
    secret_key: SecretStr = Field(
        default=SecretStr("change-me-in-production"),
        validation_alias="SECRET_KEY",
    )
    access_token_expire_minutes: int = Field(
        default=30,
        ge=1,
        validation_alias="ACCESS_TOKEN_EXPIRE_MINUTES",
    )
    refresh_token_expire_days: int = Field(
        default=7,
        ge=1,
        validation_alias="REFRESH_TOKEN_EXPIRE_DAYS",
    )
    jwt_algorithm: str = Field(
        default="HS256",
        validation_alias="JWT_ALGORITHM",
    )

    # ── OpenBB / Enrichment ──────────────────────────────────────────
    openbb_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENBB_API_KEY",
        description="OpenBB Platform API key.",
    )
    openbb_base_url: str = Field(
        default="https://openbb.co/api/v1",
        validation_alias="OPENBB_BASE_URL",
        description="OpenBB API base URL.",
    )
    openbb_api_version: str = Field(
        default="v1",
        validation_alias="OPENBB_API_VERSION",
        description="Pinned OpenBB API version.",
    )
    openbb_rate_limit_rps: int = Field(
        default=10,
        ge=1,
        validation_alias="OPENBB_RATE_LIMIT_RPS",
        description="Max requests per second to OpenBB.",
    )
    openbb_request_timeout: int = Field(
        default=30,
        ge=1,
        validation_alias="OPENBB_REQUEST_TIMEOUT",
        description="Timeout in seconds for OpenBB HTTP requests.",
    )

    # ── Price-store pruning ──────────────────────────────────────────
    price_store_keep_minute_days: int = Field(
        default=30,
        ge=1,
        validation_alias="PRICE_STORE_KEEP_MINUTE_DAYS",
        description="Number of days to retain minutely/intraday price data.",
    )
    price_store_keep_hour_days: int = Field(
        default=90,
        ge=1,
        validation_alias="PRICE_STORE_KEEP_HOUR_DAYS",
        description="Number of days to retain hourly price data.",
    )
    price_store_keep_daily_forever: bool = Field(
        default=True,
        validation_alias="PRICE_STORE_KEEP_DAILY_FOREVER",
        description="Keep daily price data forever (no pruning).",
    )

    # ── Credential encryption ────────────────────────────────────────
    master_encryption_key: SecretStr | None = Field(
        default=None,
        validation_alias="MASTER_ENCRYPTION_KEY",
        description="Hex-encoded 32-byte AES-256-GCM key for credential "
        "envelope encryption.  Generate with: openssl rand -hex 32",
    )

    # ── Actual Budget exporter ───────────────────────────────────────
    actual_budget_server_url: str = Field(
        default="http://localhost:5006",
        validation_alias="ACTUAL_BUDGET_SERVER_URL",
        description="Actual Budget server URL.",
    )
    actual_budget_password: str = Field(
        default="",
        validation_alias="ACTUAL_BUDGET_PASSWORD",
        description="Actual Budget server password.",
    )
    actual_budget_sync_id: str | None = Field(
        default=None,
        validation_alias="ACTUAL_BUDGET_SYNC_ID",
        description="Budget sync ID (UUID) from AB Settings.",
    )
    actual_budget_budget_name: str | None = Field(
        default=None,
        validation_alias="ACTUAL_BUDGET_BUDGET_NAME",
        description="Budget file display name.",
    )
    actual_budget_encryption_password: str | None = Field(
        default=None,
        validation_alias="ACTUAL_BUDGET_ENCRYPTION_PASSWORD",
        description="E2E encryption password for the budget.",
    )
    actual_budget_verify_ssl: bool = Field(
        default=True,
        validation_alias="ACTUAL_BUDGET_VERIFY_SSL",
        description="Verify SSL certificate when connecting to AB server.",
    )
    actual_budget_request_timeout: float = Field(
        default=60.0,
        validation_alias="ACTUAL_BUDGET_REQUEST_TIMEOUT",
        description="Timeout in seconds for AB HTTP requests.",
    )
    actual_budget_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        validation_alias="ACTUAL_BUDGET_BATCH_SIZE",
        description="Max transactions per export batch.",
    )

    # ── Wealthfolio exporter ─────────────────────────────────────────
    wealthfolio_output_dir: str = Field(
        default="/tmp/finance_sync_wealthfolio_exports",
        validation_alias="WEALTHFOLIO_OUTPUT_DIR",
        description="Directory for Wealthfolio CSV export files.",
    )
    wealthfolio_default_currency: str = Field(
        default="EUR",
        validation_alias="WEALTHFOLIO_DEFAULT_CURRENCY",
        description="Default currency for accounts without explicit currency.",
    )
    wealthfolio_export_holdings: bool = Field(
        default=True,
        validation_alias="WEALTHFOLIO_EXPORT_HOLDINGS",
        description="Generate holdings-mode CSV snapshot.",
    )
    wealthfolio_max_transactions_per_file: int = Field(
        default=10_000,
        ge=1,
        le=100_000,
        validation_alias="WEALTHFOLIO_MAX_TRANSACTIONS_PER_FILE",
        description="Max rows per CSV file.",
    )
    wealthfolio_include_pending: bool = Field(
        default=False,
        validation_alias="WEALTHFOLIO_INCLUDE_PENDING",
        description="Include pending transactions in export.",
    )
    wealthfolio_account_name_overrides: dict[str, str] = Field(
        default_factory=dict,
        validation_alias="WEALTHFOLIO_ACCOUNT_NAME_OVERRIDES",
        description="Override Wealthfolio account name per "
        "finance-sync account ID.",
    )
    wealthfolio_instrument_type_overrides: dict[str, str] = Field(
        default_factory=dict,
        validation_alias="WEALTHFOLIO_INSTRUMENT_TYPE_OVERRIDES",
        description="Override instrument type mapping.",
    )

    # ── Worker / APScheduler ───────────────────────────────────────
    worker_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_ENABLED",
        description="Enable the background worker process (APScheduler).",
    )
    worker_health_port: int = Field(
        default=9090,
        ge=1024,
        le=65535,
        validation_alias="WORKER_HEALTH_PORT",
        description="Port for the worker health HTTP server.",
    )

    # ── Worker: bunq sync job ──────────────────────────────────────
    worker_job_bunq_sync_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_JOB_BUNQ_SYNC_ENABLED",
    )
    worker_job_bunq_sync_interval_minutes: int = Field(
        default=15,
        ge=1,
        validation_alias="WORKER_JOB_BUNQ_SYNC_INTERVAL_MINUTES",
    )

    # ── Worker: Trading212 sync job ────────────────────────────────
    worker_job_trading212_sync_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_JOB_TRADING212_SYNC_ENABLED",
    )
    worker_job_trading212_sync_interval_hours: int = Field(
        default=1,
        ge=1,
        validation_alias="WORKER_JOB_TRADING212_SYNC_INTERVAL_HOURS",
    )

    # ── Worker: Price enrichment job ───────────────────────────────
    worker_job_price_enrichment_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_JOB_PRICE_ENRICHMENT_ENABLED",
    )
    worker_job_price_enrichment_interval_minutes: int = Field(
        default=15,
        ge=1,
        validation_alias="WORKER_JOB_PRICE_ENRICHMENT_INTERVAL_MINUTES",
    )
    worker_job_price_enrichment_market_open: str = Field(
        default="09:30",
        validation_alias="WORKER_JOB_PRICE_ENRICHMENT_MARKET_OPEN",
        description="Market open time (EST) for price enrichment "
        "window, e.g. '09:30'.",
    )
    worker_job_price_enrichment_market_close: str = Field(
        default="16:00",
        validation_alias="WORKER_JOB_PRICE_ENRICHMENT_MARKET_CLOSE",
        description="Market close time (EST) for price enrichment "
        "window, e.g. '16:00'.",
    )

    # ── Worker: Nightly reconciliation job ─────────────────────────
    worker_job_reconciliation_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_JOB_RECONCILIATION_ENABLED",
    )
    worker_job_reconciliation_cron: str = Field(
        default="0 2 * * *",
        validation_alias="WORKER_JOB_RECONCILIATION_CRON",
        description="Cron expression for nightly full reconciliation (UTC).",
    )

    # ── Worker: Outbox consumer job ────────────────────────────────
    worker_job_outbox_enabled: bool = Field(
        default=True,
        validation_alias="WORKER_JOB_OUTBOX_ENABLED",
    )
    worker_job_outbox_interval_seconds: int = Field(
        default=30,
        ge=1,
        validation_alias="WORKER_JOB_OUTBOX_INTERVAL_SECONDS",
    )

    # ── Webhooks ─────────────────────────────────────────────────────
    webhook_max_retries: int = Field(
        default=5,
        ge=0,
        le=20,
        validation_alias="WEBHOOK_MAX_RETRIES",
        description=(
            "Max webhook delivery retry attempts (exponential backoff)."
        ),
    )
    webhook_retry_base_delay_s: float = Field(
        default=10.0,
        ge=0.5,
        validation_alias="WEBHOOK_RETRY_BASE_DELAY_S",
        description="Initial retry delay in seconds (doubles each attempt).",
    )
    webhook_request_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        validation_alias="WEBHOOK_REQUEST_TIMEOUT_S",
        description="Timeout per webhook HTTP request.",
    )

    # ── AI summary generation ────────────────────────────────────────
    ai_enabled: bool = Field(
        default=True,
        validation_alias="AI_ENABLED",
        description="Enable AI summary generation features.",
    )
    ai_provider: str = Field(
        default="openai",
        validation_alias="AI_PROVIDER",
        description="AI provider: 'openai' or 'anthropic'.",
    )
    ai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="AI_API_KEY",
        description="API key for the AI summary LLM provider.",
    )
    ai_model: str = Field(
        default="gpt-4o",
        validation_alias="AI_MODEL",
        description=(
            "Model name for AI summary generation"
            " (e.g. gpt-4o, claude-sonnet-4)."
        ),
    )
    ai_base_url: str | None = Field(
        default=None,
        validation_alias="AI_BASE_URL",
        description="Base URL for the AI API (e.g. https://api.openai.com/v1). "
        "When unset the service uses the provider's default.",
    )
    ai_summary_cache_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        validation_alias="AI_SUMMARY_CACHE_TTL_SECONDS",
        description="TTL for AI summary cache in seconds (default 1 hour).",
    )
    ai_rate_limit_max_requests: int = Field(
        default=20,
        ge=1,
        validation_alias="AI_RATE_LIMIT_MAX_REQUESTS",
        description="Max AI summary requests per window per client.",
    )
    ai_rate_limit_window_seconds: int = Field(
        default=3600,
        ge=1,
        validation_alias="AI_RATE_LIMIT_WINDOW_SECONDS",
        description="Rate limit window in seconds for AI summary endpoints.",
    )
    ai_summary_max_length: int = Field(
        default=500,
        ge=50,
        le=4000,
        validation_alias="AI_SUMMARY_MAX_LENGTH",
        description="Maximum word length for generated summaries.",
    )

    # ── Home Assistant integration ────────────────────────────────────
    ha_enabled: bool = Field(
        default=True,
        validation_alias="HA_ENABLED",
        description="Enable Home Assistant sensor integration endpoints.",
    )

    # ── MCP Server ───────────────────────────────────────────────────
    mcp_port: int = Field(
        default=8100,
        ge=1024,
        le=65535,
        validation_alias="MCP_PORT",
        description="Port for the MCP SSE server.",
    )
    mcp_host: str = Field(
        default="0.0.0.0",
        validation_alias="MCP_HOST",
        description="Host address for the MCP SSE server.",
    )

    # ── GitHub issue creation (feedback) ────────────────────────────
    github_token: str = Field(
        default="",
        validation_alias="GITHUB_TOKEN",
        description="GitHub personal access token for creating issues from feedback.",
    )
    github_repo: str = Field(
        default="rbnbrls/finance-sync",
        validation_alias="GITHUB_REPO",
        description="GitHub repository name (owner/repo) for feedback issues.",
    )

    # ── Worker: Retry ──────────────────────────────────────────────
    worker_retry_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="WORKER_RETRY_MAX_ATTEMPTS",
        description="Max retry attempts for failed sync jobs "
        "(exponential backoff).",
    )
    worker_retry_base_delay_s: float = Field(
        default=1.0,
        ge=0.1,
        validation_alias="WORKER_RETRY_BASE_DELAY_S",
        description="Base delay in seconds for exponential backoff.",
    )

    # ── Validators ───────────────────────────────────────────────────

    @field_validator("secret_key")
    @classmethod
    def _secret_key_min_length(cls, v: SecretStr) -> SecretStr:
        """Ensure secret keys are at least 16 characters long."""
        if len(v.get_secret_value()) < 16:
            msg = "Secret key must be at least 16 characters long"
            raise ValueError(msg)
        return v

    # ── Computed properties ──────────────────────────────────────────

    @property
    def is_debug(self) -> bool:
        """Enable debug behaviour when the environment allows it."""
        return self.debug or self.environment.is_debug

    @property
    def is_production(self) -> bool:
        """True in production."""
        return self.environment.is_production
