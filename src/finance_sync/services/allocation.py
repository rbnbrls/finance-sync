"""
Allocation service — portfolio breakdowns by asset class, sector, and region.

Provides methods to compute how a tenant's portfolio is allocated across
different dimensions, enabling analysis like "what % is in equities" or
"how much is exposed to the Technology sector".

Multi-currency support
----------------------
Holdings can be denominated in different currencies (EUR, USD, etc.).
When ``target_currency`` is provided, the service converts all values
to that currency using the ``FxService``.  When omitted, raw values are
reported in their native currency (mixed-currency response).

Data sources
------------
- ``Holding`` records for position snapshots (latest per account+security)
- ``Security`` records for asset-class classification (security_type)
- ``SecurityMetadataObservation`` with ``metadata_type='sector_exposure'``
  for sector / region classification
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select

from finance_sync.enrichment.models import FxConversionRequest
from finance_sync.models.account import Account
from finance_sync.models.holding import Holding
from finance_sync.models.security import Security
from finance_sync.models.security_metadata_observation import (
    SecurityMetadataObservation,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.services.fx_service import FxService

# ── Constants ──────────────────────────────────────────────────────────

E = Decimal
_ZERO = E("0")
_UNCLASSIFIED = "Unclassified"

# ── Response models ────────────────────────────────────────────────────


class AllocationBucket(BaseModel):
    """A single allocation category with its value and weight."""

    name: str = Field(description="Category name (e.g. 'Technology', 'stock')")
    value: E = Field(description="Total market value in this category")
    percentage: E = Field(description="Percentage of total portfolio (0-100)")


class AssetClassBreakdown(BaseModel):
    """Breakdown by asset class (security_type)."""

    items: list[AllocationBucket] = Field(
        default_factory=list,
        description="Allocation by asset class",
    )
    total_value: E = Field(description="Sum of all asset-class values")


class SectorBreakdown(BaseModel):
    """Breakdown by sector."""

    items: list[AllocationBucket] = Field(
        default_factory=list,
        description="Allocation by sector",
    )
    total_value: E = Field(description="Sum of all sector values")


class RegionBreakdown(BaseModel):
    """Breakdown by geographic region."""

    items: list[AllocationBucket] = Field(
        default_factory=list,
        description="Allocation by region",
    )
    total_value: E = Field(description="Sum of all region values")


class AccountAllocationBreakdown(BaseModel):
    """Per-account allocation breakdown."""

    account_id: str
    account_name: str
    account_type: str
    total_value: E = _ZERO
    by_asset_class: list[AllocationBucket] = Field(default_factory=list)
    by_sector: list[AllocationBucket] = Field(default_factory=list)
    by_region: list[AllocationBucket] = Field(default_factory=list)


class AllocationResponse(BaseModel):
    """Top-level allocation summary for a tenant's portfolio."""

    total_value: E = _ZERO
    currency_code: str = "EUR"
    by_asset_class: list[AllocationBucket] = Field(
        default_factory=list,
        description="Portfolio breakdown by asset class",
    )
    by_sector: list[AllocationBucket] = Field(
        default_factory=list,
        description="Portfolio breakdown by sector",
    )
    by_region: list[AllocationBucket] = Field(
        default_factory=list,
        description="Portfolio breakdown by region",
    )
    accounts: list[AccountAllocationBreakdown] = Field(
        default_factory=list,
        description="Per-account breakdowns",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Timestamp of the holding data used",
    )


# ── Service ────────────────────────────────────────────────────────────


