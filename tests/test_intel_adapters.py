"""Adapter-level tests for the market-intelligence providers.

Covers the two wired sources:

* ``sec_press`` — SEC press-releases RSS (public-domain news, no key).
* ``openbb``   — OpenBB platform (key-gated; degraded without one).

Each adapter is exercised through the shared :class:`IntelProvider`
contract with ``httpx.MockTransport`` fixtures for the success path,
the rate-limit/retry path (``Retry-After`` honoured, retry budget
exhausted → typed error, no thundering herd) and the unavailable path
(upstream 5xx / timeout → explicit unavailable state).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from finance_sync.intel.adapters.openbb import OpenBBIntelProvider
from finance_sync.intel.adapters.sec_press import SecPressReleaseProvider
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
from tests.fixtures.intel_payloads import (
    SEC_PRESS_RSS_XML,
    openbb_earnings_payload,
    openbb_news_payload,
)

RSS_URL = "https://www.sec.gov/news/pressreleases.rss"


# ── Helpers ────────────────────────────────────────────────────────────


def _press_provider(
    *,
    handler: Any,
    retry_max_attempts: int = 3,
    retry_base_delay: float = 0.01,
) -> SecPressReleaseProvider:
    """Build a SecPressReleaseProvider whose HTTP client uses *handler*."""
    provider = SecPressReleaseProvider(
        retry_max_attempts=retry_max_attempts,
        retry_base_delay=retry_base_delay,
    )
    provider._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.sec.gov",
    )
    return provider


def _openbb_provider(
    *,
    api_key: str | None = "test-key",
    handler: Any,
    retry_max_attempts: int = 3,
    retry_base_delay: float = 0.01,
) -> OpenBBIntelProvider:
    """Build an OpenBBIntelProvider whose HTTP client uses *handler*."""
    provider = OpenBBIntelProvider(
        api_key=api_key,
        retry_max_attempts=retry_max_attempts,
        retry_base_delay=retry_base_delay,
    )
    provider._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openbb.co",
    )
    return provider


def _rss_response() -> httpx.Response:
    return httpx.Response(200, text=SEC_PRESS_RSS_XML)


# ═══════════════════════════════════════════════════════════════════════
# SEC press releases — success path
# ═══════════════════════════════════════════════════════════════════════


class TestSecPressSuccess:
    async def test_fetch_news_parses_items(self) -> None:
        """A well-formed RSS payload yields normalised news items."""
        provider = _press_provider(handler=lambda request: _rss_response())

        items = await provider.fetch(
            IntelCapability.NEWS, identifiers=None, limit=10
        )

        assert len(items) == 3
        first = items[0]
        assert first.provider == "sec_press"
        assert first.source_id == "5ee7718e-9046-432f-ad1f-48d1dd52366f"
        assert first.kind == IntelItemKind.NEWS_ARTICLE
        assert first.canonical_url is not None
        assert first.canonical_url.startswith("https://www.sec.gov/newsroom")
        assert "Tricolor" in (first.headline or "")
        assert first.language == "en"
        assert first.license_class == IntelLicenseClass.PUBLIC_DOMAIN
        assert first.license_uri == "https://www.sec.gov/copyright-policy"
        assert first.store_full_text is False
        assert first.store_summary is True
        # Snippet is the RSS description, char-capped.
        assert first.summary is not None
        assert len(first.summary) <= 500
        # Content hash is present and stable.
        assert len(first.content_hash) == 64
        # Publication time parsed from RFC-822 pubDate (UTC aware).
        assert first.published_at.tzinfo is not None
        assert first.published_at == datetime(
            2026, 8, 18, 19, 55, 10, tzinfo=UTC
        )

    async def test_fetch_news_limit_respected(self) -> None:
        """The limit caps the number of returned items."""
        provider = _press_provider(handler=lambda request: _rss_response())

        items = await provider.fetch(IntelCapability.NEWS, limit=2)
        assert len(items) == 2

    async def test_fetch_news_dedupe_stable_ids(self) -> None:
        """Same feed twice → identical source ids and content hashes."""
        provider = _press_provider(handler=lambda request: _rss_response())

        first = await provider.fetch(IntelCapability.NEWS, limit=10)
        second = await provider.fetch(IntelCapability.NEWS, limit=10)

        assert [i.source_id for i in first] == [i.source_id for i in second]
        assert [i.content_hash for i in first] == [
            i.content_hash for i in second
        ]

    async def test_capabilities_and_availability(self) -> None:
        """The provider advertises NEWS and is available when reachable."""
        provider = _press_provider(handler=lambda request: _rss_response())

        caps = await provider.capabilities()
        assert list(caps) == [IntelCapability.NEWS]

        availability = await provider.available(IntelCapability.NEWS)
        assert availability == IntelAvailability.AVAILABLE

        # Unsupported capability is explicitly unavailable.
        other = await provider.available(IntelCapability.EARNINGS)
        assert other == IntelAvailability.UNAVAILABLE

    async def test_fetch_rejects_unknown_capability(self) -> None:
        """An unsupported capability raises a typed error, never crashes."""
        provider = _press_provider(handler=lambda request: _rss_response())
        with pytest.raises(IntelProviderInvalidResponseError):
            await provider.fetch(IntelCapability.EARNINGS)

    async def test_malformed_xml_is_typed_error(self) -> None:
        """A non-XML payload raises IntelProviderInvalidResponseError."""
        provider = _press_provider(
            handler=lambda request: httpx.Response(200, text="not xml")
        )
        with pytest.raises(IntelProviderInvalidResponseError):
            await provider.fetch(IntelCapability.NEWS)

    async def test_empty_feed_returns_no_items(self) -> None:
        """A valid RSS channel with no items is 'nothing new', not an error."""
        provider = _press_provider(
            handler=lambda request: httpx.Response(
                200, text="<rss><channel><title>X</title></channel></rss>"
            )
        )
        items = await provider.fetch(IntelCapability.NEWS)
        assert items == []


# ═══════════════════════════════════════════════════════════════════════
# SEC press releases — rate limit / retry / unavailable
# ═══════════════════════════════════════════════════════════════════════


class TestSecPressFailure:
    async def test_429_retry_after_respected_and_typed_error(self) -> None:
        """A 429 with Retry-After is retried only after the window."""
        import time

        timestamps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            timestamps.append(time.monotonic())
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                text="rate limited",
            )

        provider = _press_provider(
            handler=handler,
            retry_max_attempts=2,
            retry_base_delay=0.01,
        )
        with pytest.raises(IntelProviderRateLimitError):
            await provider.fetch_with_retry(IntelCapability.NEWS)

        # Two attempts, second only after Retry-After expired.
        assert len(timestamps) == 2
        assert timestamps[1] - timestamps[0] >= 1.7  # jitter tolerance

    async def test_503_is_unavailable_error(self) -> None:
        """An upstream 5xx maps to IntelProviderUnavailableError."""
        provider = _press_provider(
            handler=lambda request: httpx.Response(503, text="down")
        )
        with pytest.raises(IntelProviderUnavailableError):
            await provider.fetch(IntelCapability.NEWS)

    async def test_timeout_is_typed_error(self) -> None:
        """A transport timeout maps to IntelProviderTimeoutError."""
        provider = _press_provider(
            handler=lambda request: (_ for _ in ()).throw(
                httpx.ReadTimeout("timed out")
            )
        )
        with pytest.raises(IntelProviderTimeoutError):
            await provider.fetch(IntelCapability.NEWS)

    async def test_available_reports_unavailable_on_5xx(self) -> None:
        """Availability probe reports degraded when the feed errors 5xx."""
        provider = _press_provider(
            handler=lambda request: httpx.Response(503, text="down")
        )
        assert (
            await provider.available(IntelCapability.NEWS)
            == IntelAvailability.DEGRADED
        )

    async def test_available_reports_unavailable_on_rate_limit(self) -> None:
        """Availability probe reports unavailable when rate limited."""
        provider = _press_provider(
            handler=lambda request: httpx.Response(429, text="slow down")
        )
        assert (
            await provider.available(IntelCapability.NEWS)
            == IntelAvailability.UNAVAILABLE
        )

    async def test_retry_budget_exhaustion_never_returns_empty(self) -> None:
        """After the retry budget is spent the typed error propagates."""
        provider = _press_provider(
            handler=lambda request: httpx.Response(500, text="boom"),
            retry_max_attempts=2,
            retry_base_delay=0.01,
        )
        with pytest.raises(IntelProviderUnavailableError):
            await provider.fetch_with_retry(IntelCapability.NEWS)


# ═══════════════════════════════════════════════════════════════════════
# OpenBB — success path
# ═══════════════════════════════════════════════════════════════════════


class TestOpenBBSuccess:
    async def test_fetch_news_parses_items(self) -> None:
        """A well-formed OpenBB news payload yields normalised items."""
        provider = _openbb_provider(
            handler=lambda request: httpx.Response(
                200, json=openbb_news_payload()
            )
        )

        items = await provider.fetch(
            IntelCapability.NEWS,
            identifiers={"ticker": "AAPL"},
            limit=10,
        )

        assert len(items) == 2
        first = items[0]
        assert first.provider == "openbb"
        assert first.source_id == "news-12345"
        assert first.canonical_url == "https://example.com/news/12345"
        assert first.kind == IntelItemKind.NEWS_ARTICLE
        assert first.license_class == IntelLicenseClass.FREE_ACCESS
        assert first.store_full_text is False
        assert first.store_summary is True
        assert first.identifiers == {"ticker": "AAPL"}
        assert len(first.content_hash) == 64

    async def test_fetch_earnings_parses_facts(self) -> None:
        """Earnings items carry structured facts (EPS/revenue estimates)."""
        provider = _openbb_provider(
            handler=lambda request: httpx.Response(
                200, json=openbb_earnings_payload()
            )
        )

        items = await provider.fetch(
            IntelCapability.EARNINGS,
            identifiers={"ticker": "AAPL"},
            limit=10,
        )

        assert len(items) == 2
        first = items[0]
        assert first.kind == IntelItemKind.ANALYST_ESTIMATE
        assert first.identifiers == {"ticker": "AAPL"}
        fact_keys = {f.key for f in first.facts}
        assert {"eps_estimate", "revenue_estimate"} <= fact_keys
        assert first.store_full_text is False

    async def test_no_key_is_degraded(self) -> None:
        """Without a key the provider reports no capabilities + unavailable."""
        provider = OpenBBIntelProvider(api_key=None)
        assert await provider.capabilities() == []
        assert (
            await provider.available(IntelCapability.NEWS)
            == IntelAvailability.UNAVAILABLE
        )
        with pytest.raises(IntelProviderAuthError):
            await provider.fetch(IntelCapability.NEWS)

    async def test_401_is_auth_error(self) -> None:
        """A 401 maps to IntelProviderAuthError."""
        provider = _openbb_provider(
            handler=lambda request: httpx.Response(401, text="unauthorized")
        )
        with pytest.raises(IntelProviderAuthError):
            await provider.fetch(IntelCapability.NEWS)

    async def test_malformed_payload_is_typed_error(self) -> None:
        """A payload with no expected list raises a typed error."""
        provider = _openbb_provider(
            handler=lambda request: httpx.Response(200, json={"unexpected": 1})
        )
        with pytest.raises(IntelProviderInvalidResponseError):
            await provider.fetch(IntelCapability.NEWS)


# ═══════════════════════════════════════════════════════════════════════
# OpenBB — rate limit / retry / unavailable
# ═══════════════════════════════════════════════════════════════════════


class TestOpenBBFailure:
    async def test_429_retry_after_respected(self) -> None:
        """429 with Retry-After is retried only after the window."""
        import time

        timestamps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            timestamps.append(time.monotonic())
            return httpx.Response(
                429,
                headers={"Retry-After": "1"},
                text="rate limited",
            )

        provider = _openbb_provider(
            handler=handler,
            retry_max_attempts=2,
            retry_base_delay=0.01,
        )
        with pytest.raises(IntelProviderRateLimitError):
            await provider.fetch_with_retry(IntelCapability.NEWS)

        assert len(timestamps) == 2
        assert timestamps[1] - timestamps[0] >= 0.7

    async def test_5xx_is_unavailable_error(self) -> None:
        """Upstream 5xx maps to IntelProviderUnavailableError."""
        provider = _openbb_provider(
            handler=lambda request: httpx.Response(502, text="bad gateway")
        )
        with pytest.raises(IntelProviderUnavailableError):
            await provider.fetch(IntelCapability.NEWS)

    async def test_timeout_is_typed_error(self) -> None:
        """A transport timeout maps to IntelProviderTimeoutError."""
        provider = _openbb_provider(
            handler=lambda request: (_ for _ in ()).throw(
                httpx.ConnectTimeout("no route")
            )
        )
        with pytest.raises(IntelProviderTimeoutError):
            await provider.fetch(IntelCapability.NEWS)

    async def test_retry_budget_exhaustion_never_returns_empty(self) -> None:
        """After the retry budget is spent the typed error propagates."""
        provider = _openbb_provider(
            handler=lambda request: httpx.Response(503, text="down"),
            retry_max_attempts=2,
            retry_base_delay=0.01,
        )
        with pytest.raises(IntelProviderUnavailableError):
            await provider.fetch_with_retry(IntelCapability.NEWS)
