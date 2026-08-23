"""SEC press-releases market-intelligence adapter.

Fetches **legally reusable public news** from the US Securities and
Exchange Commission's press-releases RSS feed
(``https://www.sec.gov/news/pressreleases.rss``).  SEC press releases
are official government publications and therefore **public domain**
(17 CFR 200.735-3 and SEC policy) — the same licensing basis as the
``sec`` EDGAR adapter.  This is the story's required *public news
source*: no API key, no paid subscription, usable by anyone.

Capabilities:

* ``news`` — SEC press releases (enforcement actions, new rules,
  speeches, investor alerts).  Items carry title, short description
  (the RSS summary), the canonical press-release URL, publication date
  and the RSS GUID as the stable source id.

The adapter requires **no API key**; like EDGAR it only asks for a
descriptive User-Agent and honours SEC fair-access limits (10 req/s,
``Retry-After`` on 403/429).

Licensing: SEC press releases are public-domain works, but finance-sync
still stores only metadata, headline and a short snippet plus the
canonical URL — keeping payloads small and privacy friendly.  The
adapter sets ``license_class=public_domain`` and
``license_uri`` pointing at SEC's copyright policy.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import httpx
import structlog

from finance_sync.intel.enums import (
    IntelAvailability,
    IntelCapability,
    IntelItemKind,
    IntelLicenseClass,
)
from finance_sync.intel.exceptions import (
    IntelProviderInvalidResponseError,
    IntelProviderRateLimitError,
    IntelProviderTimeoutError,
    IntelProviderUnavailableError,
)
from finance_sync.intel.hashing import content_hash
from finance_sync.intel.models import IntelItem
from finance_sync.intel.provider import (
    IntelFreshnessPolicy,
    IntelProvider,
    IntelRateLimit,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger(__name__)

#: SEC press-releases RSS feed (official, public-domain announcements).
_PRESS_RSS_URL = "https://www.sec.gov/news/pressreleases.rss"

#: SEC fair-access: max 10 req/s and a descriptive UA are required.
_USER_AGENT = "finance-sync/0.6.0 contact@example.com"


class SecPressReleaseProvider(IntelProvider):
    """Market-intelligence adapter backed by the SEC press-releases RSS feed."""

    provider_key = "sec_press"
    display_name = "SEC Press Releases"
    license_note = (
        "SEC press releases are public-domain official announcements "
        "(17 CFR 200.735-3). finance-sync stores metadata, headline and "
        "a short snippet plus the canonical SEC URL; full press-release "
        "text is never persisted."
    )
    config_url = "https://www.sec.gov/news/pressreleases"

    def __init__(
        self,
        *,
        feed_url: str = _PRESS_RSS_URL,
        user_agent: str = _USER_AGENT,
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
        self._feed_url = feed_url
        self._user_agent = user_agent
        self._request_timeout = request_timeout
        self._http_client: httpx.AsyncClient | None = None

    # ── HTTP client ─────────────────────────────────────────────────

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Lazy-init HTTP client with a SEC-compliant User-Agent."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._request_timeout),
                headers={
                    "User-Agent": self._user_agent,
                    "Accept-Encoding": "gzip, deflate",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                },
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    # ── Capability discovery ────────────────────────────────────────

    async def capabilities(self) -> Sequence[IntelCapability]:
        """Return the capabilities this adapter offers (no key needed)."""
        return [IntelCapability.NEWS]

    async def available(self, capability: IntelCapability) -> IntelAvailability:
        """Return runtime availability for *capability*.

        Performs a lightweight reachability probe against the RSS feed.
        A 403 (rate limited / blocked UA) or a timeout yields an
        explicit ``unavailable``.
        """
        if capability != IntelCapability.NEWS:
            return IntelAvailability.UNAVAILABLE
        try:
            await self._rate_limiter.acquire()
            response = await self.http_client.get(self._feed_url)
            if response.status_code == 200:
                return IntelAvailability.AVAILABLE
            if response.status_code in (403, 429):
                return IntelAvailability.UNAVAILABLE
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
        """Fetch news items from the SEC press-releases RSS feed.

        Raises typed errors from :mod:`finance_sync.intel.exceptions` so
        the scheduler can classify the run.
        """
        del identifiers  # the feed is not security-scoped
        if capability != IntelCapability.NEWS:
            msg = f"capability {capability.value!r} not supported by sec_press"
            raise IntelProviderInvalidResponseError(msg)

        try:
            await self._rate_limiter.acquire()
            response = await self.http_client.get(self._feed_url)
            response.raise_for_status()
            return _parse_feed(response.text, limit=limit)
        except httpx.TimeoutException as exc:
            msg = (
                "SEC press RSS request timed out after "
                f"{self._request_timeout}s"
            )
            raise IntelProviderTimeoutError(msg) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retry_after = _parse_retry_after(
                exc.response.headers.get("Retry-After")
            )
            if status in (403, 429):
                msg = (
                    "SEC press RSS rate limit or blocked User-Agent "
                    f"(HTTP {status}); check Retry-After and fair-access policy"
                )
                raise IntelProviderRateLimitError(
                    msg, retry_after=retry_after
                ) from exc
            if status >= 500:
                msg = f"SEC press RSS upstream error (HTTP {status})"
                raise IntelProviderUnavailableError(msg) from exc
            msg = f"SEC press RSS returned HTTP {status}"
            raise IntelProviderInvalidResponseError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"SEC press RSS HTTP request failed: {exc}"
            raise IntelProviderUnavailableError(msg) from exc

    # ── Defaults ────────────────────────────────────────────────────

    @staticmethod
    def default_rate_limit() -> IntelRateLimit:
        """SEC fair-access limit: 10 requests/second."""
        return IntelRateLimit(max_requests=10, window_seconds=1)

    @staticmethod
    def default_freshness() -> IntelFreshnessPolicy:
        """Press releases arrive throughout the day; refresh hourly."""
        return IntelFreshnessPolicy(
            max_age=timedelta(hours=6),
            min_interval=timedelta(minutes=15),
        )


# ── Parsing helpers ─────────────────────────────────────────────────────


def _parse_feed(xml_text: str, *, limit: int = 20) -> list[IntelItem]:
    """Parse an SEC press-releases RSS payload into :class:`IntelItem`\\ s.

    Returns a list of items with provenance, time, language, licence
    and content-hash fields set.  Skips entries without a title or a
    GUID (they cannot be deduplicated).  Raises
    :class:`IntelProviderInvalidResponseError` when the payload is not
    well-formed XML or has no ``<item>`` entries at all (shape mismatch
    — the source may have changed its format).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        msg = f"SEC press RSS payload is not well-formed XML: {exc}"
        raise IntelProviderInvalidResponseError(msg) from exc

    items: list[IntelItem] = []
    now = datetime.now(UTC)
    for entry in _iter_items(root):
        if len(items) >= limit:
            break
        parsed = _parse_item(entry, now)
        if parsed is not None:
            items.append(parsed)
    return items