class AllocationService:
    """Provides portfolio allocation breakdowns.

    Each method returns Pydantic response models.  Methods are async and
    operate on an async SQLAlchemy session.  Multi-currency conversion is
    supported via an optional ``FxService`` dependency.
    """

    def __init__(
        self,
        session: AsyncSession,
        fx_service: FxService | None = None,
    ) -> None:
        self._session = session
        self._fx_service = fx_service

    # ── Public API ────────────────────────────────────────────────────

    async def get_allocation(
        self,
        tenant_id: str,
        *,
        target_currency: str | None = None,
        account_id: str | None = None,
    ) -> AllocationResponse:
        """Compute allocation by asset class, sector, and region.

        Args:
            tenant_id: Tenant to scope the query.
            target_currency: Optional ISO-4217 code to normalise values
                into a single currency.  When omitted, holdings report
                their native currency values (may be mixed).
            account_id: Optional filter to a single account.

        Returns:
            An ``AllocationResponse`` with breakdowns.
        """
        # 1. Gather the latest holding snapshots + securities + accounts
        portfolio_data = await self._load_portfolio_data(
            tenant_id, account_id=account_id
        )
        if not portfolio_data:
            now = datetime.now(UTC)
            return AllocationResponse(as_of=now)

        holdings_list, security_map, account_map, as_of = portfolio_data

        # 2. Resolve sector / region metadata per security
        sector_map = await self._load_sector_metadata(list(security_map.keys()))
        region_map = await self._load_region_metadata(list(security_map.keys()))

        # 3. Compute per-holding allocation values
        #    (with optional currency conversion)
        holding_allocs: list[_HoldingAlloc] = []
        for h in holdings_list:
            sec = security_map.get(str(h.security_id))
            acct = account_map.get(str(h.account_id))

            raw_value = h.market_value or _ZERO
            native_currency = h.currency_code

            converted_value = raw_value
            effective_currency = native_currency

            if (
                target_currency is not None
                and self._fx_service is not None
                and native_currency != target_currency
                and raw_value > _ZERO
            ):
                conv_result = await self._fx_service.convert(
                    FxConversionRequest(
                        from_currency=native_currency,
                        to_currency=target_currency,
                        amount=raw_value,
                    )
                )
                if conv_result is not None:
                    converted_value = conv_result.converted_amount
                    effective_currency = target_currency

            holding_allocs.append(
                _HoldingAlloc(
                    security_id=str(h.security_id),
                    account_id=str(h.account_id),
                    account_name=acct.name if acct else str(h.account_id),
                    account_type=(
                        str(acct.account_type) if acct else "unknown"
                    ),
                    asset_class=(str(sec.security_type) if sec else "other"),
                    sector=sector_map.get(str(h.security_id), _UNCLASSIFIED),
                    region=region_map.get(str(h.security_id), _UNCLASSIFIED),
                    value=converted_value,
                    native_value=raw_value,
                    native_currency=native_currency,
                    effective_currency=effective_currency,
                )
            )

        total_value = sum((ha.value for ha in holding_allocs), _ZERO)
        effective_currency = target_currency or _pick_dominant_currency(
            [ha.native_currency for ha in holding_allocs]
        )

        # 4. Aggregate by asset class
        by_asset_class = _aggregate_buckets(
            holding_allocs, key_attr="asset_class", total=total_value
        )

        # 5. Aggregate by sector
        by_sector = _aggregate_buckets(
            holding_allocs, key_attr="sector", total=total_value
        )

        # 6. Aggregate by region
        by_region = _aggregate_buckets(
            holding_allocs, key_attr="region", total=total_value
        )

        # 7. Per-account breakdowns
        account_breakdowns: list[AccountAllocationBreakdown] = []
        by_account: dict[str, list[_HoldingAlloc]] = {}
        for ha in holding_allocs:
            by_account.setdefault(ha.account_id, []).append(ha)

        for acct_id, acct_holdings in by_account.items():
            acct_total = sum((h.value for h in acct_holdings), _ZERO)
            first = acct_holdings[0]
            account_breakdowns.append(
                AccountAllocationBreakdown(
                    account_id=acct_id,
                    account_name=first.account_name,
                    account_type=first.account_type,
                    total_value=acct_total,
                    by_asset_class=_aggregate_buckets(
                        acct_holdings,
                        key_attr="asset_class",
                        total=acct_total,
                    ),
                    by_sector=_aggregate_buckets(
                        acct_holdings,
                        key_attr="sector",
                        total=acct_total,
                    ),
                    by_region=_aggregate_buckets(
                        acct_holdings,
                        key_attr="region",
                        total=acct_total,
                    ),
                )
            )

        return AllocationResponse(
            total_value=total_value,
            currency_code=effective_currency,
            by_asset_class=by_asset_class,
            by_sector=by_sector,
            by_region=by_region,
            accounts=account_breakdowns,
            as_of=as_of,
        )

    # ── Data Loading ──────────────────────────────────────────────────

    async def _load_portfolio_data(
        self,
        tenant_id: str,
        *,
        account_id: str | None = None,
    ) -> (
        tuple[
            list[Holding],
            dict[str, Security],
            dict[str, Account],
            datetime,
        ]
        | None
    ):
        """Load the latest holding snapshots and related entities.

        Returns (holdings, security_map, account_map, as_of_timestamp)
        or None if no holdings exist.
        """
        # Latest holding per (account_id, security_id)
        latest_subq = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_ts"),
            )
            .where(Holding.tenant_id == tenant_id)
            .group_by(Holding.account_id, Holding.security_id)
        ).subquery()

        conditions: list[Any] = [
            Holding.tenant_id == tenant_id,
        ]
        if account_id is not None:
            conditions.append(Holding.account_id == account_id)

        holdings_q = (
            select(Holding)
            .join(
                latest_subq,
                and_(
                    Holding.account_id == latest_subq.c.account_id,
                    Holding.security_id == latest_subq.c.security_id,
                    Holding.observed_at == latest_subq.c.latest_ts,
                ),
            )
            .where(*conditions)
            .order_by(Holding.account_id)
        )
        result = await self._session.execute(holdings_q)
        holdings: list[Holding] = list(result.scalars().all())

        if not holdings:
            return None

        # Determine the "as of" timestamp (most recent observation)
        observed_times = [
            h.observed_at for h in holdings if h.observed_at is not None
        ]
        as_of = max(observed_times) if observed_times else datetime.now(UTC)

        # Resolve security IDs
        security_ids = list({h.security_id for h in holdings})
        account_ids = list({h.account_id for h in holdings})

        # Fetch accounts
        acct_result = await self._session.execute(
            select(Account).where(
                Account.id.in_(account_ids),
                Account.tenant_id == tenant_id,
            )
        )
        account_map: dict[str, Account] = {
            str(a.id): a for a in acct_result.scalars().all()
        }

        # Fetch securities
        sec_result = await self._session.execute(
            select(Security).where(Security.id.in_(security_ids))
        )
        security_map: dict[str, Security] = {
            str(s.id): s for s in sec_result.scalars().all()
        }

        return holdings, security_map, account_map, as_of

    async def _load_sector_metadata(
        self,
        security_ids: list[str],
    ) -> dict[str, str]:
        """Load sector classification per security.

        Looks at ``SecurityMetadataObservation`` with
        ``metadata_type='sector_exposure'``.  Returns the most recent
        sector label per security, or ``'Unclassified'``.
        """
        if not security_ids:
            return {}

        # Get the latest sector metadata observation per security
        latest_subq = (
            select(
                SecurityMetadataObservation.security_id,
                func.max(SecurityMetadataObservation.timestamp).label(
                    "latest_ts"
                ),
            )
            .where(
                SecurityMetadataObservation.metadata_type == "sector_exposure",
                SecurityMetadataObservation.security_id.in_(security_ids),
            )
            .group_by(SecurityMetadataObservation.security_id)
        ).subquery()

        stmt = (
            select(SecurityMetadataObservation)
            .join(
                latest_subq,
                and_(
                    SecurityMetadataObservation.security_id
                    == latest_subq.c.security_id,
                    SecurityMetadataObservation.timestamp
                    == latest_subq.c.latest_ts,
                ),
            )
            .where(
                SecurityMetadataObservation.metadata_type == "sector_exposure"
            )
        )
        result = await self._session.execute(stmt)
        rows: list[SecurityMetadataObservation] = list(result.scalars().all())

        sector_map: dict[str, str] = {}
        for row in rows:
            label = row.label
            if label:
                sector_map[str(row.security_id)] = label
            else:
                # Try extracting from metadata_json
                meta = row.metadata_json or {}
                sector = (
                    meta.get("sector") or meta.get("industry") or _UNCLASSIFIED
                )
                sector_map[str(row.security_id)] = sector

        return sector_map

    async def _load_region_metadata(
        self,
        security_ids: list[str],
    ) -> dict[str, str]:
        """Load region / geographic classification per security.

        Checks ``SecurityMetadataObservation`` with
        ``metadata_type='company_profile'`` for headquarters country/region,
        or falls back to the security's currency as a rough proxy.
        """
        if not security_ids:
            return {}

        # Look for company_profile metadata that may contain region info
        latest_subq = (
            select(
                SecurityMetadataObservation.security_id,
                func.max(SecurityMetadataObservation.timestamp).label(
                    "latest_ts"
                ),
            )
            .where(
                SecurityMetadataObservation.metadata_type.in_(
                    ["company_profile", "sector_exposure"]
                ),
                SecurityMetadataObservation.security_id.in_(security_ids),
            )
            .group_by(SecurityMetadataObservation.security_id)
        ).subquery()

        stmt = (
            select(SecurityMetadataObservation)
            .join(
                latest_subq,
                and_(
                    SecurityMetadataObservation.security_id
                    == latest_subq.c.security_id,
                    SecurityMetadataObservation.timestamp
                    == latest_subq.c.latest_ts,
                ),
            )
            .where(
                SecurityMetadataObservation.metadata_type.in_(
                    ["company_profile", "sector_exposure"]
                )
            )
        )
        result = await self._session.execute(stmt)
        rows: list[SecurityMetadataObservation] = list(result.scalars().all())

        region_map: dict[str, str] = {}
        for row in rows:
            meta = row.metadata_json or {}
            if row.metadata_type == "company_profile":
                region = (
                    meta.get("country")
                    or meta.get("region")
                    or meta.get("headquarters_country")
                )
                if region:
                    region_map[str(row.security_id)] = _region_normalise(region)
            elif row.metadata_type == "sector_exposure":
                region = meta.get("region")
                if region:
                    region_map.setdefault(
                        str(row.security_id), _region_normalise(region)
                    )

        return region_map


