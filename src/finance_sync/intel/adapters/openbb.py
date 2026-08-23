"""OpenBB market-intelligence adapter.

Implements :class:`IntelProvider` on top of the OpenBB Platform REST
API — the same endpoint family the existing enrichment gateway uses
(``openbb.co/api/v1``).  Capabilities:

* ``news`` — OpenBB news endpoint (provider-dependent; typically
  requires an OpenBB API key).  The adapter is *degraded* without a
  key and reports ``unavailable`` for every capability.
* ``earnings`` — structured earnings data when the configured OpenBB
  backend exposes it (optional; capability discovery reports what the
  backend actually serves).

Licensing: the OpenBB terms govern reuse.  The adapter stores only
headlines, short snippets and structured facts — never full articles —
unless the upstream response explicitly carries a permissive license
class, which today it never does.  ``store_full_text`` is therefore
always ``False`` and ``store_summary`` is ``True`` only for the short
snippet the API returns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog

from finance_sync.intel.enums import (
    IntelAvailability,
    IntelCapability,
    IntelItemKind,
    IntelLicenseClass,
)
from finance_sync.intel.exceptions import (
    IntelProviderAuthError,
    IntelProviderInvalidResponseError,
    IntelProviderRateLimitError,
    IntelProviderTimeoutError,
    IntelProviderUnavailableError,
)
from finance_sync.intel.hashing import content_hash
from finance_sync.intel.models import IntelItem, IntelStructuredFact
from finance_sync.intel.provider import (
    IntelFreshnessPolicy,
    IntelProvider,
    IntelRateLimit,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger(__name__)


class OpenBBIntelProvider(IntelProvider):
    """Market-intelligence adapter backed by the OpenBB Platform API."""

    provider_key = "openbb"
    display_name = "OpenBB Platform"
    license_note = (
        "OpenBB terms of service apply. finance-sync stores only "
        "headlines, short snippets and structured facts, never full "
        "articles, and always links back to the canonical URL."
    )
    config_url = (
        "https://docs.openbb.co/platform/developer-tools/authentication"
    )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://openbb.co/api/v1",
        request_timeout: float = 30.0,
        enabled: bool = True,
        rate_limit: IntelRateLimit | None = None,
        freshness: IntelFreshnessPolicy | None = None,
        retry_max_attempts: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        super().__init__(
            enabled=enabled,
            rate_limit=rate_limit,
            freshness=freshness,
            retry_max_attempts=retry_max_attempts,
            retry_base_delay=retry_base_delay,
        )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._request_timeout = request_timeout
        self._http_client: httpx.AsyncClient | None = None
        if not api_key:
            logger.warning(
                "openbb_intel_provider_degraded",
                reason="no_api_key",
                message=(
                    "OpenBB API key not configured — market-intelligence "
                    "capabilities will report unavailable."
                ),
            )

    def configure(self, credentials: dict[str, str]) -> None:
        """Inject decrypted per-tenant OpenBB credentials before a run.

        Accepts ``api_key`` (and optionally ``token``) from the
        envelope-decrypted credential store.  A key injected here
        replaces any key from the global settings for this provider
        instance (per-tenant credentials win).  The client is rebuilt
        lazily on the next access, so a rotation mid-run is picked up
        by the next request.
        """
        api_key = credentials.get("api_key") or credentials.get("token")
        if api_key:
            self._api_key = api_key
            self._http_client = None  # rebuild with the new key

    # ── HTTP client ─────────────────────────────────────────────────

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy-init HTTP client for the OpenBB API."""
        if self._http_client is None or self._http_client.is_closed:
            headers: dict[str, str] = {
                "Accept": "application/json",
                "User-Agent": "finance-sync/0.5.0",
            }
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._http_client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._request_timeout),
                headers=headers,
            )
        return self._http_client

    async def close(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    # ── Capability discovery ────────────────────────────────────────

    async def capabilities(self) -> Sequence[IntelCapability]:
        """Return the capabilities this adapter can attempt.

        Without an API key the adapter is degraded and reports no
        capabilities (every capability then surfaces as
        ``unavailable`` through :meth:`available`).
        """
        if not self._api_key:
            return []
        return [IntelCapability.NEWS, IntelCapability.EARNINGS]

    async def available(self, capability: IntelCapability) -> IntelAvailability:
        """Return runtime availability for *capability*.

        * no API key                        → ``unavailable``
        * capability not offered            → ``unavailable``
        * reachable + key                   → ``available``
        * reachable, key, unknown capability→ ``unavailable``
        """
        if not self._api_key:
            return IntelAvailability.UNAVAILABLE
        if capability not in (IntelCapability.NEWS, IntelCapability.EARNINGS):
            return IntelAvailability.UNAVAILABLE
        # A lightweight reachability probe keeps discovery honest: if
        # the API is down we say so explicitly instead of pretending.
        try:
            await self._rate_limiter.acquire()
            response = await self.http_client.get("/api/v1/health")
            if response.status_code in (200, 404):
                return IntelAvailability.AVAILABLE
            return IntelAvailability.DEGRADED
        except (httpx.TimeoutException, httpx.HTTPError):
            return IntelAvailability.UNAVAILABLE

    # ── Fetch ───────────────────────────────────────────────────────

    async def fetch(
        self,
        capability: IntelCapability,
        *,
        identifiers: dict[str, str] | None = None,
        limit: int = 20,
    ) -> Sequence[IntelItem]:
        """Fetch items for *capability* from OpenBB.

        Raises typed :mod:`finance_sync.intel.exceptions` errors so the
        scheduler can classify the run.  Items are built with
        ``store_full_text=False`` and ``store_summary=True`` (short
        snippet only) — full article text is never persisted.
        """
        if not self._api_key:
            msg = "OpenBB API key not configured — provider is degraded"
            raise IntelProviderAuthError(msg)

        try:
            if capability == IntelCapability.NEWS:
                return await self._fetch_news(
                    identifiers=identifiers, limit=limit
                )
            if capability == IntelCapability.EARNINGS:
                return await self._fetch_earnings(
                    identifiers=identifiers, limit=limit
                )
            msg = f"capability {capability.value!r} not supported by openbb"
            raise IntelProviderInvalidResponseError(msg)
        except httpx.TimeoutException as exc:
            msg = (
                f"OpenBB intel request timed out after {self._request_timeout}s"
            )
            raise IntelProviderTimeoutError(msg) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retry_after = _parse_retry_after(
                exc.response.headers.get("Retry-After")
            )
            if status == 401:
                msg = "Invalid or expired OpenBB API key (401)"
                raise IntelProviderAuthError(msg) from exc
            if status == 403:
                msg = (
                    "OpenBB API key lacks permission for this capability (403)"
                )
                raise IntelProviderAuthError(msg) from exc
            if status == 429:
                msg = "OpenBB API rate limit exceeded (429)"
                raise IntelProviderRateLimitError(
                    msg, retry_after=retry_after
                ) from exc
            if status >= 500:
                msg = f"OpenBB upstream error (HTTP {status})"
                raise IntelProviderUnavailableError(msg) from exc
            msg = f"OpenBB returned HTTP {status}"
            raise IntelProviderInvalidResponseError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"OpenBB intel HTTP request failed: {exc}"
            raise IntelProviderUnavailableError(msg) from exc

    async def _fetch_news(
        self,
        *,
        identifiers: dict[str, str] | None = None,
        limit: int = 20,
    ) -> list[IntelItem]:
        """Fetch news items via the OpenBB news endpoint.

        The endpoint shape varies by backend; we defensively accept
        ``results``/``data``/``news`` arrays and per-item ``id``/``url``
        keys.  Unknown shapes raise :class:`IntelProviderInvalidResponseError`.
        """
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if identifiers:
            ticker = identifiers.get("ticker") or identifiers.get("symbol")
            if ticker:
                params["symbols"] = ticker
            if identifiers.get("isin"):
                params["symbols"] = identifiers["isin"]

        response = await self.http_client.get(
            "/api/v1/market/news",
            params=params,
        )
        response.raise_for_status()
        data: Any = response.json()

        raw_items = _as_item_dicts(data, "results", "data", "news", "items")

        now = datetime.now(UTC)
        items: list[IntelItem] = []
        for raw_dict in raw_items[:limit]:
            source_id = (
                raw_dict.get("id")
                or raw_dict.get("article_id")
                or raw_dict.get("url")
                or ""
            )
            if not source_id:
                continue
            url = (
                raw_dict.get("url")
                or raw_dict.get("link")
                or raw_dict.get("canonical_url")
            )
            headline = raw_dict.get("title") or raw_dict.get("headline") or ""
            summary = (
                raw_dict.get("summary") or raw_dict.get("description") or ""
            )
            published = _parse_datetime(
                raw_dict.get("published_utc")
                or raw_dict.get("published_at")
                or raw_dict.get("date")
                or raw_dict.get("datetime")
            )
            ticker_hits = _extract_tickers(raw_dict)
            hash_input: dict[str, str] = {
                "provider": self.provider_key,
                "source_id": str(source_id),
            }
            if url:
                hash_input["url"] = str(url)
            if headline:
                hash_input["headline"] = str(headline)
            items.append(
                IntelItem(
                    provider=self.provider_key,
                    source_id=str(source_id),
                    canonical_url=str(url) if url else None,
                    kind=IntelItemKind.NEWS_ARTICLE,
                    published_at=published or now,
                    fetched_at=now,
                    language=str(raw_dict.get("language") or "en"),
                    license_class=IntelLicenseClass.FREE_ACCESS,
                    content_hash=content_hash(hash_input),
                    headline=str(headline) or None,
                    summary=str(summary)[:500] or None,
                    store_full_text=False,
                    store_summary=True,
                    identifiers=ticker_hits,
                    provider_metadata=_clean_meta(raw_dict),
                )
            )
        return items

    async def _fetch_earnings(
        self,
        *,
        identifiers: dict[str, str] | None = None,
        limit: int = 20,
    ) -> list[IntelItem]:
        """Fetch structured earnings estimates via the OpenBB API.

        This is an optional capability: when the backend does not
        expose it, an ``IntelProviderInvalidResponseError`` is raised
        and the scheduler records the capability as unavailable.
        """
        if not identifiers:
            return []
        ticker = identifiers.get("ticker") or identifiers.get("symbol")
        if not ticker:
            return []
        params: dict[str, Any] = {"symbol": ticker, "limit": min(limit, 100)}
        response = await self.http_client.get(
            "/api/v1/market/earnings",
            params=params,
        )
        response.raise_for_status()
        data: Any = response.json()

        raw_items = _as_item_dicts(
            data, "results", "data", "earnings", "estimates"
        )

        now = datetime.now(UTC)
        items: list[IntelItem] = []
        for raw_dict in raw_items[:limit]:
            period = raw_dict.get("period") or raw_dict.get("date")
            source_id = str(
                raw_dict.get("id")
                or raw_dict.get("estimate_id")
                or f"{ticker}-{period or now.date()}"
            )
            url = raw_dict.get("url") or raw_dict.get("canonical_url")
            published = _parse_datetime(
                raw_dict.get("published_utc")
                or raw_dict.get("published_at")
                or raw_dict.get("date")
            )
            facts: list[IntelStructuredFact] = []
            eps_est = raw_dict.get("eps_estimate") or raw_dict.get("eps")
            if eps_est is not None:
                facts.append(
                    IntelStructuredFact(
                        key="eps_estimate",
                        value=str(eps_est),
                        unit="currency_per_share",
                        as_of=published or now,
                        item_url=str(url) if url else None,
                    )
                )
            rev_est = raw_dict.get("revenue_estimate") or raw_dict.get(
                "revenue"
            )
            if rev_est is not None:
                facts.append(
                    IntelStructuredFact(
                        key="revenue_estimate",
                        value=str(rev_est),
                        unit="currency",
                        as_of=published or now,
                        item_url=str(url) if url else None,
                    )
                )
            items.append(
                IntelItem(
                    provider=self.provider_key,
                    source_id=source_id,
                    canonical_url=str(url) if url else None,
                    kind=IntelItemKind.ANALYST_ESTIMATE,
                    published_at=published or now,
                    fetched_at=now,
                    language="en",
                    license_class=IntelLicenseClass.FREE_ACCESS,
                    content_hash=content_hash(
                        {
                            "provider": self.provider_key,
                            "source_id": source_id,
                            "ticker": ticker,
                        }
                    ),
                    headline=(
                        f"{ticker} earnings estimate"
                        + (
                            f" ({raw_dict.get('period')})"
                            if raw_dict.get("period")
                            else ""
                        )
                    ),
                    summary=None,
                    store_full_text=False,
                    store_summary=True,
                    identifiers={"ticker": ticker},
                    facts=facts,
                    provider_metadata=_clean_meta(raw_dict),
                )
            )
        return items


# ── Helpers ─────────────────────────────────────────────────────────────


def _as_item_dicts(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Normalise a provider payload into a list of item dicts.

    Accepts a bare list or a dict wrapping a list under one of *keys*.
    Non-dict entries are skipped.  An empty result for a dict payload
    raises :class:`IntelProviderInvalidResponseError` (shape mismatch).
    """
    if isinstance(data, list):
        return _filter_dicts(data)
    if isinstance(data, dict):
        data_dict = cast("dict[str, Any]", data)
        for key in keys:
            candidate = data_dict.get(key)
            if isinstance(candidate, list):
                return _filter_dicts(candidate)
        msg = (
            f"provider response missing a list under {keys}: "
            f"keys={sorted(data_dict.keys())}"
        )
        raise IntelProviderInvalidResponseError(msg)
    msg = f"provider response is not a list or dict: {type(data).__name__}"
    raise IntelProviderInvalidResponseError(msg)


def _filter_dicts(raw: Any) -> list[dict[str, Any]]:
    """Return only the dict entries of *raw* as ``dict[str, Any]``."""
    return [
        cast("dict[str, Any]", entry)
        for entry in raw
        if isinstance(entry, dict)
    ]


def _parse_datetime(raw: Any) -> datetime | None:
    """Parse a provider datetime (ISO string, epoch seconds or aware)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _extract_tickers(raw: dict[str, Any]) -> dict[str, str]:
    """Extract candidate security identifiers from a news item."""
    identifiers: dict[str, str] = {}
    for key in ("symbols", "tickers", "related_tickers", "symbol"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            first = value.split(",")[0].strip().upper()
            if first:
                identifiers["ticker"] = first
                break
        elif isinstance(value, list) and value:
            identifiers["ticker"] = str(value[0]).strip().upper()  # type: ignore[arg-type]
            break
    if raw.get("isin"):
        identifiers["isin"] = str(raw["isin"])
    return identifiers


def _clean_meta(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop known secret-shaped keys before persisting provider metadata."""
    forbidden = {"api_key", "apikey", "token", "authorization", "key"}
    return {k: v for k, v in raw.items() if k.lower() not in forbidden}


def _parse_retry_after(raw: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP-date)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        retry_at = parsedate_to_datetime(raw)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