def _iter_items(root: ET.Element) -> list[ET.Element]:
    """Return the ``<item>`` children of an RSS channel (any depth)."""
    return [el for el in root.iter() if _local_name(el.tag) == "item"]


def _local_name(tag: str) -> str:
    """Strip the XML namespace prefix from an element tag."""
    return tag.rsplit("}", 1)[-1]


def _parse_item(entry: ET.Element, now: datetime) -> IntelItem | None:
    """Parse one RSS ``<item>`` into an :class:`IntelItem` (or None)."""
    title = _child_text(entry, "title")
    guid = _child_text(entry, "guid") or _child_text(entry, "id")
    link = _child_text(entry, "link")
    if not title or not guid:
        return None

    description = _child_text(entry, "description") or ""
    # Strip embedded HTML from the RSS summary.
    summary = _strip_html(description).strip()
    published = _parse_rss_date(_child_text(entry, "pubDate"))

    hash_input: dict[str, str] = {
        "provider": "sec_press",
        "source_id": guid,
    }
    if link:
        hash_input["url"] = link
    if title:
        hash_input["title"] = title

    return IntelItem(
        provider="sec_press",
        source_id=guid,
        canonical_url=link or None,
        kind=IntelItemKind.NEWS_ARTICLE,
        published_at=published or now,
        fetched_at=now,
        language="en",
        license_class=IntelLicenseClass.PUBLIC_DOMAIN,
        license_uri="https://www.sec.gov/copyright-policy",
        content_hash=content_hash(hash_input),
        headline=title,
        summary=summary[:500] or None,
        store_full_text=False,
        store_summary=True,
        identifiers={},
        facts=[],
        provider_metadata={
            "feed": "press-releases",
            "guid": guid,
            "url": link or None,
        },
    )


def _child_text(entry: ET.Element, name: str) -> str | None:
    """Return the trimmed text of the first child element named *name*."""
    for child in entry:
        if _local_name(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def _strip_html(raw: str) -> str:
    """Strip HTML/XML tags from an RSS summary (best-effort)."""
    return re.sub(r"<[^>]+>", " ", raw)


def _parse_rss_date(raw: str | None) -> datetime | None:
    """Parse an RFC-822 RSS ``pubDate`` into an aware UTC datetime."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


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
        from email.utils import parsedate_to_datetime as _pd

        retry_at = _pd(raw)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