# ── Internal helpers ───────────────────────────────────────────────────


class _HoldingAlloc:
    """Internal holding allocation data (not a Pydantic model)."""

    __slots__ = (
        "account_id",
        "account_name",
        "account_type",
        "asset_class",
        "effective_currency",
        "native_currency",
        "native_value",
        "region",
        "sector",
        "security_id",
        "value",
    )

    def __init__(
        self,
        *,
        security_id: str,
        account_id: str,
        account_name: str,
        account_type: str,
        asset_class: str,
        sector: str,
        region: str,
        value: E,
        native_value: E,
        native_currency: str,
        effective_currency: str,
    ) -> None:
        self.security_id = security_id
        self.account_id = account_id
        self.account_name = account_name
        self.account_type = account_type
        self.asset_class = asset_class
        self.sector = sector
        self.region = region
        self.value = value
        self.native_value = native_value
        self.native_currency = native_currency
        self.effective_currency = effective_currency


def _aggregate_buckets(
    items: list[_HoldingAlloc],
    *,
    key_attr: str,
    total: E,
) -> list[AllocationBucket]:
    """Aggregate holding allocations by a string attribute.

    Args:
        items: List of holding allocations.
        key_attr: Attribute name to group by (e.g. 'asset_class').
        total: Total portfolio value for percentage calculation.

    Returns:
        Sorted list of AllocationBucket (highest value first).
    """
    buckets: dict[str, E] = {}
    for item in items:
        key = getattr(item, key_attr, _UNCLASSIFIED)
        buckets[key] = buckets.get(key, _ZERO) + item.value

    return [
        AllocationBucket(
            name=name,
            value=val,
            percentage=_safe_pct(val, total),
        )
        for name, val in sorted(
            buckets.items(), key=lambda x: x[1], reverse=True
        )
    ]


