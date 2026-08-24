"""SEC EDGAR market-intelligence adapter.

Fetches structured, legally reusable corporate events from the US
Securities and Exchange Commission's EDGAR system — the same source the
OpenBB ``sec`` extension uses.  EDGAR filings are **public domain**
(17 CFR 200.735-3 and SEC policy): the full text of filings carries no
copyright, so this adapter is the one source where storing full text is
explicitly allowed.  In practice we still store only metadata,
headlines and structured facts, keeping payloads small and privacy
friendly.

Capabilities:

* ``corporate_events`` — 8-K current reports (material corporate
  events: M&A, leadership changes, bankruptcies, asset disposals…).
* ``earnings`` — 8-K Item 2.02 results announcements (structured
  metadata only: registrant, date, accession number, URL).

The adapter requires **no API key**: EDGAR only asks for a descriptive
User-Agent.  It is rate-limited to 10 requests/second (EDGAR's
published limit) and honours the `Retry-After` header on 403s, as
required by SEC's fair-access policy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

#: SEC EDGAR JSON submissions index for a company (by CIK).
_FILINGS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

#: SEC fair-access: max 10 req/s and a descriptive UA are required.
_USER_AGENT = "finance-sync/0.7.0 contact@example.com"

#: 8-K items that constitute material corporate events (beyond the
#: generic results announcement, Item 2.02).
_EVENT_ITEMS = {
    "1.01": "entry into a material definitive agreement",
    "1.02": "termination of a material definitive agreement",
    "1.03": "bankruptcy or receivership",
    "1.04": "mine safety",
    "2.01": "completion of acquisition or disposition of assets",
    "2.04": (
        "triggering events that accelerate or increase a direct "
        "financial obligation"
    ),
    "2.05": "costs associated with exit or disposal activities",
    "2.06": "material impairments",
    "3.01": (
        "notice of delisting or failure to satisfy a continued listing rule"
    ),
    "3.02": "unregistered sales of equity securities",
    "4.01": "changes in registrant's certifying accountant",
    "4.02": "non-reliance on previously issued financial statements",
    "5.01": "changes in control of registrant",
    "5.02": "departure of directors or certain officers",
    "5.03": "amendments to articles of incorporation or bylaws",
    "5.07": "submission of matters to a vote of security holders",
    "7.01": "regulation FD disclosure",
    "8.01": "other events",
}


class SecEdgarProvider(IntelProvider):
    """Market-intelligence adapter backed by SEC EDGAR public data."""

    provider_key = "sec"
    display_name = "SEC EDGAR"
    license_note = (
        "SEC filings are public domain (17 CFR 200.735-3). finance-sync "
        "stores metadata, headlines and structured facts plus the canonical "
        "EDGAR URL; full filing text is never persisted."
    )
    config_url = "https://www.sec.gov/edgar"

    def __init__(
        self,
        *,
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
                    "Accept": "application/json",
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
        return [IntelCapability.CORPORATE_EVENTS, IntelCapability.EARNINGS]

    async def available(self, capability: IntelCapability) -> IntelAvailability:
        """Return runtime availability for *capability*.

        Performs a lightweight reachability probe against the SEC
        submissions index.  A 403 (rate limited / blocked UA) or a
        timeout yields an explicit ``unavailable``.
        """
        if capability not in (
            IntelCapability.CORPORATE_EVENTS,
            IntelCapability.EARNINGS,
        ):
            return IntelAvailability.UNAVAILABLE
        try:
            await self._rate_limiter.acquire()
            response = await self.http_client.get(
                _FILINGS_URL.format(cik=320193)
            )
            if response.status_code == 200:
                return IntelAvailability.AVAILABLE
            if response.status_code == 403:
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
        """Fetch items for *capability* from SEC EDGAR.

        Requires a ``cik`` or ``ticker`` identifier.  Raises typed
        errors from :mod:`finance_sync.intel.exceptions`.
        """
        cik = _resolve_cik(identifiers)
        if cik is None:
            msg = (
                "SEC EDGAR requires a 'cik' or 'ticker' identifier to "
                "fetch corporate events"
            )
            raise IntelProviderInvalidResponseError(msg)

        try:
            if capability == IntelCapability.CORPORATE_EVENTS:
                return await self._fetch_events(cik, limit=limit)
            if capability == IntelCapability.EARNINGS:
                return await self._fetch_earnings(cik, limit=limit)
            msg = f"capability {capability.value!r} not supported by sec"
            raise IntelProviderInvalidResponseError(msg)
        except httpx.TimeoutException as exc:
            msg = f"SEC EDGAR request timed out after {self._request_timeout}s"
            raise IntelProviderTimeoutError(msg) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retry_after = _parse_retry_after(
                exc.response.headers.get("Retry-After")
            )
            if status == 403:
                msg = (
                    "SEC EDGAR rate limit or blocked User-Agent (403); "
                    "check Retry-After and fair-access policy"
                )
                raise IntelProviderRateLimitError(
                    msg, retry_after=retry_after
                ) from exc
            if status == 429:
                msg = "SEC EDGAR rate limit exceeded (429)"
                raise IntelProviderRateLimitError(
                    msg, retry_after=retry_after
                ) from exc
            if status == 404:
                msg = f"CIK {cik} not found on SEC EDGAR (404)"
                raise IntelProviderInvalidResponseError(msg) from exc
            if status >= 500:
                msg = f"SEC EDGAR upstream error (HTTP {status})"
                raise IntelProviderUnavailableError(msg) from exc
            msg = f"SEC EDGAR returned HTTP {status}"
            raise IntelProviderInvalidResponseError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"SEC EDGAR HTTP request failed: {exc}"
            raise IntelProviderUnavailableError(msg) from exc

    async def _fetch_events(self, cik: str, *, limit: int) -> list[IntelItem]:
        """Fetch 8-K current reports as corporate events."""
        recent = _recent_submissions(await self._get_submissions(cik))
        forms = _str_list(recent.get("form"))
        dates = _str_list(recent.get("filingDate"))
        accessions = _str_list(recent.get("accessionNumber"))
        primary_docs = _str_list(recent.get("primaryDocument"))
        items_raw = _str_list_list(recent.get("items"))

        now = datetime.now(UTC)
        items: list[IntelItem] = []
        for idx, form in enumerate(forms):
            if form != "8-K":
                continue
            if len(items) >= limit:
                break
            items_list = items_raw[idx] if idx < len(items_raw) else []
            if items_list and not _has_event_item(items_list):
                continue
            accession = accessions[idx] if idx < len(accessions) else ""
            primary = primary_docs[idx] if idx < len(primary_docs) else ""
            filing_date = dates[idx] if idx < len(dates) else ""
            if not accession:
                continue
            url = _filing_url(cik, accession, primary)
            published = _parse_filing_date(filing_date)
            items.append(
                IntelItem(
                    provider=self.provider_key,
                    source_id=f"8k-{accession}",
                    canonical_url=url,
                    kind=IntelItemKind.CORPORATE_EVENT,
                    published_at=published or now,
                    fetched_at=now,
                    language="en",
                    license_class=IntelLicenseClass.PUBLIC_DOMAIN,
                    license_uri="https://www.sec.gov/copyright-policy",
                    content_hash=content_hash(
                        {
                            "provider": self.provider_key,
                            "source_id": f"8k-{accession}",
                            "cik": cik,
                        }
                    ),
                    headline=(
                        f"8-K current report ({cik})"
                        + (f" — {', '.join(items_list)}" if items_list else "")
                    ),
                    summary=None,
                    store_full_text=False,
                    store_summary=True,
                    identifiers={"cik": cik},
                    facts=[],
                    provider_metadata={
                        "form": "8-K",
                        "cik": cik,
                        "accession_number": accession,
                        "filing_date": filing_date,
                        "items": items_list,
                    },
                )
            )
        return items

    async def _fetch_earnings(self, cik: str, *, limit: int) -> list[IntelItem]:
        """Fetch 8-K Item 2.02 (results of operations) as earnings items."""
        recent = _recent_submissions(await self._get_submissions(cik))
        forms = _str_list(recent.get("form"))
        dates = _str_list(recent.get("filingDate"))
        accessions = _str_list(recent.get("accessionNumber"))
        primary_docs = _str_list(recent.get("primaryDocument"))
        items_raw = _str_list_list(recent.get("items"))

        now = datetime.now(UTC)
        items: list[IntelItem] = []
        for idx, form in enumerate(forms):
            if form != "8-K":
                continue
            if len(items) >= limit:
                break
            items_list = items_raw[idx] if idx < len(items_raw) else []
            if "2.02" not in items_list:
                continue
            accession = accessions[idx] if idx < len(accessions) else ""
            primary = primary_docs[idx] if idx < len(primary_docs) else ""
            filing_date = dates[idx] if idx < len(dates) else ""
            if not accession:
                continue
            url = _filing_url(cik, accession, primary)
            published = _parse_filing_date(filing_date)
            items.append(
                IntelItem(
                    provider=self.provider_key,
                    source_id=f"8k-results-{accession}",
                    canonical_url=url,
                    kind=IntelItemKind.EARNINGS_REPORT,
                    published_at=published or now,
                    fetched_at=now,
                    language="en",
                    license_class=IntelLicenseClass.PUBLIC_DOMAIN,
                    license_uri="https://www.sec.gov/copyright-policy",
                    content_hash=content_hash(
                        {
                            "provider": self.provider_key,
                            "source_id": f"8k-results-{accession}",
                            "cik": cik,
                        }
                    ),
                    headline=f"{cik} results announcement (8-K Item 2.02)",
                    summary=None,
                    store_full_text=False,
                    store_summary=True,
                    identifiers={"cik": cik},
                    facts=[],
                    provider_metadata={
                        "form": "8-K",
                        "cik": cik,
                        "accession_number": accession,
                        "filing_date": filing_date,
                        "items": items_list,
                    },
                )
            )
        return items

    async def _get_submissions(self, cik: str) -> dict[str, Any]:
        """Fetch and parse the EDGAR submissions index for a CIK."""
        await self._rate_limiter.acquire()
        response = await self.http_client.get(_FILINGS_URL.format(cik=cik))
        response.raise_for_status()
        data: Any = response.json()
        if not isinstance(data, dict):
            msg = (
                f"SEC EDGAR submissions response for CIK {cik} is not an object"
            )
            raise IntelProviderInvalidResponseError(msg)
        return cast("dict[str, Any]", data)

    # ── Defaults ────────────────────────────────────────────────────

    @staticmethod
    def default_rate_limit() -> IntelRateLimit:
        """SEC fair-access limit: 10 requests/second."""
        return IntelRateLimit(max_requests=10, window_seconds=1)

    @staticmethod
    def default_freshness() -> IntelFreshnessPolicy:
        """8-K filings arrive daily; re-fetch at most hourly."""
        return IntelFreshnessPolicy(
            max_age=timedelta(hours=24),
            min_interval=timedelta(hours=1),
        )


# ── Helpers ─────────────────────────────────────────────────────────────


def _recent_submissions(data: dict[str, Any]) -> dict[str, Any]:
    """Return the ``recent`` block of an EDGAR submissions payload."""
    recent = data.get("recent")
    if isinstance(recent, dict):
        return cast("dict[str, Any]", recent)
    return {}


def _str_list(raw: Any) -> list[str]:
    """Normalise an EDGAR column (list of strings) to ``list[str]``."""
    if not isinstance(raw, list):
        return []
    raw_list = cast("list[Any]", raw)
    return [str(entry) for entry in raw_list]


def _str_list_list(raw: Any) -> list[list[str]]:
    """Normalise EDGAR's per-filing ``items`` column to ``list[list[str]]``."""
    if not isinstance(raw, list):
        return []
    raw_rows = cast("list[Any]", raw)
    result: list[list[str]] = []
    for entry in raw_rows:
        if isinstance(entry, list):
            row = cast("list[Any]", entry)
            result.append([str(item) for item in row])
        elif entry is not None:
            result.append([str(entry)])
    return result


def _filing_url(cik: str, accession: str, primary: str) -> str:
    """Build the canonical EDGAR URL for a filing."""
    accession_clean = accession.replace("-", "")
    base = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_clean}"
    )
    return f"{base}/{primary}" if primary else f"{base}/"


def _resolve_cik(identifiers: dict[str, str] | None) -> str | None:
    """Return a 10-digit CIK from identifiers (cik or ticker)."""
    if not identifiers:
        return None
    cik = identifiers.get("cik")
    if cik:
        digits = "".join(ch for ch in cik if ch.isdigit())
        if digits:
            return digits.zfill(10)
    return None


def _parse_filing_date(raw: Any) -> datetime | None:
    """Parse an EDGAR filing date (YYYY-MM-DD)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).replace(tzinfo=UTC)
    except ValueError:
        return None


def _has_event_item(items_list: Any) -> bool:
    """Return True when an 8-K items list contains a material event item."""
    if not items_list:
        return False
    return any(str(item).strip() in _EVENT_ITEMS for item in items_list)


def _parse_retry_after(raw: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP-date).

    Returns ``None`` when absent or unparseable — the caller then falls
    back to its own backoff.
    """
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
