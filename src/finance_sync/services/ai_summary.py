"""AI-powered financial summary generation service.

Generates natural-language summaries of recent financial activity
using configurable LLM providers (OpenAI / Anthropic).  Results are
cached for one hour to avoid excessive API costs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from finance_sync.services.read_api import ReadService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.config.settings import Settings

logger = structlog.get_logger(__name__)

# ── Prompt templates ──────────────────────────────────────────────

_PROMPT_SUMMARY = (
    "You are a personal finance assistant. Given the following financial "
    "data for a user, write a concise natural-language summary "
    "(max {max_length} "
    "words) covering their recent activity: total spending, income, "
    "portfolio performance, and notable account changes. "
    "Use a professional but friendly tone. Do not use markdown formatting.\n\n"
    "--- Financial Data ---\n{data}"
)

_PROMPT_DAILY_BRIEFING = (
    "You are a personal finance assistant. Create a brief daily financial "
    "briefing (max {max_length} words) covering:\n"
    "1. Spending since yesterday\n"
    "2. Net worth change\n"
    "3. Portfolio highlights (best/worst performers)\n"
    "4. Any unusual activity\n"
    "Use a professional but friendly tone. Do not use markdown.\n\n"
    "--- Financial Data ---\n{data}"
)

# ── Response models ───────────────────────────────────────────────


class SummaryResponse:
    """Natural-language summary response."""

    def __init__(
        self,
        *,
        summary: str,
        generated_at: datetime,
        source: str = "ai_generated",
        model: str = "",
    ) -> None:
        self.summary = summary
        self.generated_at = generated_at
        self.source = source
        self.model = model

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "generated_at": self.generated_at.isoformat(),
            "source": self.source,
            "model": self.model,
        }


class DailyBriefingResponse:
    """Daily financial briefing."""

    def __init__(
        self,
        *,
        briefing: str,
        date: str,
        generated_at: datetime,
        model: str = "",
    ) -> None:
        self.briefing = briefing
        self.date = date
        self.generated_at = generated_at
        self.model = model

    def to_dict(self) -> dict[str, Any]:
        return {
            "briefing": self.briefing,
            "date": self.date,
            "generated_at": self.generated_at.isoformat(),
            "model": self.model,
        }


# ── Cache ─────────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl_seconds: int) -> Any | None:
    """Return cached value if still fresh, else None."""
    _ = ttl_seconds  # kept for API consistency with _cache_set
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if expires_at < _now_timestamp():
        del _CACHE[key]
        return None
    return value


def _cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    """Store value with a TTL."""
    expires_at = _now_timestamp() + ttl_seconds
    _CACHE[key] = (expires_at, value)


def _now_timestamp() -> float:
    """Current monotonic-like timestamp for cache comparisons."""
    return datetime.now(UTC).timestamp()


# ── Helpers ───────────────────────────────────────────────────────


def _format_prompt(
    template: str,
    data: dict[str, Any],
    max_length: int,
) -> str:
    """Format a prompt template with financial data."""
    data_json = json.dumps(data, indent=2, default=str)
    return template.format(max_length=max_length, data=data_json)


# ── Service ───────────────────────────────────────────────────────


class AISummaryService:
    """Generate AI-powered financial summaries.

    Uses a configurable LLM API (OpenAI or Anthropic) with a simple
    HTTP client.  Results are cached in memory for the configured TTL.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        self._session = session
        self._settings = settings
        self._read_service = ReadService(session)
        self._http_client: httpx.AsyncClient | None = None
        self._log = logger.bind(service="ai_summary")

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init the HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self) -> None:
        """Release the HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ── Public API ────────────────────────────────────────────────

    async def generate_summary(
        self,
        tenant_id: str,
        *,
        time_period_days: int = 30,
        force_refresh: bool = False,
    ) -> SummaryResponse:
        """Generate a natural-language summary of recent financial activity.

        Parameters
        ----------
        tenant_id:
            The tenant scope.
        time_period_days:
            How many days of history to consider.
        force_refresh:
            Bypass the cache.

        Returns
        -------
        SummaryResponse with the generated text and metadata.
        """
        cache_key = f"summary:{tenant_id}:{time_period_days}"

        if not force_refresh:
            cached = _cache_get(
                cache_key, self._settings.ai_summary_cache_ttl_seconds
            )
            if cached is not None:
                self._log.info("ai_summary_cache_hit", tenant_id=tenant_id)
                return cached

        self._log.info(
            "ai_summary_generating",
            tenant_id=tenant_id,
            time_period_days=time_period_days,
        )

        # Gather financial data
        data = await self._collect_financial_data(tenant_id, time_period_days)

        # Build prompt
        prompt = _format_prompt(
            _PROMPT_SUMMARY,
            data,
            self._settings.ai_summary_max_length,
        )

        # Call LLM
        model, raw_text = await self._call_llm(prompt)

        response = SummaryResponse(
            summary=raw_text,
            generated_at=datetime.now(UTC),
            model=model,
        )

        # Cache
        _cache_set(
            cache_key, response, self._settings.ai_summary_cache_ttl_seconds
        )

        return response

    async def generate_daily_briefing(
        self,
        tenant_id: str,
        *,
        force_refresh: bool = False,
    ) -> DailyBriefingResponse:
        """Generate a daily financial briefing.

        Parameters
        ----------
        tenant_id:
            The tenant scope.
        force_refresh:
            Bypass the cache.

        Returns
        -------
        DailyBriefingResponse with the briefing text.
        """
        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        cache_key = f"daily_briefing:{tenant_id}:{today_str}"

        if not force_refresh:
            cached = _cache_get(
                cache_key, self._settings.ai_summary_cache_ttl_seconds
            )
            if cached is not None:
                self._log.info(
                    "ai_daily_briefing_cache_hit", tenant_id=tenant_id
                )
                return cached

        self._log.info(
            "ai_daily_briefing_generating",
            tenant_id=tenant_id,
            date=today_str,
        )

        # Gather data for the daily briefing (shorter lookback)
        data = await self._collect_financial_data(tenant_id, time_period_days=1)

        # Build prompt
        prompt = _format_prompt(
            _PROMPT_DAILY_BRIEFING,
            data,
            self._settings.ai_summary_max_length,
        )

        # Call LLM
        model, raw_text = await self._call_llm(prompt)

        response = DailyBriefingResponse(
            briefing=raw_text,
            date=today_str,
            generated_at=datetime.now(UTC),
            model=model,
        )

        _cache_set(
            cache_key, response, self._settings.ai_summary_cache_ttl_seconds
        )

        return response

    # ── Data gathering ─────────────────────────────────────────────

    async def _collect_financial_data(
        self,
        tenant_id: str,
        time_period_days: int,
    ) -> dict[str, Any]:
        """Gather portfolio, net-worth, account, and transaction data.

        Returns a serialisable dict for prompt templating.
        """
        since = datetime.now(UTC) - timedelta(days=time_period_days)

        # Parallel data collection
        net_worth_data = await self._read_service.get_net_worth(tenant_id)
        portfolio_data = await self._read_service.get_portfolio(tenant_id)

        accounts_data = await self._read_service.list_accounts(
            tenant_id, limit=100
        )

        # Recent transactions (grab across accounts via the transaction method)
        # We iterate known accounts
        all_transactions: list[dict[str, Any]] = []
        spending_total = Decimal(0)
        income_total = Decimal(0)

        for acct in accounts_data.items:
            tx_data = await self._read_service.list_account_transactions(
                tenant_id,
                acct.id,
                limit=50,
                date_from=since,
            )
            for tx in tx_data.items:
                tx_dict = {
                    "account": acct.name,
                    "amount": str(tx.amount),
                    "currency": tx.currency_code,
                    "description": tx.description,
                    "date": tx.occurred_at.isoformat()
                    if tx.occurred_at
                    else None,
                    "type": tx.transaction_type,
                }
                all_transactions.append(tx_dict)

                # Categorise for quick totals
                if tx.amount < Decimal(0):
                    spending_total += abs(tx.amount)
                elif tx.amount > Decimal(0):
                    income_total += tx.amount

        # Sort by date (newest first)
        all_transactions.sort(key=lambda t: t.get("date") or "", reverse=True)

        return {
            "net_worth": {
                "total_assets": str(net_worth_data.total_assets or "0"),
                "total_liabilities": str(
                    net_worth_data.total_liabilities or "0"
                ),
                "net_worth": str(net_worth_data.net_worth or "0"),
                "currency": net_worth_data.currency_code,
            },
            "portfolio": {
                "total_value": str(portfolio_data.total_value or "0"),
                "total_cost_basis": str(portfolio_data.total_cost_basis or "0"),
                "account_count": len(portfolio_data.accounts),
            },
            "accounts": {
                "total": accounts_data.total,
                "active_count": sum(
                    1 for a in accounts_data.items if a.is_active
                ),
            },
            "transactions": {
                "total_in_period": len(all_transactions),
                "spending_total": str(spending_total),
                "income_total": str(income_total),
                "recent": all_transactions[:20],
            },
        }

    # ── LLM call ──────────────────────────────────────────────────

    async def _call_llm(self, prompt: str) -> tuple[str, str]:
        """Call the configured LLM provider and return (model, text).

        Raises
        ------
        RuntimeError
            If no API key is configured or the call fails.
        """
        api_key = self._settings.ai_api_key
        if api_key is None:
            msg = (
                "AI summary generation is disabled: no AI_API_KEY configured. "
                "Set the AI_API_KEY environment variable."
            )
            raise RuntimeError(msg)

        secret = api_key.get_secret_value()
        provider = self._settings.ai_provider

        if provider == "openai":
            return await self._call_openai(secret, prompt)
        if provider == "anthropic":
            return await self._call_anthropic(secret, prompt)
        msg = (
            f"Unsupported AI provider: {provider!r}"
            " (expected 'openai' or 'anthropic')"
        )
        raise ValueError(msg)

    async def _call_openai(self, api_key: str, prompt: str) -> tuple[str, str]:
        """Call the OpenAI Chat Completions API."""
        base_url = self._settings.ai_base_url or "https://api.openai.com/v1"
        model = self._settings.ai_model

        client = await self._get_client()
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self._settings.ai_summary_max_length * 2,
                "temperature": 0.3,
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        choices: list[Any] = data.get("choices", [])
        if not choices:
            msg = "OpenAI returned no choices"
            raise RuntimeError(msg)
        text: str = choices[0].get("message", {}).get("content", "")
        used_model: str = data.get("model", model)
        return used_model, text.strip()

    async def _call_anthropic(
        self, api_key: str, prompt: str
    ) -> tuple[str, str]:
        """Call the Anthropic Messages API."""
        base_url = self._settings.ai_base_url or "https://api.anthropic.com/v1"
        model = self._settings.ai_model

        # Map common model aliases if needed
        client = await self._get_client()
        response = await client.post(
            f"{base_url}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": self._settings.ai_summary_max_length * 2,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        content_list: list[Any] = data.get("content", [])
        if not content_list:
            msg = "Anthropic returned no content"
            raise RuntimeError(msg)
        text: str = "".join(
            block.get("text", "")
            for block in content_list
            if block.get("type") == "text"
        )
        used_model: str = data.get("model", model)
        return used_model, text.strip()