def _safe_pct(part: E, total: E) -> E:
    """Compute (part / total) * 100, returning 0 when total is 0."""
    if total == _ZERO:
        return _ZERO
    return (part / total * E("100")).quantize(E("0.01"))


def _region_normalise(region: str) -> str:
    """Normalise a region/country name to a standard geographic region."""
    mapping = {
        # Countries → region
        "US": "North America",
        "USA": "North America",
        "United States": "North America",
        "Canada": "North America",
        "CA": "North America",
        "GB": "Europe",
        "UK": "Europe",
        "United Kingdom": "Europe",
        "DE": "Europe",
        "Germany": "Europe",
        "FR": "Europe",
        "France": "Europe",
        "NL": "Europe",
        "Netherlands": "Europe",
        "CH": "Europe",
        "Switzerland": "Europe",
        "SE": "Europe",
        "Sweden": "Europe",
        "DK": "Europe",
        "Denmark": "Europe",
        "FI": "Europe",
        "Finland": "Europe",
        "NO": "Europe",
        "Norway": "Europe",
        "ES": "Europe",
        "Spain": "Europe",
        "IT": "Europe",
        "Italy": "Europe",
        "JP": "Asia Pacific",
        "Japan": "Asia Pacific",
        "CN": "Asia Pacific",
        "China": "Asia Pacific",
        "HK": "Asia Pacific",
        "Hong Kong": "Asia Pacific",
        "AU": "Asia Pacific",
        "Australia": "Asia Pacific",
        "NZ": "Asia Pacific",
        "New Zealand": "Asia Pacific",
        "IN": "Asia Pacific",
        "India": "Asia Pacific",
        "KR": "Asia Pacific",
        "South Korea": "Asia Pacific",
        "SG": "Asia Pacific",
        "Singapore": "Asia Pacific",
        "TW": "Asia Pacific",
        "Taiwan": "Asia Pacific",
        "BR": "Latin America",
        "Brazil": "Latin America",
        "MX": "Latin America",
        "Mexico": "Latin America",
        "ZA": "Middle East & Africa",
        "South Africa": "Middle East & Africa",
        "AE": "Middle East & Africa",
        "UAE": "Middle East & Africa",
        "RU": "Europe",
        "Russia": "Europe",
    }

    cleaned = region.strip()
    # Direct country/region lookup
    if cleaned in mapping:
        return mapping[cleaned]

    # Check case-insensitively
    for key, value in mapping.items():
        if key.lower() == cleaned.lower():
            return value

    # If it's a 2-letter code we don't know, return as-is
    return cleaned


def _pick_dominant_currency(currencies: list[str]) -> str:
    """Pick the most common currency from a list."""
    if not currencies:
        return "EUR"
    counts: dict[str, int] = {}
    for c in currencies:
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get)
