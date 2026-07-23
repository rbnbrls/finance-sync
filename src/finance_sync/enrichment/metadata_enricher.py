"""MetadataEnricher — fundamentals, ETF composition, and sector metadata
enrichment for securities.

Orchestrates fetching fundamental data, ETF composition details, and
sector/industry exposure from OpenBB (via the gateway) and persists
the results as FundamentalObservation and SecurityMetadataObservation
records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from finance_sync.enrichment.models import (
    ETFComposition,
    FundamentalObservationData,
    FundamentalRatioSummary,
    SectorExposure,
    SecurityMetadataObservationData,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from finance_sync.db.uow import UnitOfWork
    from finance_sync.enrichment.gateway import EnrichmentGateway
    from finance_sync.models.fundamental_observation import (
        FundamentalObservation,
    )
    from finance_sync.models.security_metadata_observation import (
        SecurityMetadataObservation,
    )


# ── GICS sector reference ───────────────────────────────────────────────

# Mapping of common sector keywords to normalised sector names.
# Used for classifying sector exposure from raw data.
GICS_SECTORS: dict[str, str] = {
    "technology": "Technology",
    "information technology": "Technology",
    "software": "Technology",
    "hardware": "Technology",
    "semiconductors": "Technology",
    "financials": "Financials",
    "bank": "Financials",
    "banking": "Financials",
    "insurance": "Financials",
    "diversified financials": "Financials",
    "healthcare": "Health Care",
    "health": "Health Care",
    "pharmaceuticals": "Health Care",
    "biotechnology": "Health Care",
    "consumer discretionary": "Consumer Discretionary",
    "consumer cyclical": "Consumer Discretionary",
    "retail": "Consumer Discretionary",
    "automotive": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "consumer defensive": "Consumer Staples",
    "food & beverage": "Consumer Staples",
    "food and beverage": "Consumer Staples",
    "energy": "Energy",
    "oil & gas": "Energy",
    "oil and gas": "Energy",
    "industrials": "Industrials",
    "industrial": "Industrials",
    "manufacturing": "Industrials",
    "materials": "Materials",
    "basic materials": "Materials",
    "mining": "Materials",
    "chemicals": "Materials",
    "utilities": "Utilities",
    "electric": "Utilities",
    "real estate": "Real Estate",
    "property": "Real Estate",
    "communication services": "Communication Services",
    "telecommunications": "Communication Services",
    "telecom": "Communication Services",
    "media": "Communication Services",
}


class MetadataEnricher:
    """Orchestrates fundamental and metadata enrichment for securities.

    This service:
    - Fetches fundamental ratio data from OpenBB via the gateway
    - Resolves ETF composition (holdings, weights, sector/region exposure)
    - Classifies sector exposure for individual securities
    - Persists enriched data as observation records
    """

    def __init__(
        self,
        uow: UnitOfWork,
        gateway: EnrichmentGateway,
    ) -> None:
        self._uow = uow
        self._gateway = gateway

    # ── Public API ───────────────────────────────────────────────────────

    async def enrich_security(
        self,
        security_id: str,
        identifier: str,
        identifier_type: str = "ticker",
        *,
        security_type: str | None = None,
    ) -> dict[str, Any]:
        """Run full metadata enrichment for a single security.

        Fetches fundamentals, ETF composition (if applicable), and
        sector exposure.  Returns a summary of what was enriched.
        """
        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "security_id": security_id,
            "fundamentals": False,
            "etf_composition": False,
            "sector_exposure": False,
        }

        # 1. Fundamentals
        fund_data = await self._fetch_and_store_fundamentals(
            security_id=security_id,
            identifier=identifier,
            identifier_type=identifier_type,
            observed_at=now,
        )
        if fund_data is not None:
            result["fundamentals"] = True

        # 2. ETF composition (if security is an ETF)
        if security_type and security_type.lower() == "etf":
            etf_data = await self._fetch_and_store_etf_composition(
                security_id=security_id,
                identifier=identifier,
                observed_at=now,
            )
            if etf_data is not None:
                result["etf_composition"] = True

        # 3. Sector exposure (from fundamentals or gateway)
        sector_data = await self._classify_and_store_sector_exposure(
            security_id=security_id,
            identifier=identifier,
            identifier_type=identifier_type,
            observed_at=now,
        )
        if sector_data is not None:
            result["sector_exposure"] = True

        return result

    # ── Fundamentals ─────────────────────────────────────────────────────

    async def get_fundamentals(
        self,
        identifier: str,
        identifier_type: str = "ticker",
    ) -> FundamentalObservationData | None:
        """Fetch fundamental ratio data from OpenBB via the gateway.

        Returns None in degraded mode or if the provider returns no data.
        """
        if self._gateway.is_degraded:
            return None

        try:
            return await self._gateway.get_fundamentals(
                identifier=identifier,
                identifier_type=identifier_type,
            )
        except Exception:
            return None

    async def compute_ratio_summary(
        self,
        security_id: str,
    ) -> FundamentalRatioSummary | None:
        """Compute a condensed ratio summary from the most recent
        fundamental observation.
        """
        obs = await self._get_latest_fundamental(security_id)
        if obs is None:
            return None

        return FundamentalRatioSummary(
            pe_ratio=obs.pe_ratio,
            forward_pe=obs.forward_pe,
            dividend_yield=obs.dividend_yield,
            eps=obs.eps,
            market_cap=obs.market_cap,
            beta=obs.beta,
        )

    async def get_recent_fundamentals(
        self,
        security_id: str,
        limit: int = 5,
    ) -> list[FundamentalObservationData]:
        """Return recent fundamental observations for a security."""
        observations = await self._list_recent_fundamentals(
            security_id, limit=limit
        )
        return [_fund_obs_to_dto(o) for o in observations]

    # ── ETF Composition ──────────────────────────────────────────────────

    async def get_etf_composition(
        self,
        identifier: str,
        identifier_type: str = "ticker",
    ) -> ETFComposition | None:
        """Fetch full ETF composition data from OpenBB via the gateway.

        Returns None in degraded mode or for non-ETF securities.
        """
        if self._gateway.is_degraded:
            return None

        try:
            return await self._gateway.get_etf_composition(
                identifier=identifier,
                identifier_type=identifier_type,
            )
        except Exception:
            return None

    # ── Sector Exposure ──────────────────────────────────────────────────

    def classify_sector(
        self,
        sector_name: str | None,
    ) -> str | None:
        """Normalise a raw sector string to a GICS sector name.

        Uses a keyword-based mapping to classify sectors from
        provider data.  Returns None if the string cannot be matched.
        """
        if not sector_name:
            return None

        cleaned = sector_name.strip().lower()
        # Try exact match first
        if cleaned in GICS_SECTORS:
            return GICS_SECTORS[cleaned]

        # Try partial keyword match
        for keyword, gics_name in GICS_SECTORS.items():
            if keyword in cleaned:
                return gics_name

        return None

    def classify_sector_exposures(
        self,
        raw_exposures: list[dict[str, Any]],
    ) -> list[SectorExposure]:
        """Classify raw sector exposure data into normalised
        SectorExposure DTOs.

        Each raw entry should have 'sector' (or 'name') and
        'weight' (or 'exposure' / 'percentage') keys.
        """
        result: list[SectorExposure] = []
        for item in raw_exposures:
            sector_raw = item.get("sector") or item.get("name") or item.get("industry")
            if not sector_raw:
                continue

            normalised = self.classify_sector(sector_raw) or sector_raw.title()
            weight = _extract_weight(item)
            if weight is not None:
                result.append(
                    SectorExposure(sector=normalised, weight=weight)
                )

        return result

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _fetch_and_store_fundamentals(
        self,
        security_id: str,
        identifier: str,
        identifier_type: str,
        *,
        observed_at: datetime,
    ) -> FundamentalObservationData | None:
        """Fetch fundamentals from gateway and persist to DB."""
        fund_data = await self.get_fundamentals(
            identifier=identifier,
            identifier_type=identifier_type,
        )
        if fund_data is None:
            return None

        # Re-assign security_id
        fund_data.security_id = security_id

        # Persist as FundamentalObservation
        await self._store_fundamental_observation(fund_data)

        # Also persist as a fundamental_ratios metadata observation
        await self._store_fundamental_metadata_observation(
            security_id=security_id,
            fund_data=fund_data,
            observed_at=observed_at,
        )

        return fund_data

    async def _fetch_and_store_etf_composition(
        self,
        security_id: str,
        identifier: str,
        *,
        observed_at: datetime,
    ) -> ETFComposition | None:
        """Fetch ETF composition from gateway and persist to DB."""
        etf_data = await self.get_etf_composition(
            identifier=identifier,
            identifier_type="ticker",
        )
        if etf_data is None:
            return None

        metadata_json: dict[str, Any] = {
            "etf_name": etf_data.etf_name,
            "total_holdings": etf_data.total_holdings,
            "holdings": [h.model_dump(mode="json") for h in etf_data.holdings],
            "sector_exposures": [
                s.model_dump(mode="json")
                for s in etf_data.sector_exposures
            ],
            "region_exposures": [
                r.model_dump(mode="json")
                for r in etf_data.region_exposures
            ],
            "expense_ratio": (
                str(etf_data.expense_ratio)
                if etf_data.expense_ratio is not None
                else None
            ),
            "dividend_yield": (
                str(etf_data.dividend_yield)
                if etf_data.dividend_yield is not None
                else None
            ),
        }

        obs = SecurityMetadataObservationData(
            security_id=security_id,
            metadata_type="etf_composition",
            timestamp=observed_at,
            metadata_json=metadata_json,
            label=etf_data.etf_name,
            source=etf_data.source,
        )
        await self._store_metadata_observation(obs)
        return etf_data

    async def _classify_and_store_sector_exposure(
        self,
        security_id: str,
        identifier: str,
        identifier_type: str,
        *,
        observed_at: datetime,
    ) -> SecurityMetadataObservationData | None:
        """Classify sector exposure from fundamentals or gateway data."""
        # Try to get sector info from fundamentals
        fund_data = await self._get_latest_fundamental(security_id)
        if fund_data is not None and fund_data.provider_metadata:
            raw_sector = fund_data.provider_metadata.get("sector")
            if raw_sector:
                sector_name = self.classify_sector(raw_sector) or raw_sector
                metadata_json: dict[str, Any] = {
                    "primary_sector": sector_name,
                    "sector_exposures": [
                        {"sector": sector_name, "weight": Decimal("1.0")}
                    ],
                }
                obs = SecurityMetadataObservationData(
                    security_id=security_id,
                    metadata_type="sector_exposure",
                    timestamp=observed_at,
                    metadata_json=metadata_json,
                    label=sector_name,
                    source="openbb",
                )
                await self._store_metadata_observation(obs)
                return obs

        # Try gateway sector info
        if not self._gateway.is_degraded:
            try:
                sector_info = await self._gateway.resolve_security(
                    identifier=identifier,
                    identifier_type=identifier_type,
                )
                if sector_info and sector_info.provider_metadata:
                    raw_sector = sector_info.provider_metadata.get("sector")
                    if raw_sector:
                        sector_name = (
                            self.classify_sector(raw_sector) or raw_sector
                        )
                        metadata_json = {
                            "primary_sector": sector_name,
                            "sector_exposures": [
                                {
                                    "sector": sector_name,
                                    "weight": Decimal("1.0"),
                                }
                            ],
                        }
                        obs = SecurityMetadataObservationData(
                            security_id=security_id,
                            metadata_type="sector_exposure",
                            timestamp=observed_at,
                            metadata_json=metadata_json,
                            label=sector_name,
                            source="openbb",
                        )
                        await self._store_metadata_observation(obs)
                        return obs
            except Exception:
                pass

        return None

    async def _store_fundamental_observation(
        self,
        data: FundamentalObservationData,
    ) -> None:
        """Persist a FundamentalObservation to the database."""
        from finance_sync.models.fundamental_observation import (
            FundamentalObservation,
        )

        # De-dupe: check for existing observation at this timestamp
        existing = await self._find_fundamental_observation(
            security_id=data.security_id,
            timestamp=data.timestamp,
            source=data.source,
        )
        if existing is not None:
            return

        obs = FundamentalObservation(
            security_id=data.security_id,
            timestamp=data.timestamp,
            pe_ratio=data.pe_ratio,
            forward_pe=data.forward_pe,
            peg_ratio=data.peg_ratio,
            eps=data.eps,
            eps_forward=data.eps_forward,
            book_value_per_share=data.book_value_per_share,
            dividend_yield=data.dividend_yield,
            dividend_rate=data.dividend_rate,
            market_cap=data.market_cap,
            enterprise_value=data.enterprise_value,
            shares_outstanding=data.shares_outstanding,
            beta=data.beta,
            high_52w=data.high_52w,
            low_52w=data.low_52w,
            source=data.source,
            provider_metadata=data.provider_metadata,
        )
        await self._uow.fundamental_observations.add(obs)

    async def _store_fundamental_metadata_observation(
        self,
        security_id: str,
        fund_data: FundamentalObservationData,
        *,
        observed_at: datetime,
    ) -> None:
        """Persist fundamental data as a fundamental_ratios metadata observation."""
        metadata_json: dict[str, Any] = {
            "pe_ratio": (
                str(fund_data.pe_ratio)
                if fund_data.pe_ratio is not None
                else None
            ),
            "forward_pe": (
                str(fund_data.forward_pe)
                if fund_data.forward_pe is not None
                else None
            ),
            "eps": (
                str(fund_data.eps) if fund_data.eps is not None else None
            ),
            "dividend_yield": (
                str(fund_data.dividend_yield)
                if fund_data.dividend_yield is not None
                else None
            ),
            "market_cap": (
                str(fund_data.market_cap)
                if fund_data.market_cap is not None
                else None
            ),
            "beta": (
                str(fund_data.beta) if fund_data.beta is not None else None
            ),
            "source": fund_data.source,
        }
        if fund_data.provider_metadata:
            metadata_json["provider_metadata"] = fund_data.provider_metadata

        meta_obs = SecurityMetadataObservationData(
            security_id=security_id,
            metadata_type="fundamental_ratios",
            timestamp=observed_at,
            metadata_json=metadata_json,
            label=None,
            source=fund_data.source,
        )
        await self._store_metadata_observation(meta_obs)

    async def _store_metadata_observation(
        self,
        data: SecurityMetadataObservationData,
    ) -> None:
        """Persist a SecurityMetadataObservation to the database."""
        from finance_sync.models.security_metadata_observation import (
            SecurityMetadataObservation,
        )

        # De-dupe
        existing = await self._find_metadata_observation(
            security_id=data.security_id,
            metadata_type=data.metadata_type,
            timestamp=data.timestamp,
            source=data.source,
        )
        if existing is not None:
            return

        obs = SecurityMetadataObservation(
            security_id=data.security_id,
            metadata_type=data.metadata_type,
            timestamp=data.timestamp,
            metadata_json=data.metadata_json,
            label=data.label,
            source=data.source,
        )
        await self._uow.security_metadata_observations.add(obs)

    async def _find_fundamental_observation(
        self,
        security_id: str,
        timestamp: datetime,
        source: str,
    ) -> FundamentalObservation | None:
        """Check for an existing fundamental observation."""
        from finance_sync.models.fundamental_observation import (
            FundamentalObservation,
        )

        results = await self._uow.fundamental_observations.list(
            FundamentalObservation.security_id == security_id,  # type: ignore[attr-defined]
            FundamentalObservation.timestamp == timestamp,  # type: ignore[attr-defined]
            FundamentalObservation.source == source,  # type: ignore[attr-defined]
        )
        if results:
            return results[0]
        return None

    async def _find_metadata_observation(
        self,
        security_id: str,
        metadata_type: str,
        timestamp: datetime,
        source: str,
    ) -> SecurityMetadataObservation | None:
        """Check for an existing metadata observation."""
        from finance_sync.models.security_metadata_observation import (
            SecurityMetadataObservation,
        )

        results = await self._uow.security_metadata_observations.list(
            SecurityMetadataObservation.security_id == security_id,  # type: ignore[attr-defined]
            SecurityMetadataObservation.metadata_type == metadata_type,  # type: ignore[attr-defined]
            SecurityMetadataObservation.timestamp == timestamp,  # type: ignore[attr-defined]
            SecurityMetadataObservation.source == source,  # type: ignore[attr-defined]
        )
        if results:
            return results[0]
        return None

    async def _get_latest_fundamental(
        self,
        security_id: str,
    ) -> FundamentalObservation | None:
        """Return the most recent fundamental observation for a security."""
        from finance_sync.models.fundamental_observation import (
            FundamentalObservation,
        )

        results = await self._uow.fundamental_observations.list(
            FundamentalObservation.security_id == security_id,  # type: ignore[attr-defined]
            order_by=FundamentalObservation.timestamp.desc(),  # type: ignore[attr-defined]
            limit=1,
        )
        if results:
            return results[0]
        return None

    async def _list_recent_fundamentals(
        self,
        security_id: str,
        limit: int = 5,
    ) -> Sequence[FundamentalObservation]:
        """Return recent fundamental observations ordered by time desc."""
        from finance_sync.models.fundamental_observation import (
            FundamentalObservation,
        )

        return await self._uow.fundamental_observations.list(
            FundamentalObservation.security_id == security_id,  # type: ignore[attr-defined]
            order_by=FundamentalObservation.timestamp.desc(),  # type: ignore[attr-defined]
            limit=limit,
        )


# ── Module-level helpers ─────────────────────────────────────────────────


def _fund_obs_to_dto(
    obs: FundamentalObservation,
) -> FundamentalObservationData:
    """Convert an ORM FundamentalObservation to a Pydantic DTO."""
    return FundamentalObservationData(
        security_id=obs.security_id,
        timestamp=obs.timestamp,
        pe_ratio=_to_decimal(obs.pe_ratio),
        forward_pe=_to_decimal(obs.forward_pe),
        peg_ratio=_to_decimal(obs.peg_ratio),
        eps=_to_decimal(obs.eps),
        eps_forward=_to_decimal(obs.eps_forward),
        book_value_per_share=_to_decimal(obs.book_value_per_share),
        dividend_yield=_to_decimal(obs.dividend_yield),
        dividend_rate=_to_decimal(obs.dividend_rate),
        market_cap=_to_decimal(obs.market_cap),
        enterprise_value=_to_decimal(obs.enterprise_value),
        shares_outstanding=_to_decimal(obs.shares_outstanding),
        beta=_to_decimal(obs.beta),
        high_52w=_to_decimal(obs.high_52w),
        low_52w=_to_decimal(obs.low_52w),
        source=obs.source,
        provider_metadata=obs.provider_metadata,
    )


def _to_decimal(value: Decimal | None) -> Decimal | None:
    """Convert a value to Decimal if it's not None."""
    if value is None:
        return None
    return Decimal(str(value))


def _extract_weight(item: dict[str, Any]) -> Decimal | None:
    """Extract a weight value from a dict, trying multiple keys."""
    for key in ("weight", "exposure", "percentage", "pct", "allocation"):
        raw = item.get(key)
        if raw is not None:
            try:
                return Decimal(str(raw))
            except (ValueError, TypeError, ArithmeticError):
                continue
    return None


# ── Normalised sector name helpers ──────────────────────────────────────


def get_gics_sectors() -> dict[str, str]:
    """Return the canonical GICS sector keyword mapping."""
    return dict(GICS_SECTORS)
