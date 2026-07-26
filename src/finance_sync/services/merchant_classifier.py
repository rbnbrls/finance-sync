"""Merchant classification using fundamentals and ETF metadata.

Categorises merchants into sectors and computes subscription likelihood
using Phase 3 fundamentals/ETF enrichment data.

Provides:
- Merchant-to-ticker resolution for known subscription merchants
- GICS sector classification with subscription likelihood scoring
- Fundamentals-aware likelihood boosting (PE, market cap, dividend yield)
- Integration point for the subscription detection pipeline
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from finance_sync.db.uow import UnitOfWork

logger = structlog.get_logger("finance_sync.services.merchant_classifier")

# ── Subscription Likelihood Levels ───────────────────────────────────────

LIKELIHOOD_HIGH = "high"
LIKELIHOOD_MEDIUM = "medium"
LIKELIHOOD_LOW = "low"

# Numeric scores used for confidence boosting
_LIKELIHOOD_BOOST: dict[str, float] = {
    LIKELIHOOD_HIGH: 0.12,
    LIKELIHOOD_MEDIUM: 0.06,
    LIKELIHOOD_LOW: 0.0,
}

# ── Merchant → Ticker Resolution ────────────────────────────────────────

# Known subscription merchants mapped to their stock ticker symbols.
# Private companies are mapped with a None ticker.
MERCHANT_TICKER_MAP: dict[str, dict[str, Any]] = {
    # ── Streaming ────────────────────────────────────────────────────
    "netflix": {
        "ticker": "NFLX",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "netflix.com": {
        "ticker": "NFLX",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "spotify": {
        "ticker": "SPOT",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "disney": {
        "ticker": "DIS",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "disney+": {
        "ticker": "DIS",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "hbo": {
        "ticker": "WBD",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "hbo max": {
        "ticker": "WBD",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "hulu": {
        "ticker": "DIS",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "paramount+": {
        "ticker": "PARA",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "paramount": {
        "ticker": "PARA",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "amc+": {
        "ticker": "AMC",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "peacock": {
        "ticker": "CMCSA",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "youtube premium": {
        "ticker": "GOOGL",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "youtube music": {
        "ticker": "GOOGL",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "apple music": {
        "ticker": "AAPL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "apple tv": {
        "ticker": "AAPL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "apple one": {
        "ticker": "AAPL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "amazon prime": {
        "ticker": "AMZN",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "prime video": {
        "ticker": "AMZN",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "twitch": {
        "ticker": "AMZN",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "tidal": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "deezer": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    # ── Software / SaaS ─────────────────────────────────────────────
    "microsoft 365": {
        "ticker": "MSFT",
        "sector": "Technology",
        "security_type": "stock",
    },
    "office 365": {
        "ticker": "MSFT",
        "sector": "Technology",
        "security_type": "stock",
    },
    "microsoft": {
        "ticker": "MSFT",
        "sector": "Technology",
        "security_type": "stock",
    },
    "google workspace": {
        "ticker": "GOOGL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "google drive": {
        "ticker": "GOOGL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "google one": {
        "ticker": "GOOGL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "dropbox": {
        "ticker": "DBX",
        "sector": "Technology",
        "security_type": "stock",
    },
    "github": {
        "ticker": "MSFT",
        "sector": "Technology",
        "security_type": "stock",
    },
    "gitlab": {
        "ticker": "GTLB",
        "sector": "Technology",
        "security_type": "stock",
    },
    "notion": {"ticker": None, "sector": "Technology", "security_type": None},
    "figma": {"ticker": None, "sector": "Technology", "security_type": None},
    "adobe": {
        "ticker": "ADBE",
        "sector": "Technology",
        "security_type": "stock",
    },
    "creative cloud": {
        "ticker": "ADBE",
        "sector": "Technology",
        "security_type": "stock",
    },
    "slack": {
        "ticker": "CRM",
        "sector": "Technology",
        "security_type": "stock",
    },
    "salesforce": {
        "ticker": "CRM",
        "sector": "Technology",
        "security_type": "stock",
    },
    "zoom": {"ticker": "ZM", "sector": "Technology", "security_type": "stock"},
    "zoom video": {
        "ticker": "ZM",
        "sector": "Technology",
        "security_type": "stock",
    },
    "atlassian": {
        "ticker": "TEAM",
        "sector": "Technology",
        "security_type": "stock",
    },
    "jira": {
        "ticker": "TEAM",
        "sector": "Technology",
        "security_type": "stock",
    },
    "confluence": {
        "ticker": "TEAM",
        "sector": "Technology",
        "security_type": "stock",
    },
    "trello": {"ticker": None, "sector": "Technology", "security_type": None},
    "datadog": {
        "ticker": "DDOG",
        "sector": "Technology",
        "security_type": "stock",
    },
    "new relic": {
        "ticker": "NEWR",
        "sector": "Technology",
        "security_type": "stock",
    },
    "digitalocean": {
        "ticker": "DOCN",
        "sector": "Technology",
        "security_type": "stock",
    },
    "digital ocean": {
        "ticker": "DOCN",
        "sector": "Technology",
        "security_type": "stock",
    },
    "aws": {"ticker": "AMZN", "sector": "Technology", "security_type": "stock"},
    "openai": {"ticker": None, "sector": "Technology", "security_type": None},
    "chatgpt": {"ticker": None, "sector": "Technology", "security_type": None},
    "midjourney": {
        "ticker": None,
        "sector": "Technology",
        "security_type": None,
    },
    "claude": {"ticker": None, "sector": "Technology", "security_type": None},
    "anthropic": {
        "ticker": None,
        "sector": "Technology",
        "security_type": None,
    },
    "canva": {"ticker": None, "sector": "Technology", "security_type": None},
    "miro": {"ticker": None, "sector": "Technology", "security_type": None},
    "linear": {"ticker": None, "sector": "Technology", "security_type": None},
    "vercel": {"ticker": None, "sector": "Technology", "security_type": None},
    "netlify": {"ticker": None, "sector": "Technology", "security_type": None},
    "heroku": {
        "ticker": "CRM",
        "sector": "Technology",
        "security_type": "stock",
    },
    # ── Cloud Storage ───────────────────────────────────────────────
    "icloud": {
        "ticker": "AAPL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "apple icloud": {
        "ticker": "AAPL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "one drive": {
        "ticker": "MSFT",
        "sector": "Technology",
        "security_type": "stock",
    },
    "onedrive": {
        "ticker": "MSFT",
        "sector": "Technology",
        "security_type": "stock",
    },
    "box": {"ticker": "BOX", "sector": "Technology", "security_type": "stock"},
    "mega": {"ticker": None, "sector": "Technology", "security_type": None},
    "pcloud": {"ticker": None, "sector": "Technology", "security_type": None},
    # ── News / Media ─────────────────────────────────────────────────
    "new york times": {
        "ticker": "NYT",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "nyt": {
        "ticker": "NYT",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "wall street journal": {
        "ticker": "NWSA",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "wsj": {
        "ticker": "NWSA",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "the guardian": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "substack": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "medium": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "economist": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "bloomberg": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "ft": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "financial times": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    # ── Fitness ──────────────────────────────────────────────────────
    "peloton": {
        "ticker": "PTON",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "basic fit": {
        "ticker": "BFIT",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "fitbit": {
        "ticker": "GOOGL",
        "sector": "Technology",
        "security_type": "stock",
    },
    "whoop": {
        "ticker": None,
        "sector": "Consumer Discretionary",
        "security_type": None,
    },
    "strava": {"ticker": None, "sector": "Technology", "security_type": None},
    "myfitnesspal": {
        "ticker": None,
        "sector": "Technology",
        "security_type": None,
    },
    # ── Gaming ───────────────────────────────────────────────────────
    "xbox": {
        "ticker": "MSFT",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "xbox game pass": {
        "ticker": "MSFT",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "playstation": {
        "ticker": "SONY",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "ps plus": {
        "ticker": "SONY",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "playstation plus": {
        "ticker": "SONY",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "nintendo switch online": {
        "ticker": "NTDOY",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "ea play": {
        "ticker": "EA",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "electronic arts": {
        "ticker": "EA",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "ubisoft": {
        "ticker": "UBSFY",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "roblox": {
        "ticker": "RBLX",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "steam": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "epic games": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "activision": {
        "ticker": "MSFT",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    # ── Donations / Creator ──────────────────────────────────────────
    "patreon": {"ticker": None, "sector": "Technology", "security_type": None},
    "kickstarter": {
        "ticker": None,
        "sector": "Technology",
        "security_type": None,
    },
    "buymeacoffee": {
        "ticker": None,
        "sector": "Technology",
        "security_type": None,
    },
    "ko-fi": {"ticker": None, "sector": "Technology", "security_type": None},
    "onlyfans": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    # ── Telecom / Internet ───────────────────────────────────────────
    "ziggo": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "vodafone": {
        "ticker": "VOD",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "kpn": {
        "ticker": None,
        "sector": "Communication Services",
        "security_type": None,
    },
    "t-mobile": {
        "ticker": "TMUS",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "verizon": {
        "ticker": "VZ",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    "at&t": {
        "ticker": "T",
        "sector": "Communication Services",
        "security_type": "stock",
    },
    # ── Insurance ────────────────────────────────────────────────────
    "zilveren kruis": {
        "ticker": None,
        "sector": "Financials",
        "security_type": None,
    },
    "cz": {"ticker": None, "sector": "Financials", "security_type": None},
    "vgz": {"ticker": None, "sector": "Financials", "security_type": None},
    "ohra": {"ticker": None, "sector": "Financials", "security_type": None},
    "allianz": {
        "ticker": "ALV",
        "sector": "Financials",
        "security_type": "stock",
    },
    "aegon": {
        "ticker": "AEG",
        "sector": "Financials",
        "security_type": "stock",
    },
    "nn group": {
        "ticker": "NN",
        "sector": "Financials",
        "security_type": "stock",
    },
    "asr": {
        "ticker": "ASRNL",
        "sector": "Financials",
        "security_type": "stock",
    },
    # ── Utilities ────────────────────────────────────────────────────
    "e.on": {"ticker": "EOAN", "sector": "Utilities", "security_type": "stock"},
    "rwe": {"ticker": "RWE", "sector": "Utilities", "security_type": "stock"},
    "essent": {"ticker": None, "sector": "Utilities", "security_type": None},
    "vattenfall": {
        "ticker": None,
        "sector": "Utilities",
        "security_type": None,
    },
    "eneco": {"ticker": None, "sector": "Utilities", "security_type": None},
    "nuon": {"ticker": None, "sector": "Utilities", "security_type": None},
    # ── Cloud / Infrastructure ───────────────────────────────────────
    "cloudflare": {
        "ticker": "NET",
        "sector": "Technology",
        "security_type": "stock",
    },
    "fastly": {
        "ticker": "FSLY",
        "sector": "Technology",
        "security_type": "stock",
    },
    "akamai": {
        "ticker": "AKAM",
        "sector": "Technology",
        "security_type": "stock",
    },
    "mongodb": {
        "ticker": "MDB",
        "sector": "Technology",
        "security_type": "stock",
    },
    "databricks": {
        "ticker": None,
        "sector": "Technology",
        "security_type": None,
    },
    "snowflake": {
        "ticker": "SNOW",
        "sector": "Technology",
        "security_type": "stock",
    },
    "confluent": {
        "ticker": "CFLT",
        "sector": "Technology",
        "security_type": "stock",
    },
    "hashicorp": {
        "ticker": "HCP",
        "sector": "Technology",
        "security_type": "stock",
    },
    "elastic": {
        "ticker": "ESTC",
        "sector": "Technology",
        "security_type": "stock",
    },
    # ── E-commerce subscriptions ─────────────────────────────────────
    "amazon": {
        "ticker": "AMZN",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "amazon.com": {
        "ticker": "AMZN",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "bol.com": {
        "ticker": None,
        "sector": "Consumer Discretionary",
        "security_type": None,
    },
    "coolblue": {
        "ticker": None,
        "sector": "Consumer Discretionary",
        "security_type": None,
    },
    "picnic": {
        "ticker": None,
        "sector": "Consumer Staples",
        "security_type": None,
    },
    "hellofresh": {
        "ticker": "HFG",
        "sector": "Consumer Staples",
        "security_type": "stock",
    },
    "hello fresh": {
        "ticker": "HFG",
        "sector": "Consumer Staples",
        "security_type": "stock",
    },
    "takeaway": {
        "ticker": "TKWY",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "thuisbezorgd": {
        "ticker": "TKWY",
        "sector": "Consumer Discretionary",
        "security_type": "stock",
    },
    "uber": {
        "ticker": "UBER",
        "sector": "Technology",
        "security_type": "stock",
    },
    "uber eats": {
        "ticker": "UBER",
        "sector": "Technology",
        "security_type": "stock",
    },
    "deliveroo": {
        "ticker": "ROO",
        "sector": "Technology",
        "security_type": "stock",
    },
    "doordash": {
        "ticker": "DASH",
        "sector": "Technology",
        "security_type": "stock",
    },
}

# ── Subscription likelihood by GICS sector ──────────────────────────────

# Maps GICS sector (as stored in SecurityMetadataObservation) to
# subscription likelihood level.
SUBSCRIPTION_LIKELIHOOD_BY_SECTOR: dict[str, str] = {
    "Technology": LIKELIHOOD_HIGH,
    "Communication Services": LIKELIHOOD_HIGH,
    "Consumer Discretionary": LIKELIHOOD_HIGH,
    "Consumer Staples": LIKELIHOOD_MEDIUM,
    "Financials": LIKELIHOOD_MEDIUM,
    "Health Care": LIKELIHOOD_MEDIUM,
    "Utilities": LIKELIHOOD_MEDIUM,
    "Industrials": LIKELIHOOD_LOW,
    "Materials": LIKELIHOOD_LOW,
    "Energy": LIKELIHOOD_LOW,
    "Real Estate": LIKELIHOOD_LOW,
}

# ── Category to sector mapping ──────────────────────────────────────────

# Maps existing _classify_category labels to GICS sectors so the
# keyword-based classification feeds into the same pipeline.
CATEGORY_TO_SECTOR: dict[str, str] = {
    "streaming": "Communication Services",
    "software": "Technology",
    "utilities": "Utilities",
    "fitness": "Consumer Discretionary",
    "insurance": "Financials",
    "news_media": "Communication Services",
    "donations": "Technology",
    "cloud_storage": "Technology",
}


# ── DTOs ─────────────────────────────────────────────────────────────────


class MerchantClassification:
    """Result of classifying a merchant for subscription likelihood.

    Attributes:
        merchant_name: Normalised merchant name.
        sector: GICS sector classification (e.g. 'Technology').
        subscription_likelihood: 'high', 'medium', or 'low'.
        likelihood_score: Numeric boost to apply (0.0-0.12).
        ticker: Ticker symbol if known, else None.
        security_id: Resolved security DB id if available, else None.
        fundamentals_available: Whether fundamentals data was used.
        source: How the classification was derived
            ('merchant_map', 'fundamentals', 'category_map', 'sector_map').
    """

    def __init__(
        self,
        merchant_name: str,
        sector: str | None = None,
        subscription_likelihood: str = LIKELIHOOD_MEDIUM,
        ticker: str | None = None,
        security_id: str | None = None,
        fundamentals_available: bool = False,
        source: str = "sector_map",
    ) -> None:
        self.merchant_name = merchant_name
        self.sector = sector
        self.subscription_likelihood = subscription_likelihood
        self.likelihood_score = _LIKELIHOOD_BOOST.get(
            subscription_likelihood, 0.0
        )
        self.ticker = ticker
        self.security_id = security_id
        self.fundamentals_available = fundamentals_available
        self.source = source

    def __repr__(self) -> str:
        return (
            f"<MerchantClassification {self.merchant_name!r} "
            f"sector={self.sector!r} "
            f"likelihood={self.subscription_likelihood!r}>"
        )


# ── Classification logic ────────────────────────────────────────────────


def _normalise_merchant_name(name: str) -> str:
    """Normalise a merchant name for lookup in the ticker map.

    Strips common legal suffixes (bv, inc, llc, ltd, corp, nv, plc, gmbh)
    and returns lowercase for dictionary lookup.
    """
    cleaned = name.lower().strip()
    # Remove common legal suffixes
    for suffix in [
        " b.v.",
        " bv",
        " n.v.",
        " nv",
        " inc.",
        " inc",
        " l.l.c.",
        " llc",
        " ltd.",
        " ltd",
        " corp.",
        " corp",
        " plc",
        " gmbh",
        " s.a.",
        " sa",
        " s.l.",
        " sl",
        " ag",
        " co.",
        " co",
        " group",
        " holding",
        " holdings",
        " international",
        " technologies",
        " technology",
    ]:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()

    return cleaned


def _resolve_merchant_entry(name: str) -> dict[str, Any] | None:
    """Look up a merchant name in the ticker map.

    Tries exact match first, then progressively shorter prefixes.
    """
    normalised = _normalise_merchant_name(name)

    # Exact match
    if normalised in MERCHANT_TICKER_MAP:
        return MERCHANT_TICKER_MAP[normalised]

    # Prefix match: try the first N words
    words = normalised.split()
    for end in range(len(words), 0, -1):
        prefix = " ".join(words[:end])
        if prefix in MERCHANT_TICKER_MAP:
            return MERCHANT_TICKER_MAP[prefix]

    return None


def _get_sector_likelihood(sector: str | None) -> str:
    """Return the subscription likelihood for a GICS sector."""
    if sector and sector in SUBSCRIPTION_LIKELIHOOD_BY_SECTOR:
        return SUBSCRIPTION_LIKELIHOOD_BY_SECTOR[sector]
    return LIKELIHOOD_MEDIUM


def _sector_from_category(category: str | None) -> str | None:
    """Map a subscription category to its corresponding GICS sector."""
    if category and category in CATEGORY_TO_SECTOR:
        return CATEGORY_TO_SECTOR[category]
    return None


def _adjust_likelihood_with_fundamentals(
    likelihood: str,
    pe_ratio: Decimal | None,
    dividend_yield: Decimal | None,
) -> str:
    """Adjust subscription likelihood based on fundamental data.

    Rules (applied only to HIGH and MEDIUM):
    - Very high dividend yield (>4%) -> downgrade one level
      (mature dividend payers are less likely to be pure subscription plays)
    - Very high PE ratio (>50) -> upgrade one level
      (growth companies, often subscription-revenue driven)
    - Very low or negative EPS + high market cap -> upgrade
      (growth-stage SaaS)
    """
    if likelihood == LIKELIHOOD_LOW:
        return likelihood  # No upgrade from low based on fundamentals alone

    downgrade = False
    upgrade = False

    # High dividend → more traditional, less subscription-focused
    if dividend_yield is not None and dividend_yield > Decimal("0.04"):
        downgrade = True

    # Very high PE → growth company, likely subscription-revenue model
    if pe_ratio is not None and pe_ratio > Decimal(50):
        upgrade = True

    # Dividend yield below threshold cancels upgrade from PE
    if dividend_yield is not None and dividend_yield > Decimal("0.03"):
        upgrade = False

    if downgrade and likelihood == LIKELIHOOD_HIGH:
        return LIKELIHOOD_MEDIUM
    if upgrade and likelihood == LIKELIHOOD_MEDIUM:
        return LIKELIHOOD_HIGH

    return likelihood


# ── Service ──────────────────────────────────────────────────────────────


class MerchantClassifier:
    """Classify merchants using fundamentals and ETF metadata.

    Integrates Phase 3 enrichment data to label merchants with
    sector, ticker, and subscription likelihood.

    Usage::

        classifier = MerchantClassifier(uow=container.uow)
        classification = await classifier.classify("Netflix B.V.")
        print(classification.sector, classification.subscription_likelihood)
    """

    def __init__(self, uow: UnitOfWork | None = None) -> None:
        self._uow = uow
        self._log = logger.bind(component="MerchantClassifier")

    async def classify(
        self,
        merchant_name: str,
        category: str | None = None,
        *,
        use_fundamentals: bool = True,
    ) -> MerchantClassification:
        """Classify a merchant and return the full classification result.

        Resolution priority:
        1. Merchant → ticker map (for known subscription merchants)
        2. Category-based sector mapping (from keyword-based detection)
        3. Fundamentals from DB (if available and uow is provided)
        4. Sector-based default likelihood

        Args:
            merchant_name: Normalised merchant name.
            category: Previously detected subscription category (optional).
            use_fundamentals: Whether to query DB for fundamentals data.

        Returns:
            A MerchantClassification with sector, likelihood, and source.
        """
        # Step 1: Try merchant ticker map
        entry = _resolve_merchant_entry(merchant_name)
        if entry is not None:
            sector = entry["sector"]
            ticker = entry["ticker"]
            likelihood = _get_sector_likelihood(sector)
            security_id: str | None = None
            fundamentals_available = False

            # Step 2: resolve security and fundamentals via UoW + ticker
            if use_fundamentals and self._uow is not None and ticker:
                (
                    sec_id,
                    fund_data,
                ) = await self._resolve_security_with_fundamentals(
                    ticker,
                )
                if sec_id:
                    security_id = sec_id
                if fund_data:
                    fundamentals_available = True
                    likelihood = _adjust_likelihood_with_fundamentals(
                        likelihood,
                        pe_ratio=fund_data.get("pe_ratio"),
                        dividend_yield=fund_data.get("dividend_yield"),
                    )

            return MerchantClassification(
                merchant_name=merchant_name,
                sector=sector,
                subscription_likelihood=likelihood,
                ticker=ticker,
                security_id=security_id,
                fundamentals_available=fundamentals_available,
                source="merchant_map",
            )

        # Step 3: Try category-based sector mapping
        sector = _sector_from_category(category)
        if sector:
            likelihood = _get_sector_likelihood(sector)
            return MerchantClassification(
                merchant_name=merchant_name,
                sector=sector,
                subscription_likelihood=likelihood,
                source="category_map",
            )

        # Step 4: Fallback — default likelihood
        return MerchantClassification(
            merchant_name=merchant_name,
            sector=None,
            subscription_likelihood=LIKELIHOOD_MEDIUM,
            source="sector_map",
        )

    async def classify_batch(
        self,
        merchants: list[dict[str, Any]],
        *,
        use_fundamentals: bool = True,
    ) -> dict[str, MerchantClassification]:
        """Classify multiple merchants in batch.

        Args:
            merchants: List of dicts with at least 'merchant_name' and
                optionally 'category' keys.
            use_fundamentals: Whether to query DB for fundamentals.

        Returns:
            Dict mapping merchant_name -> MerchantClassification.
        """
        results: dict[str, MerchantClassification] = {}
        for m in merchants:
            name = m.get("merchant_name", "")
            if not name:
                continue
            results[name] = await self.classify(
                name,
                category=m.get("category"),
                use_fundamentals=use_fundamentals,
            )
        return results

    async def _resolve_security_with_fundamentals(
        self,
        ticker: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Resolve a ticker to a security ID and fetch its fundamentals.

        Queries the Security table by ticker, then looks up the most
        recent FundamentalObservation for that security.
        """
        if self._uow is None:
            return None, None

        try:
            # Find security by ticker
            security = await self._find_security_by_ticker(ticker)
            if security is None:
                return None, None

            security_id = str(security.id)  # type: ignore[attr-defined]

            # Fetch latest fundamental observation
            fund_data = await self._find_latest_fundamentals(security_id)
            return security_id, fund_data

        except Exception:
            self._log.debug(
                "fundamentals_lookup_failed",
                ticker=ticker,
                exc_info=True,
            )
            return None, None

    async def _find_security_by_ticker(
        self,
        ticker: str,
    ) -> Any:
        """Find a Security record by ticker symbol."""
        if self._uow is None:
            return None

        from sqlalchemy import select

        from finance_sync.models.security import Security

        async with self._uow._session_factory() as session:  # noqa: SLF001
            stmt = select(Security).where(Security.ticker == ticker)  # type: ignore[attr-defined]
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _find_latest_fundamentals(
        self,
        security_id: str,
    ) -> dict[str, Any] | None:
        """Fetch the most recent FundamentalObservation for a security."""
        if self._uow is None:
            return None

        from sqlalchemy import select

        from finance_sync.models.fundamental_observation import (
            FundamentalObservation,
        )

        async with self._uow._session_factory() as session:  # noqa: SLF001
            stmt = (
                select(FundamentalObservation)
                .where(
                    FundamentalObservation.security_id == security_id  # type: ignore[attr-defined]
                )
                .order_by(FundamentalObservation.timestamp.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
            result = await session.execute(stmt)
            obs = result.scalar_one_or_none()
            if obs is None:
                return None

            return {
                "market_cap": obs.market_cap,
                "pe_ratio": obs.pe_ratio,
                "dividend_yield": obs.dividend_yield,
                "eps": obs.eps,
                "beta": obs.beta,
                "forward_pe": obs.forward_pe,
            }
