"""Canonical enums for the market-intelligence source layer.

The provider-independent source layer ("bronlaag") models five
capability kinds (news, corporate events, earnings, analyst estimates,
earnings-call material) plus licensing, availability and resolution
metadata.  All enum members use UPPER_CASE names and are stored as
their ``.value`` (lower-case) in the database, matching the convention
in ``finance_sync.models.enums``.
"""

from __future__ import annotations

from enum import StrEnum


class IntelCapability(StrEnum):
    """Capability kinds a market-intelligence provider may offer.

    A provider advertises the capabilities it can satisfy via
    capability discovery; consumers must never assume a capability
    exists just because the interface defines it.
    """

    NEWS = "news"
    CORPORATE_EVENTS = "corporate_events"
    EARNINGS = "earnings"
    ANALYST_ESTIMATES = "analyst_estimates"
    EARNINGS_CALL = "earnings_call"


class IntelLicenseClass(StrEnum):
    """Reuse class of a source's content, driving what may be stored.

    The ingestion policy is deliberately conservative:

    * ``public_domain`` / ``open_license``  — full content may be stored.
    * ``free_access`` / ``subscriber_only`` — only metadata, short
      snippets and structured facts may be stored, always with a link
      back to the canonical source.
    * ``proprietary``                       — metadata + structured facts
      only; never snippets, never full text.
    """

    PUBLIC_DOMAIN = "public_domain"
    OPEN_LICENSE = "open_license"
    FREE_ACCESS = "free_access"
    SUBSCRIBER_ONLY = "subscriber_only"
    PROPRIETARY = "proprietary"


#: License classes whose full text may be persisted verbatim.
FULL_CONTENT_LICENSE_CLASSES = frozenset(
    {IntelLicenseClass.PUBLIC_DOMAIN, IntelLicenseClass.OPEN_LICENSE}
)

#: License classes for which a short snippet may be persisted.
SNIPPET_LICENSE_CLASSES = frozenset(
    {
        IntelLicenseClass.PUBLIC_DOMAIN,
        IntelLicenseClass.OPEN_LICENSE,
        IntelLicenseClass.FREE_ACCESS,
        IntelLicenseClass.SUBSCRIBER_ONLY,
    }
)


class IntelAvailability(StrEnum):
    """Runtime availability of a provider capability.

    ``unavailable`` is an explicit, persisted state — never the absence
    of a row.  Providers must surface it through capability discovery
    so consumers can distinguish "source down" from "source exists but
    was never run".
    """

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class IntelItemKind(StrEnum):
    """Concrete kind of a stored market-intelligence item."""

    NEWS_ARTICLE = "news_article"
    CORPORATE_EVENT = "corporate_event"
    EARNINGS_REPORT = "earnings_report"
    EARNINGS_CALL_TRANSCRIPT = "earnings_call_transcript"
    ANALYST_ESTIMATE = "analyst_estimate"
    DIVIDEND = "dividend"
    GUIDANCE = "guidance"


class IntelResolutionStatus(StrEnum):
    """Security-identity resolution state of an item.

    Items are matched to a canonical security through the existing
    FIGI/ISIN/ticker/listing pipeline.  Ambiguous matches must never be
    silently attached to a holding — they land in the review queue
    (``unresolved``) instead.
    """

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"
    IGNORED = "ignored"
